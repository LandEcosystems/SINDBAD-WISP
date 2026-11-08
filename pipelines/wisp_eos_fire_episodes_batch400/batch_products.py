"""Durable wave products, delayed input cleanup and per-region publication.

The scientific file validator and merger are the audited pilot implementation.
Only orchestration changes: a durable, verified wave can release its inputs
before other waves of the same region have been prepared.
"""
from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import time

import netCDF4 as nc
import numpy as np

PILOT = Path(__file__).resolve().parent.parent / 'wisp_eos_fire_episodes'


def _load(name, filename):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, PILOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


p = _load('_batch400_pilot_products', 'products.py')
selection_tools = _load('_batch400_pilot_selection', 'selection.py')


def _region(c):
    root = Path(c['root'])
    regions = p._read(root / 'manifests/regions.json')
    if len(regions) != 1:
        raise ValueError('A batch400 region root must contain exactly one region')
    token, region = next(iter(regions.items()))
    p._name(token)
    p._name(region['region_id'])
    manifest = p._read(root / 'manifests/selection.json')
    selection_tools.validate_manifest(manifest)
    if set(manifest['regions']) != {token}:
        raise ValueError('Region and selection identities differ')
    selected = manifest['regions'][token]
    if selected['region_id'] != region['region_id']:
        raise ValueError('Selected region identifier differs')
    for key in ('cube_sha256', 'lat', 'lon'):
        if key not in region or selected['cube_record'].get(key) != region[key]:
            raise ValueError('Selected source/grid identity differs: ' + key)
    audit = selected['source_audit']
    if (audit.get('all_centre_labels_finite_binary') is not True
            or audit.get('date_count') != 3641
            or audit.get('cube_sha256') != region['cube_sha256']):
        raise ValueError('Selection lacks a complete audited VIIRS time series')
    return token, region, manifest


def _wave(queue):
    matches = [w for w in queue['waves'] if w['wave_id'] == queue['active_wave']]
    if len(matches) != 1:
        raise ValueError('Active wave must resolve uniquely')
    wave = matches[0]
    keys = wave['keys']
    if not keys or len(set(keys)) != len(keys) or any(k not in queue['tasks'] for k in keys):
        raise ValueError('Invalid active-wave task list')
    return wave


def _wave_path(c, wave):
    value = wave['wave_id']
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('Wave identifier must be a nonnegative integer')
    return Path(c['root']) / 'reports/waves' / ('wave_%04d.json' % value)


def _wave_identity(c, queue):
    token, region, manifest = _region(c)
    wave = _wave(queue)
    receipts = []
    dates = []
    for key in wave['keys']:
        task = queue['tasks'][key]
        registered = manifest['tasks'].get(key)
        if (registered is None or task['region'] != token
                or any(task.get(k) != v for k, v in registered.items())):
            raise ValueError('Wave task differs from frozen selection: ' + key)
        if task.get('prediction_status') != 'done':
            raise ValueError('Wave prediction is incomplete: ' + key)
        receipt = p._read(Path(c['root']) / 'manifests/tasks' / key / 'output.json')
        p.validate_chunk(c, key, receipt, task)
        receipts.append(receipt)
        dates.extend(task['dates'])
    if dates != sorted(set(dates)):
        raise ValueError('Wave dates must be unique and increasing')
    if wave.get('days', len(dates)) != len(dates):
        raise ValueError('Wave date count differs')
    return dict(schema=1, validated=True, wave_id=wave['wave_id'], region=token,
                region_id=region['region_id'], days=len(dates), dates=dates,
                keys=wave['keys'], selection_sha256=manifest['selection_sha256'],
                deployment_fingerprint=p._read(Path(c['root']) / 'manifests/deployment.json')['fingerprint'],
                checkpoint_sha256=c['checkpoint_sha256'], model_commit=c['code_commit'],
                chunk_receipts=receipts, chunk_receipts_sha256=p._digest(receipts))


def verify_wave_outputs(c):
    """Validate every durable prediction and publish an immutable wave receipt."""
    import adapter
    q = adapter.Queue(c)
    with adapter.locked(Path(c['root']) / 'locks/products/wave.lock'):
        queue = q.snapshot()
        identity = _wave_identity(c, queue)
        path = _wave_path(c, _wave(queue))
        if path.exists():
            old = p._read(path)
            if any(old.get(k) != v for k, v in identity.items()):
                raise ValueError('Verified wave identity changed')
            if not isinstance(old.get('completed_epoch'), (int, float)) or not np.isfinite(old['completed_epoch']):
                raise ValueError('Invalid verified-wave publication time')
            return old
        receipt = dict(identity, completed=p._now(), completed_epoch=time.time())
        p._atomic(path, receipt)
        return receipt


def _cleanup_gate(queue, receipt, now):
    wave = _wave(queue)
    if wave['wave_id'] != receipt['wave_id'] or wave['keys'] != receipt['keys']:
        raise ValueError('Cleanup wave differs from verified wave')
    if queue.get('phase') != 'CLEANUP_WAIT' or queue.get('cleanup_authorized') is not True:
        return 'cleanup not authorized or not in cleanup phase'
    deadline = queue.get('cleanup_not_before_epoch')
    if not isinstance(deadline, (int, float)) or not np.isfinite(deadline):
        raise ValueError('Missing finite cleanup deadline')
    if deadline < receipt['completed_epoch'] + 600:
        raise ValueError('Cleanup deadline violates required 600-second retention')
    if now < deadline:
        return '600-second post-publication retention has not elapsed'
    for key in wave['keys']:
        task = queue['tasks'][key]
        if (task.get('input_status') not in {'ready', 'cleaned'}
                or task.get('prediction_status') != 'done' or task.get('kind') != 'episode'):
            raise ValueError('Cleanup task is not fully complete: ' + key)
        if any(task.get(k) is not None for k in ('lease', 'job', 'pid')):
            return 'active task lease: ' + key
    return None


def _no_symlinks(path):
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            if (Path(parent) / name).is_symlink():
                raise ValueError('Symlink inside registered input tree')


def _cleanup_wave_impl(c):
    """Remove only verified wave inputs after 600 seconds, leases and locks.

    Per-day input hashes, registrations and all predictions remain durable. A
    persisted intent permits recovery if the process died during rmtree.
    """
    import adapter
    q = adapter.Queue(c)
    budget = adapter.actual_budget(c)
    root = Path(c['root'])
    with adapter.locked(root / 'locks/products/wave.lock'):
        queue = q.snapshot()
        wave = _wave(queue)
        path = _wave_path(c, wave)
        receipt = p._read(p._owned_path(path, root))
        # Revalidate predictions before any deletion, even after partial cleanup.
        identity = _wave_identity(c, queue)
        if any(receipt.get(k) != v for k, v in identity.items()):
            raise ValueError('Cleanup receipt differs from current verified predictions')
        reason = _cleanup_gate(queue, receipt, time.time())
        if reason:
            return dict(cleaned=False, pending=True, reason=reason)
        with contextlib.ExitStack() as locks:
            try:
                for key in sorted(wave['keys']):
                    locks.enter_context(adapter.locked(root / 'locks/tasks' / (key + '.lock'), False))
            except BlockingIOError:
                return dict(cleaned=False, pending=True, reason='active task lock')
            plans = []
            for key in wave['keys']:
                if queue['tasks'][key].get('pre5_reused_prediction'):
                    from pre5_migration import verify_imported_prediction
                    verify_imported_prediction(c, key, queue['tasks'][key])
                    continue  # No new input tree was created for a copied prediction.
                registration = p._registration(c, key)
                task = queue['tasks'][key]
                input_path = Path(c['scratch']) / 'inputs' / key
                p._owned_path(input_path, Path(c['scratch']) / 'inputs', must_exist=False)
                if input_path.exists() and not input_path.is_dir():
                    raise ValueError('Registered input is not a directory')
                _no_symlinks(input_path)
                meta = root / 'manifests/tasks' / key
                days_path = p._owned_path(meta / 'input_days.json', root)
                days = p._read(days_path)
                if set(days) != set(task['dates']) or any(
                        set(day.get('arrays', {})) != p.INPUT_ARRAYS for day in days.values()):
                    raise ValueError('Retained seven-tensor input journal is incomplete')
                record = dict(key=key, wave_id=wave['wave_id'], deleted_path=str(input_path),
                              wave_receipt_sha256=p._sha(path),
                              registration_sha256=p._digest(registration),
                              input_days_sha256=p._sha(days_path))
                intent = meta / 'wave_cleanup_intent.json'
                if intent.exists() and p._read(intent) != record:
                    raise ValueError('Cleanup identity changed during resume')
                if not input_path.exists() and not intent.exists():
                    raise ValueError('Input disappeared without a registered cleanup intent')
                plans.append((key, input_path, meta, intent, record))
            for key, input_path, meta, intent, record in plans:
                with q.edit() as current:
                    reason = _cleanup_gate(current, receipt, time.time())
                    if reason:
                        return dict(cleaned=False, pending=True, reason=reason)
                    p._atomic(intent, record)
                    p._owned_path(input_path, Path(c['scratch']) / 'inputs', must_exist=False)
                    _no_symlinks(input_path)
                    if input_path.exists():
                        shutil.rmtree(input_path)
                    if budget is not None:
                        # Release measured bytes immediately after physical deletion.
                        # Repeating this after an interrupted cleanup is safe: a
                        # missing registered store is measured as zero, never credited twice.
                        budget.refresh_path(input_path)
                    p._atomic(meta / 'wave_cleanup.json', dict(record, completed=p._now()))
                    current['tasks'][key].update(input_status='cleaned', reserved=0,
                                                cleaned_at=p._now(), updated=p._now())
        return dict(cleaned=True, wave_id=wave['wave_id'], chunks=len(plans),
                    wave_receipt_sha256=p._sha(path))


def cleanup_wave(c):
    """Never delete while a CPU/GPU regional reader (including preflight) is active."""
    path = Path(c['root']) / 'locks/prediction_readers.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        try:
            fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(cleaned=False, pending=True, reason='active regional prediction reader')
        try:
            return _cleanup_wave_impl(c)
        finally:
            fcntl.lockf(stream, fcntl.LOCK_UN)


def cleanup_final_chunks(c, region, tasks, final):
    """Remove only registered chunk products after the final file is validated.

    Receipts and input hashes remain durable. An immutable per-file intent makes
    interruption between unlink and completion journalling safely resumable.
    Reused pilot files never enter this function.
    """
    import adapter
    root = Path(c['root'])
    q = adapter.Queue(c)
    with adapter.locked(root / 'locks/products' / (region['region_id'] + '.lock')):
        checked = p.validate_final(c, region, tasks)
        if checked != final:
            raise ValueError('Final receipt changed before chunk cleanup')
        expected = {receipt['key']: receipt for receipt in final['chunk_receipts']}
        if set(expected) != set(tasks):
            raise ValueError('Final chunk cleanup task set differs')
        with contextlib.ExitStack() as locks:
            for key in sorted(expected):
                locks.enter_context(adapter.locked(root / 'locks/tasks' / (key + '.lock'), False))
            state = q.snapshot()
            if state['phase'] not in {'MERGE', 'COMPLETE'} or set(state['tasks']) != set(tasks):
                raise ValueError('Final chunk cleanup is not authorized by region phase')
            for key in expected:
                task = state['tasks'][key]
                if (task.get('input_status') != 'cleaned' or task.get('prediction_status') != 'done'
                        or any(task.get(name) is not None for name in ('lease', 'job', 'pid'))):
                    raise ValueError('Final chunk cleanup requires cleaned inputs and no task owners')
            plans = []
            for key, receipt in expected.items():
                registration = p._registration(c, key)
                if registration['identity']['region'] != region['region_id']:
                    raise ValueError('Final cleanup registration belongs to another region')
                path = root / 'monthly' / region['region_id'] / (key + '.nc')
                if Path(receipt['path']) != path or path == Path(final['path']):
                    raise ValueError('Final cleanup references an unauthorized product path')
                p._owned_path(path, root, must_exist=False)
                if path.exists() and (not path.is_file() or p._sha(path) != receipt['sha256']):
                    raise ValueError('Chunk checksum differs before final cleanup: ' + key)
                meta = root / 'manifests/tasks' / key
                record = dict(key=key, deleted_path=str(path), region_id=region['region_id'],
                              chunk_sha256=receipt['sha256'], final_sha256=final['sha256'],
                              registration_sha256=p._digest(registration))
                intent = meta / 'final_chunk_cleanup_intent.json'
                if intent.exists() and p._read(intent) != record:
                    raise ValueError('Final cleanup identity changed during resume')
                if not path.exists() and not intent.exists():
                    raise ValueError('Chunk disappeared without final cleanup intent')
                plans.append((path, meta, intent, record))
            for path, meta, intent, record in plans:
                p._atomic(intent, record)
                p._owned_path(path, root, must_exist=False)
                if path.exists():
                    if p._sha(path) != record['chunk_sha256']:
                        raise ValueError('Chunk changed during final cleanup')
                    path.unlink()
                    fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                p._atomic(meta / 'final_chunk_cleanup.json', dict(record, completed=p._now()))
            # Final checksum is checked after cleanup as well; completion cannot
            # conceal damage to the only remaining scientific product.
            p.validate_final(c, region, tasks)
            report = dict(cleaned=True, chunks=len(plans), final_sha256=final['sha256'], completed=p._now())
            p._atomic(root / 'reports/final_chunk_cleanup.json', report)
            return report


def finalize_region(c):
    """Merge all selected dates, then safely retire intermediate predictions."""
    import adapter
    token, region, manifest = _region(c)
    if not manifest['regions'][token]['selected_dates']:
        return empty_result(c)
    tasks = adapter.Queue(c).snapshot()['tasks']
    if set(tasks) != set(manifest['tasks']):
        raise ValueError('Final task set differs from complete region selection')
    if any(t.get('prediction_status') != 'done' for t in tasks.values()):
        raise ValueError('Cannot publish a partially predicted region')
    if any(t.get('input_status') != 'cleaned' for t in tasks.values()):
        raise ValueError('Final publication requires completed wave input cleanup')
    receipt = p.consolidate(c, region, tasks)
    cleanup_final_chunks(c, region, tasks, receipt)
    p._atomic(Path(c['root']) / 'reports/final.json', receipt)
    return receipt


def _empty_identity(c, region, manifest):
    return dict(schema=1, validated=True, status='no_observed_fire',
                region_id=region['region_id'], days=0, chunks=0, episodes=[],
                date_start=None, date_end=None, cube_sha256=region['cube_sha256'],
                selection_sha256=manifest['selection_sha256'],
                deployment_fingerprint=p._read(Path(c['root']) / 'manifests/deployment.json')['fingerprint'],
                checkpoint_sha256=c['checkpoint_sha256'], model_commit=c['code_commit'])


def _validate_empty(c, region, manifest, path, receipt):
    identity = _empty_identity(c, region, manifest)
    if any(receipt.get(k) != v for k, v in identity.items()) or receipt.get('path') != str(path):
        raise ValueError('Empty product receipt identity differs')
    p._owned_path(path, Path(c['root']))
    if p._sha(path) != receipt.get('sha256'):
        raise ValueError('Empty product checksum differs')
    with nc.Dataset(path) as ds:
        ds.set_auto_maskandscale(False)
        dims = dict(time=0, lat=96, lon=96, query=10, axis=2, **{'class': 2}, bounds=2)
        if set(ds.dimensions) != set(dims) or any(len(ds.dimensions[k]) != v for k, v in dims.items()):
            raise ValueError('Empty product dimensions differ')
        if not ds.dimensions['time'].isunlimited():
            raise ValueError('Empty time dimension must be unlimited')
        for name, dimensions in dict(p.DIMS, episode_id=('time',)).items():
            if name not in ds.variables or ds[name].dimensions != dimensions:
                raise ValueError('Empty product schema differs: ' + name)
        for key in ('status', 'region_id', 'selection_sha256', 'deployment_fingerprint',
                    'checkpoint_sha256', 'model_commit'):
            if ds.getncattr(key) != identity[key]:
                raise ValueError('Empty product metadata differs: ' + key)
        for axis in ('lat', 'lon'):
            if not np.array_equal(ds[axis][:], np.asarray(region[axis])[16:112]):
                raise ValueError('Empty product coordinate differs: ' + axis)
        for axis in ('query', 'axis', 'class'):
            if not np.array_equal(ds[axis][:], np.arange(dims[axis])):
                raise ValueError('Empty product query coordinate differs')
        if (ds['time'].units != 'seconds since 1970-01-01 00:00:00'
                or ds['time'].calendar != 'proleptic_gregorian'):
            raise ValueError('Empty product time encoding differs')
    return receipt


def empty_result(c):
    """Publish a zero-time product only for a fully audited no-fire source."""
    import adapter
    token, region, manifest = _region(c)
    selected = manifest['regions'][token]
    if any(selected[k] for k in ('fire_dates', 'selected_dates', 'episodes', 'blocks')) or manifest['tasks']:
        raise ValueError('Nonempty fire selection cannot produce an empty result')
    root = Path(c['root'])
    path, report_path = p._final_paths(c, region)
    with adapter.locked(root / 'locks/products' / (region['region_id'] + '.lock')):
        if report_path.exists():
            receipt = _validate_empty(c, region, manifest, path, p._read(report_path))
        else:
            identity = _empty_identity(c, region, manifest)
            path.parent.mkdir(parents=True, exist_ok=True)
            p._owned_path(path, root, must_exist=False)
            temporary = path.with_suffix('.partial.nc')
            p._owned_path(temporary, root, must_exist=False)
            with nc.Dataset(temporary, 'w') as ds:
                for name, size in dict(time=None, lat=96, lon=96, query=10, axis=2,
                                       **{'class': 2}, bounds=2).items():
                    ds.createDimension(name, size)
                ds.setncatts({k: v for k, v in identity.items()
                             if isinstance(v, (str, int, float)) and not isinstance(v, bool)})
                ds.setncatts(dict(batch_size=8, precision='bfloat16', history_mode='observed_viirs',
                    input_protocol='history-only-vegetation/v1',
                    experiment='Retrospective observation-conditioned fire-episode hindcast',
                    unselected_dates='Absent; never interpreted as predicted zero fire',
                    empty_reason='Complete VIIRS centre labels contain no observed fire day; no model inference performed'))
                for name, dimensions in dict(p.DIMS, episode_id=('time',)).items():
                    dtype = ('i8' if name in {'time', 'time_bounds'} else 'i4' if name == 'episode_id'
                             else 'i2' if name in {'query', 'axis', 'class'}
                             else 'u1' if name in {'wisp_eos_valid_mask', 'noaa20_in_history'}
                             else 'f8' if name in {'lat', 'lon', 'query_latitude', 'query_longitude'} else 'f4')
                    ds.createVariable(name, dtype, dimensions, fill_value=False, zlib=True, complevel=3)
                ds['time'].units = 'seconds since 1970-01-01 00:00:00'
                ds['time'].calendar = 'proleptic_gregorian'
                for axis in ('lat', 'lon'):
                    ds[axis][:] = np.asarray(region[axis])[16:112]
                for axis in ('query', 'axis', 'class'):
                    ds[axis][:] = np.arange(len(ds.dimensions[axis]))
                ds.sync()
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            receipt = dict(identity, path=str(path), sha256=p._sha(path), completed=p._now(),
                           completed_epoch=time.time())
            _validate_empty(c, region, manifest, path, receipt)
            p._atomic(report_path, receipt)
        p._atomic(root / 'reports/final.json', receipt)
        return receipt


def verify_reuse(oldroot, c):
    """Reference a validated pilot product after per-region semantic comparison.

    The pilot global selection digest includes two regions, so it is deliberately
    not compared to a new single-region digest. Dates, source, grid, selection
    rules and model identities must nevertheless match exactly.
    """
    token, region, manifest = _region(c)
    oldroot = Path(oldroot)
    old_c = p._read(oldroot / 'config/deployment.json')
    if Path(old_c['root']) != oldroot:
        raise ValueError('Reuse root differs from deployed pilot root')
    for name in ('checkpoint_sha256', 'code_commit'):
        if old_c[name] != c[name]:
            raise ValueError('Reuse model identity differs: ' + name)
    if (p._read(oldroot / 'manifests/deployment.json')['fingerprint']
            != p._read(Path(c['root']) / 'manifests/deployment.json')['fingerprint']):
        raise ValueError('Reuse scientific preprocessing identity differs')
    old_regions = p._read(oldroot / 'manifests/regions.json')
    old_region = old_regions[token]
    for name in ('region_id', 'cube_sha256', 'lat', 'lon'):
        if old_region.get(name) != region[name]:
            raise ValueError('Reuse source/grid identity differs: ' + name)
    if old_region.get('day_start_hour_utc', 0) != region.get('day_start_hour_utc', 0):
        raise ValueError('Reuse prediction day boundary differs')
    old_manifest = p._read(oldroot / 'manifests/selection.json')
    selection_tools.validate_manifest(old_manifest)
    old_selected = old_manifest['regions'][token]
    for name in ('cube_sha256', 'lat', 'lon'):
        if old_selected['cube_record'].get(name) != region[name]:
            raise ValueError('Reuse selection source identity differs: ' + name)
    audit = old_selected['source_audit']
    if (audit.get('cube_sha256') != region['cube_sha256']
            or audit.get('date_count') != 3641
            or audit.get('all_centre_labels_finite_binary') is not True):
        raise ValueError('Reuse selection source audit is incomplete')
    if old_manifest['rules'] != manifest['rules']:
        raise ValueError('Reuse date-selection rules differ')
    for name in ('region_id', 'fire_dates', 'selected_dates', 'episodes', 'blocks'):
        if old_manifest['regions'][token][name] != manifest['regions'][token][name]:
            raise ValueError('Reuse per-region selection differs: ' + name)
    final = p.validate_final(old_c, old_region, old_manifest['tasks'])
    source_report = p._final_paths(old_c, old_region)[1]
    receipt = dict(final, status='reused', reused=True, source_root=str(oldroot),
                   source_receipt_sha256=p._sha(source_report),
                   cube_sha256=region['cube_sha256'], selection_sha256=manifest['selection_sha256'],
                   validated_reuse_at=p._now())
    root = Path(c['root'])
    p._atomic(root / 'reports/reuse.json', receipt)
    p._atomic(root / 'reports/final.json', receipt)
    return receipt


# Backend provenance is an operational extension. The frozen pilot file and all
# numerical arrays remain unchanged; its private/module-local validators are
# wrapped so an explicitly authorized CPU result is never mislabeled as CUDA.
BACKEND_VARS = {
    'inference_backend_id', 'inference_device_code', 'inference_precision_code',
    'inference_batch_size', 'inference_provenance_source_code',
}
BACKEND_POLICY = {'cuda': ('bfloat16', 8), 'cpu': ('float32', 1)}


def _backend_record(metadata, *, receipt=False):
    """Resolve explicit metadata, or audited legacy A100/bfloat16 metadata."""
    backend = metadata.get('inference_backend')
    hardware = metadata.get('hardware')
    if not isinstance(hardware, str) or not hardware.strip():
        raise ValueError('Inference provenance lacks hardware identity')
    legacy = backend is None
    if legacy:
        # Absence alone cannot establish a CPU/GPU identity. Existing audited
        # products were exclusively A100 batch=8/bfloat16; require that evidence.
        if 'A100' not in hardware:
            raise ValueError('Unidentified legacy inference hardware')
        backend = 'cuda'
    if backend == 'gpu':
        backend = 'cuda'
    if backend not in BACKEND_POLICY:
        raise ValueError('Unsupported inference backend: ' + str(backend))
    precision, batch = BACKEND_POLICY[backend]
    if metadata.get('precision', precision if legacy and receipt else None) != precision:
        raise ValueError('Inference provenance precision differs')
    if metadata.get('batch_size', batch if legacy and receipt else None) != batch:
        raise ValueError('Inference provenance batch size differs')
    return dict(inference_backend=backend, precision=precision, batch_size=batch,
                hardware=hardware, provenance_source=('legacy_frozen_gpu_metadata'
                                                       if legacy else 'explicit'))


def _dataset_backend(ds, receipt=None):
    metadata = {name: ds.getncattr(name) for name in ds.ncattrs()}
    record = _backend_record(metadata)
    if receipt is not None:
        other = _backend_record(receipt, receipt=True)
        if other != record:
            raise ValueError('Chunk receipt inference provenance differs from file')
    return record


def _read_backend_catalog(ds):
    import json
    present = BACKEND_VARS.intersection(ds.variables)
    if not present:
        return None
    if present != BACKEND_VARS:
        raise ValueError('Incomplete per-day inference provenance variables')
    try:
        catalog = json.loads(ds.getncattr('inference_backend_catalog_json'))
    except (AttributeError, ValueError, TypeError) as exc:
        raise ValueError('Missing or invalid inference backend catalog') from exc
    if not isinstance(catalog, list) or not catalog:
        raise ValueError('Empty inference backend catalog')
    normalized = []
    for record in catalog:
        if not isinstance(record, dict):
            raise ValueError('Invalid inference backend catalog item')
        # The explicit backend is retained in each normalized catalog item;
        # legacy source remains separately traceable, not reclassified explicit.
        source = record.get('provenance_source')
        if source not in {'explicit', 'legacy_frozen_gpu_metadata'}:
            raise ValueError('Unknown inference provenance source')
        expected = _backend_record(record, receipt=True)
        if source == 'legacy_frozen_gpu_metadata' and (
                expected['inference_backend'] != 'cuda' or 'A100' not in expected['hardware']):
            raise ValueError('Invalid legacy backend attribution')
        expected['provenance_source'] = source
        if record != expected or record in normalized:
            raise ValueError('Duplicate or inconsistent inference backend catalog')
        normalized.append(expected)
    n = len(ds.dimensions['time'])
    for name in BACKEND_VARS:
        if ds[name].dimensions != ('time',) or ds[name].dtype.kind not in 'iu':
            raise ValueError('Incorrect per-day inference provenance schema: ' + name)
    ids = np.asarray(ds['inference_backend_id'][:])
    if ids.shape != (n,) or np.any(ids < 0) or np.any(ids >= len(catalog)):
        raise ValueError('Invalid daily inference backend IDs')
    for index, record in enumerate(catalog):
        mask = ids == index
        expected = dict(inference_device_code=1 if record['inference_backend'] == 'cuda' else 2,
                        inference_precision_code=1 if record['precision'] == 'bfloat16' else 2,
                        inference_batch_size=record['batch_size'],
                        inference_provenance_source_code=1 if record['provenance_source'] == 'explicit' else 2)
        for name, value in expected.items():
            if not np.all(np.asarray(ds[name][:])[mask] == value):
                raise ValueError('Daily inference provenance disagrees with catalog: ' + name)
    summary = _backend_summary(catalog)
    for name, value in summary.items():
        if ds.getncattr(name) != value:
            raise ValueError('Global inference provenance disagrees with daily catalog: ' + name)
    return catalog, ids


def _backend_summary(catalog):
    def common(name, fallback):
        values = {r[name] for r in catalog}
        return next(iter(values)) if len(values) == 1 else fallback
    return dict(inference_backend=common('inference_backend', 'mixed'),
                precision=common('precision', 'mixed'),
                batch_size=common('batch_size', 0),
                hardware=','.join(sorted({r['hardware'] for r in catalog})))


class _FrozenMetadataView:
    """Reuse every frozen science check while validating backend attrs separately."""
    def __init__(self, dataset):
        self.dataset = dataset

    def __getattr__(self, name):
        return getattr(self.dataset, name)

    def __getitem__(self, name):
        return self.dataset[name]

    def getncattr(self, name):
        return {'precision': 'bfloat16', 'batch_size': 8}.get(name, self.dataset.getncattr(name))


def _daily_backend_records(receipt):
    record = _backend_record(receipt, receipt=True)
    n = receipt['days']
    hardware = receipt.get('inference_hardware_by_day')
    if hardware is None:
        return [record] * n
    if (record['inference_backend'] != 'cpu' or not isinstance(hardware, list)
            or len(hardware) != n or any(not isinstance(value, str) or not value.strip() for value in hardware)):
        raise ValueError('Invalid daily CPU processor receipt')
    if set(receipt.get('cpu_processor_models', [])) != set(hardware):
        raise ValueError('CPU processor summary disagrees with per-day receipt')
    return [dict(record, hardware=value) for value in hardware]


def _validate_backend_journal(c, key, receipt):
    with nc.Dataset(receipt['path']) as ds:
        record = _dataset_backend(ds, receipt)
    if record['inference_backend'] != 'cpu':
        return
    journal = p._read(Path(c['root']) / 'manifests/tasks' / key / 'prediction_days.json')
    records = _daily_backend_records(receipt)
    for index, expected in enumerate(records):
        day = journal[str(index)]
        metadata = day.get('timing', day)
        for name in ('inference_backend', 'precision', 'batch_size', 'hardware', 'provenance_source'):
            if metadata.get(name) != expected[name]:
                raise ValueError('CPU daily prediction journal lacks matching provenance: ' + name)


def _append_merged_provenance(path, receipts):
    import json
    catalog, ids = [], []
    for receipt in receipts:
        with nc.Dataset(receipt['path']) as src:
            record = _dataset_backend(src, receipt)
        for day_record in _daily_backend_records(receipt):
            if day_record not in catalog:
                catalog.append(day_record)
            ids.append(catalog.index(day_record))
    with nc.Dataset(path, 'a') as ds:
        if len(ids) != len(ds.dimensions['time']):
            raise ValueError('Merged provenance date count differs')
        if BACKEND_VARS.intersection(ds.variables):
            raise ValueError('Unexpected pre-existing provenance in merge target')
        ds.setncatts(_backend_summary(catalog))
        ds.inference_backend_catalog_json = json.dumps(catalog, sort_keys=True)
        ds.inference_provenance_note = (
            'Per-day backend, precision, batch size and hardware are recorded. '
            'CPU float32 and CUDA bfloat16 outputs may differ numerically. '
            'Legacy CUDA attribution is inferred from validated frozen A100 file metadata, '
            'rather than a contemporaneous per-day device journal.')
        ids = np.asarray(ids, dtype='i2')
        values = dict(inference_backend_id=ids,
            inference_device_code=np.asarray([1 if catalog[i]['inference_backend'] == 'cuda' else 2 for i in ids], dtype='u1'),
            inference_precision_code=np.asarray([1 if catalog[i]['precision'] == 'bfloat16' else 2 for i in ids], dtype='u1'),
            inference_batch_size=np.asarray([catalog[i]['batch_size'] for i in ids], dtype='i2'),
            inference_provenance_source_code=np.asarray([1 if catalog[i]['provenance_source'] == 'explicit' else 2 for i in ids], dtype='u1'))
        descriptions = {
            'inference_backend_id': 'Zero-based entry in inference_backend_catalog_json',
            'inference_device_code': '1=CUDA GPU; 2=CPU',
            'inference_precision_code': '1=bfloat16 autocast; 2=float32',
            'inference_batch_size': 'Requested inference batch size; final partial batch may be smaller',
            'inference_provenance_source_code': '1=explicit backend metadata; 2=inferred from validated legacy frozen GPU metadata',
        }
        for name, value in values.items():
            var = ds.createVariable(name, value.dtype, ('time',), fill_value=False, zlib=True, complevel=3)
            var.long_name = descriptions[name]
            var[:] = value
        ds.sync()
        _read_backend_catalog(ds)
    with Path(path).open('rb') as stream:
        os.fsync(stream.fileno())


def activate_products(module=None):
    """Idempotently extend a loaded pilot products module, never its source file."""
    module = p if module is None else module
    if getattr(module, '_batch400_mixed_products_active', False):
        return module
    original_dataset = module._validate_dataset
    original_chunk = module.validate_chunk
    original_merge = module._stream_merge
    original_final = module.validate_final

    def validate_dataset(ds, c, region, dates, monthly_receipt=None):
        original_dataset(_FrozenMetadataView(ds), c, region, dates, monthly_receipt)
        if _read_backend_catalog(ds) is None:
            _dataset_backend(ds, monthly_receipt)
        elif monthly_receipt is not None:
            raise ValueError('Final-product provenance unexpectedly found in a chunk')

    def validate_chunk(c, key, receipt, task=None):
        result = original_chunk(c, key, receipt, task)
        _validate_backend_journal(c, key, receipt)
        return result

    def stream_merge(c, region, receipts, temporary, dates, episode_ids, episodes, selection_sha):
        original_merge(c, region, receipts, temporary, dates, episode_ids, episodes, selection_sha)
        _append_merged_provenance(temporary, receipts)
        with nc.Dataset(temporary) as ds:
            validate_dataset(ds, c, region, dates)

    def validate_final(c, region, tasks, selection=None):
        result = original_final(c, region, tasks, selection)
        with nc.Dataset(result['path']) as ds:
            resolved = _read_backend_catalog(ds)
            if resolved is not None:
                catalog, ids = resolved
                expected = []
                for receipt in result['chunk_receipts']:
                    expected.extend(_daily_backend_records(receipt))
                if len(expected) != len(ids) or any(catalog[i] != record for i, record in zip(ids, expected)):
                    raise ValueError('Final daily backend provenance differs from immutable chunk receipts')
        return result

    module._validate_dataset = validate_dataset
    module.validate_chunk = validate_chunk
    module.validate_month = validate_chunk
    module._stream_merge = stream_merge
    module.validate_final = validate_final
    module._batch400_mixed_products_active = True
    return module


def validate_chunk_provenance(c, key, receipt, task=None):
    """Consumer-facing validator for either explicitly pinned CPU or GPU output."""
    return activate_products().validate_chunk(c, key, receipt, task)


activate_products()
