"""Durable wave products, delayed input cleanup and per-region publication.

The scientific file validator and merger are the audited pilot implementation.
Only orchestration changes: a durable, verified wave can release its inputs
before other waves of the same region have been prepared.
"""
from __future__ import annotations

import contextlib
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


def cleanup_wave(c):
    """Remove only verified wave inputs after 600 seconds, leases and locks.

    Per-day input hashes, registrations and all predictions remain durable. A
    persisted intent permits recovery if the process died during rmtree.
    """
    import adapter
    q = adapter.Queue(c)
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
                    p._atomic(meta / 'wave_cleanup.json', dict(record, completed=p._now()))
                    current['tasks'][key].update(input_status='cleaned', reserved=0,
                                                cleaned_at=p._now(), updated=p._now())
        return dict(cleaned=True, wave_id=wave['wave_id'], chunks=len(plans),
                    wave_receipt_sha256=p._sha(path))


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
