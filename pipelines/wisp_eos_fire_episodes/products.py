"""Validated sparse-date fire-episode products and isolated delayed cleanup.

The frozen writer still calls its short files ``monthly``; these files contain
registered event chunks, which may cross calendar months.  Scientific values and
identities are preserved.  Unselected dates are absent, never filled with zero.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import netCDF4 as nc
import numpy as np

DAY = 86400
DIMS = {
    'time': ('time',), 'time_bounds': ('time', 'bounds'),
    'lat': ('lat',), 'lon': ('lon',), 'query': ('query',),
    'axis': ('axis',), 'class': ('class',),
    'wisp_eos_fire_score': ('time', 'lat', 'lon'),
    'query_points_yx': ('time', 'query', 'axis'),
    'query_confidence': ('time', 'query'),
    'query_logits': ('time', 'query', 'class'),
    'query_latitude': ('time', 'query'),
    'query_longitude': ('time', 'query'),
    'wisp_eos_valid_mask': ('time', 'lat', 'lon'),
    'noaa20_in_history': ('time',),
}
UNIT_INTERVAL = {'wisp_eos_fire_score', 'query_points_yx', 'query_confidence'}
JOURNAL_ARRAYS = {'wisp_eos_fire_score', 'query_points_yx', 'query_confidence',
                  'query_logits', 'query_latitude', 'query_longitude', 'wisp_eos_valid_mask'}


def _read(path):
    return json.loads(Path(path).read_text())


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(2 ** 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _digest(value):
    # This digest belongs to the operations receipt; scientific fingerprints are
    # copied from the frozen deployment and never recomputed with this function.
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _array_sha(value):
    """Frozen wisp_batch.common.array_hash encoding, without importing Torch."""
    value = np.ascontiguousarray(value)
    h = hashlib.sha256()
    h.update(str(value.dtype).encode())
    h.update(str(value.shape).encode())
    h.update(memoryview(value).cast('B'))
    return h.hexdigest()


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    with temporary.open('w') as f:
        json.dump(value, f, sort_keys=True, indent=2, allow_nan=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _name(value):
    if not isinstance(value, str) or not value or Path(value).name != value or value in {'.', '..'}:
        raise ValueError('Unsafe path component: ' + repr(value))
    return value


def _owned_path(path, parent, must_exist=True):
    """Reject symlinks in any component below the specified trusted parent."""
    path, parent = Path(path), Path(parent)
    try:
        relative = path.relative_to(parent)
    except ValueError as exc:
        raise ValueError('Path outside authorized parent: ' + str(path)) from exc
    cursor = parent
    if cursor.is_symlink():
        raise ValueError('Symlink in authorized parent: ' + str(parent))
    for part in relative.parts:
        if part in {'.', '..'}:
            raise ValueError('Unsafe path: ' + str(path))
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError('Symlink in product/input path: ' + str(cursor))
    if must_exist and not path.is_file():
        raise ValueError('Missing regular file: ' + str(path))
    if path.resolve().is_relative_to(parent.resolve()) is False:
        raise ValueError('Resolved path outside authorized parent: ' + str(path))
    return path


def validate_date_axis(actual, expected, consecutive=False):
    actual, expected = np.asarray(actual), np.asarray(expected)
    if actual.ndim != 1 or expected.ndim != 1 or not len(expected):
        raise ValueError('Invalid empty or multidimensional time axis')
    if actual.dtype.kind not in 'iu' or not np.array_equal(actual, expected):
        raise ValueError('Output time mismatch: gaps, duplicates, order, or dates differ')
    if np.any(expected % DAY) or np.any(np.diff(expected) <= 0):
        raise ValueError('Expected time axis must contain unique increasing UTC midnights')
    if consecutive and np.any(np.diff(expected) != DAY):
        raise ValueError('Event chunk dates must be consecutive')


def _ticks(dates):
    values = np.asarray(dates, dtype='datetime64[s]')
    if np.isnat(values).any():
        raise ValueError('Invalid NaT prediction date')
    ticks = values.astype('int64')
    validate_date_axis(ticks, ticks)
    return ticks


def _registration(c, key):
    key = _name(key)
    root = Path(c['root'])
    path = _owned_path(root / 'manifests/tasks' / key / 'registration.json', root)
    registration = _read(path)
    expected_input = Path(c['scratch']) / 'inputs' / key
    if registration.get('key') != key or registration.get('scratch_path') != str(expected_input):
        raise ValueError('Invalid input registration: ' + key)
    identity = registration['identity']
    if identity['deployment'] != _read(root / 'manifests/deployment.json')['fingerprint']:
        raise ValueError('Registered scientific deployment mismatch: ' + key)
    _name(identity['region'])
    return registration


def _validate_dataset(ds, c, region, dates, monthly_receipt=None):
    ds.set_auto_maskandscale(False)
    sizes = {'time': len(dates), 'lat': 96, 'lon': 96, 'query': 10,
             'axis': 2, 'class': 2, 'bounds': 2}
    for name, size in sizes.items():
        if name not in ds.dimensions or len(ds.dimensions[name]) != size:
            raise ValueError('Incorrect dimension: ' + name)
    for name, dimensions in DIMS.items():
        if name not in ds.variables or ds[name].dimensions != dimensions:
            raise ValueError('Missing or incorrectly shaped variable: ' + name)
    deployment = _read(Path(c['root']) / 'manifests/deployment.json')['fingerprint']
    attrs = {'deployment_fingerprint': deployment, 'checkpoint_sha256': c['checkpoint_sha256'],
             'model_commit': c['code_commit'], 'region_id': region['region_id'],
             'batch_size': 8, 'precision': 'bfloat16', 'history_mode': 'observed_viirs',
             'input_protocol': 'history-only-vegetation/v1'}
    if monthly_receipt is not None:
        attrs.update(fingerprint=monthly_receipt['fingerprint'],
                     input_fingerprint=monthly_receipt['input_fingerprint'],
                     hardware=monthly_receipt['hardware'])
    for name, expected in attrs.items():
        if name not in ds.ncattrs() or ds.getncattr(name) != expected:
            raise ValueError('Output metadata differs: ' + name)
    validate_date_axis(ds['time'][:], dates)
    if ds['time'].getncattr('units') != 'seconds since 1970-01-01 00:00:00' or ds['time'].getncattr('calendar') != 'proleptic_gregorian':
        raise ValueError('Output time units/calendar mismatch')
    if not np.array_equal(ds['time_bounds'][:], np.stack([dates, dates + DAY], axis=1)):
        raise ValueError('Output bounds mismatch')
    for axis in ('lat', 'lon'):
        grid = np.asarray(region[axis])[16:112]
        if len(grid) != 96 or not np.isfinite(grid).all() or not np.array_equal(ds[axis][:], grid):
            raise ValueError('Output crop mismatch: ' + axis)
    for axis in ('query', 'axis', 'class'):
        if not np.array_equal(ds[axis][:], np.arange(sizes[axis])):
            raise ValueError('Output index coordinate mismatch: ' + axis)
    # Bound memory for a final 3641-day product; check every variable, including
    # optional variables a newer writer might add, on every day.
    for begin in range(0, len(dates), 31):
        end = min(begin + 31, len(dates))
        for name, var in ds.variables.items():
            index = [slice(None)] * var.ndim
            if 'time' in var.dimensions:
                index[var.dimensions.index('time')] = slice(begin, end)
            elif begin:
                continue
            a = np.asarray(var[tuple(index)] if index else var[...])
            if a.dtype.kind in 'fci' and not np.isfinite(a).all():
                raise ValueError('Nonfinite output values: ' + name)
            if name in UNIT_INTERVAL and (np.any(a < 0) or np.any(a > 1)):
                raise ValueError('Output outside [0,1]: ' + name)
            if name == 'wisp_eos_valid_mask' and not np.all(a == 1):
                raise ValueError('Incomplete prediction coverage')
            if name == 'noaa20_in_history' and not np.isin(a, [0, 1]).all():
                raise ValueError('Invalid source history flag')
        points = np.asarray(ds['query_points_yx'][begin:end])
        for axis, coord in enumerate(('latitude', 'longitude')):
            grid = np.asarray(region['lat' if axis == 0 else 'lon'])[16:112]
            expected = np.interp(points[..., axis] * 95, np.arange(96), grid)
            if not np.array_equal(ds['query_' + coord][begin:end], expected):
                raise ValueError('Query coordinate disagrees with query points: ' + coord)


def validate_chunk(c, key, receipt, task=None):
    """Verify a registered immutable 1–7 day result, journal and frozen identity."""
    registration = _registration(c, key)
    identity = registration['identity']
    root = Path(c['root'])
    expected = root / 'monthly' / identity['region'] / (key + '.nc')
    if receipt.get('validated') is not True or receipt.get('key') != key or Path(receipt.get('path', '')) != expected:
        raise ValueError('Unvalidated or unexpected durable output: ' + key)
    _owned_path(expected, root)
    if _sha(expected) != receipt['sha256']:
        raise ValueError('Monthly output checksum mismatch: ' + key)
    dates = np.asarray(identity['dates'], dtype='datetime64[s]').astype('int64')
    validate_date_axis(dates, dates, consecutive=True)
    if not 1 <= len(dates) <= 7:
        raise ValueError('Event chunks must contain between one and seven days')
    if task is not None and not np.array_equal(_ticks(task['dates']), dates):
        raise ValueError('Task/registration dates mismatch')
    if receipt['days'] != len(dates) or receipt.get('date_start') != identity['dates'][0] or receipt.get('date_end') != identity['dates'][-1]:
        raise ValueError('Incorrect receipt dates: ' + key)
    with nc.Dataset(expected) as ds:
        _validate_dataset(ds, c, dict(identity, region_id=identity['region']), dates, receipt)
        journal_path = _owned_path(root / 'manifests/tasks' / key / 'prediction_days.json', root)
        journal = _read(journal_path)
        if set(journal) != {str(i) for i in range(len(dates))}:
            raise ValueError('Prediction journal incomplete: ' + key)
        for i, date in enumerate(identity['dates']):
            day = journal[str(i)]
            if day.get('date') != date or day.get('fingerprint') != receipt['fingerprint']:
                raise ValueError('Prediction journal identity differs: ' + key)
            if set(day.get('arrays', {})) != JOURNAL_ARRAYS:
                raise ValueError('Prediction journal array coverage differs: ' + key)
            for name, checksum in day['arrays'].items():
                if _array_sha(ds[name][i]) != checksum:
                    raise ValueError('Prediction journal checksum differs: ' + name)
    if _sha(expected) != receipt['sha256']:
        raise ValueError('Monthly output changed during validation: ' + key)
    return {'passed': True, 'key': key, 'sha256': receipt['sha256'], 'checked': _now()}


# Existing frozen request-cleanup adapters may use the former function name.
# Both names validate the event registration; neither deletes anything.
validate_month = validate_chunk


def _final_paths(c, region):
    region_id = _name(region['region_id'])
    root = Path(c['root'])
    return (root / 'outputs' / region_id / ('wisp_eos_q10_' + region_id + '_fire_episodes.nc'),
            root / 'reports/regions' / (region_id + '.json'))


def _region_token(c, region):
    matches = [key for key, value in _read(Path(c['root']) / 'manifests/regions.json').items()
               if value['region_id'] == region['region_id']]
    if len(matches) != 1:
        raise ValueError('Region must resolve to exactly one registered token')
    return matches[0]


def _tasks(c, region, tasks):
    """Resolve only this region's registered tasks in increasing date order."""
    token = _region_token(c, region)
    if not isinstance(tasks, dict):
        queue = _read(Path(c['root']) / 'state/queue.json')
        tasks = {key: queue['tasks'][key] for key in tasks}
    result = {key: value for key, value in tasks.items() if value['region'] == token}
    if not result:
        raise ValueError('No event tasks for region')
    if any(value.get('kind') != 'episode' for value in result.values()):
        raise ValueError('Non-event task cannot enter episode product')
    return dict(sorted(result.items(), key=lambda item: (item[1]['dates'][0], item[0])))


def _selection_contract(c, region, tasks, selection=None):
    """Validate selected dates and derive stable, one-based episode IDs."""
    tasks = _tasks(c, region, tasks)
    concatenated = []
    for key, task in tasks.items():
        registration = _registration(c, key)['identity']
        if registration['region'] != region['region_id'] or any(
                registration[axis] != region[axis] for axis in ('lat', 'lon')):
            raise ValueError('Task belongs to another region or grid')
        tick = _ticks(task['dates'])
        validate_date_axis(tick, tick, consecutive=True)
        if not 1 <= len(tick) <= 7 or not np.array_equal(tick, _ticks(registration['dates'])):
            raise ValueError('Chunk length or registration differs from task')
        concatenated.extend(task['dates'])
    dates = _ticks(concatenated)
    # If no explicit selection was passed, read the frozen manifest.  Never
    # accept a subset simply because its currently finished tasks agree.
    if selection is None:
        manifest = _read(Path(c['root']) / 'manifests/selection.json')
        selection = manifest['regions'][_region_token(c, region)]
    selected = _ticks(selection['selected_dates'])
    validate_date_axis(dates, selected)
    ids = np.cumsum(np.r_[True, np.diff(dates) > DAY]).astype('i4')
    episodes = []
    for episode_id in np.unique(ids):
        mask = ids == episode_id
        episode_dates = dates[mask]
        episodes.append({'episode_id': int(episode_id),
                         'start': str(np.datetime64(int(episode_dates[0]), 's')),
                         'end': str(np.datetime64(int(episode_dates[-1]), 's')),
                         'days': int(mask.sum())})
    if 'episodes' in selection:
        expected = [(str(np.datetime64(e['start'], 's')), str(np.datetime64(e['end'], 's')))
                    for e in selection['episodes']]
        if expected != [(e['start'], e['end']) for e in episodes]:
            raise ValueError('Frozen episode bounds disagree with selected dates')
    identity = {'region_id': region['region_id'], 'tasks': [
        {'key': key, 'dates': value['dates']} for key, value in tasks.items()],
        'selected_dates': concatenated, 'episodes': episodes}
    return tasks, dates, ids, episodes, _digest(identity)


def _equal_arrays(left, right):
    a, b = np.asarray(left), np.asarray(right)
    return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()


def _compare_source(out, src, offset):
    if set(out.variables) - {'episode_id'} != set(src.variables) or set(out.dimensions) != set(src.dimensions):
        raise ValueError('Chunk schema mismatch')
    n = len(src.dimensions['time'])
    for name, var in src.variables.items():
        dest = out[name]
        if dest.dimensions != var.dimensions or dest.dtype != var.dtype:
            raise ValueError('Chunk variable schema differs: ' + name)
        if 'time' in var.dimensions:
            index = [slice(None)] * var.ndim
            index[var.dimensions.index('time')] = slice(offset, offset + n)
            merged = dest[tuple(index)]
        else:
            merged = dest[...]
        if not _equal_arrays(merged, var[...]):
            raise ValueError('Merged values differ from source: ' + name)


def _stream_merge(c, region, receipts, temporary, dates, episode_ids, episodes, selection_sha):
    pos = 0
    with nc.Dataset(temporary, 'w') as out:
        out.set_auto_maskandscale(False)
        for number, receipt in enumerate(receipts):
            with nc.Dataset(receipt['path']) as src:
                src.set_auto_maskandscale(False)
                if number == 0:
                    for name, dim in src.dimensions.items():
                        out.createDimension(name, len(dates) if name == 'time' else len(dim))
                    out.setncatts({k: src.getncattr(k) for k in src.ncattrs()
                                   if k not in {'fingerprint', 'input_fingerprint', 'hardware'}})
                    out.setncatts({'chunk_receipts_sha256': _digest(receipts),
                        'selection_contract_sha256': selection_sha,
                        'hardware': ','.join(sorted({r['hardware'] for r in receipts})),
                        'date_selection': date_selection_description(c),
                        'experiment': 'Retrospective observation-conditioned fire-episode hindcast; not a full-year forecast evaluation',
                        'unselected_dates': 'Absent; never interpreted as predicted zero fire',
                        'episode_catalog_json': json.dumps(episodes, sort_keys=True)})
                    for name, var in src.variables.items():
                        fill = var.getncattr('_FillValue') if '_FillValue' in var.ncattrs() else False
                        target = out.createVariable(name, var.dtype, var.dimensions,
                                                    zlib=True, complevel=3, fill_value=fill)
                        target.setncatts({k: var.getncattr(k) for k in var.ncattrs() if k != '_FillValue'})
                        target.set_auto_maskandscale(False)
                    ids = out.createVariable('episode_id', 'i4', ('time',), fill_value=False)
                    ids.long_name = 'One-based contiguous selected-date episode number within this region'
                    ids[:] = episode_ids
                n = len(src.dimensions['time'])
                for name, var in src.variables.items():
                    if name not in out.variables or var.dimensions != out[name].dimensions or var.dtype != out[name].dtype:
                        raise ValueError('Chunk variable schema mismatch: ' + name)
                    if 'time' in var.dimensions:
                        index = [slice(None)] * var.ndim
                        index[var.dimensions.index('time')] = slice(pos, pos + n)
                        out[name][tuple(index)] = var[...]
                    elif number == 0:
                        out[name][...] = var[...]
                _compare_source(out, src, pos)
                pos += n
        if pos != len(dates):
            raise ValueError('Merged selected-date axis incomplete')
        out.sync()
    with nc.Dataset(temporary) as out:
        _validate_dataset(out, c, region, dates)
        if not np.array_equal(out['episode_id'][:], episode_ids):
            raise ValueError('Episode index changed during merge')
        pos = 0
        for receipt in receipts:
            if _sha(receipt['path']) != receipt['sha256']:
                raise ValueError('Chunk changed during merge')
            with nc.Dataset(receipt['path']) as src:
                src.set_auto_maskandscale(False)
                _compare_source(out, src, pos)
                pos += len(src.dimensions['time'])
    with Path(temporary).open('rb') as f:
        os.fsync(f.fileno())


def date_selection_description(c):
    rules = _read(Path(c['root']) / 'manifests/selection.json').get('rules')
    if rules is None:
        # Older sparse-product manifests specify dates without an expansion rule.
        return 'Explicit dates in the frozen selection manifest; expansion unspecified'
    before = rules['margin_days']
    after = rules.get('margin_after_days', before)
    return ('Union of observed central-96x96 VIIRS fire dates: '
            f'[fire day - {before} days, fire day + {after} days], inclusive; clipped to study period')


def validate_final(c, region, tasks, selection=None):
    tasks, dates, ids, episodes, selection_sha = _selection_contract(c, region, tasks, selection)
    path, receipt_path = _final_paths(c, region)
    root = Path(c['root'])
    _owned_path(path, root)
    receipt = _read(_owned_path(receipt_path, root))
    receipts = receipt.get('chunk_receipts', [])
    if (receipt.get('validated') is not True or receipt.get('path') != str(path)
            or receipt.get('region_id') != region['region_id'] or receipt.get('days') != len(dates)
            or receipt.get('chunks') != len(tasks) or receipt.get('episodes') != episodes
            or receipt.get('selection_contract_sha256') != selection_sha
            or receipt.get('all_merged_variables_verified') is not True
            or [r.get('key') for r in receipts] != list(tasks)
            or sum(r.get('days', 0) for r in receipts) != len(dates)
            or receipt.get('chunk_receipts_sha256') != _digest(receipts)):
        raise ValueError('Final event product receipt is inconsistent')
    for item in receipts:
        key = _name(item['key'])
        if item.get('validated') is not True or item.get('path') != str(root / 'monthly' / region['region_id'] / (key + '.nc')):
            raise ValueError('Final receipt references unowned or unvalidated chunk')
    if receipt.get('deployment_fingerprint') != _read(root / 'manifests/deployment.json')['fingerprint']:
        raise ValueError('Final scientific deployment mismatch')
    if _sha(path) != receipt.get('sha256'):
        raise ValueError('Final event product checksum mismatch')
    with nc.Dataset(path) as ds:
        _validate_dataset(ds, c, region, dates)
        if (ds.getncattr('chunk_receipts_sha256') != receipt['chunk_receipts_sha256']
                or ds.getncattr('selection_contract_sha256') != selection_sha
                or ds['episode_id'].dimensions != ('time',)
                or not np.array_equal(ds['episode_id'][:], ids)):
            raise ValueError('Final episode metadata mismatch')
    if _sha(path) != receipt['sha256']:
        raise ValueError('Final product changed during validation')
    return receipt


def consolidate(c, region, tasks, selection=None):
    """Publish a sparse-date file; leave registered inputs for delayed cleanup."""
    import runtime
    path, receipt_path = _final_paths(c, region)
    root = Path(c['root'])
    with runtime.locked(root / 'locks/products' / (region['region_id'] + '.lock')):
        if receipt_path.exists():
            return validate_final(c, region, tasks, selection)
        tasks, dates, ids, episodes, selection_sha = _selection_contract(c, region, tasks, selection)
        receipts = [_read(root / 'manifests/tasks' / key / 'output.json') for key in tasks]
        for (key, task), receipt in zip(tasks.items(), receipts):
            validate_chunk(c, key, receipt, task)
        path.parent.mkdir(parents=True, exist_ok=True)
        _owned_path(path, root, must_exist=False)
        temporary = path.with_suffix('.partial.nc')
        _owned_path(temporary, root, must_exist=False)
        _stream_merge(c, region, receipts, temporary, dates, ids, episodes, selection_sha)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        with nc.Dataset(path) as ds:
            _validate_dataset(ds, c, region, dates)
        receipt = {'schema': 1, 'validated': True, 'path': str(path), 'sha256': _sha(path),
            'region_id': region['region_id'], 'days': len(dates), 'chunks': len(tasks),
            'date_start': str(np.datetime64(int(dates[0]), 's')),
            'date_end': str(np.datetime64(int(dates[-1]), 's')),
            'episodes': episodes, 'selection_contract_sha256': selection_sha,
            'deployment_fingerprint': _read(root / 'manifests/deployment.json')['fingerprint'],
            'chunk_receipts': receipts, 'chunk_receipts_sha256': _digest(receipts),
            'all_merged_variables_verified': True, 'completed': _now(), 'completed_epoch': time.time()}
        _atomic(receipt_path, receipt)
        return validate_final(c, region, tasks, selection)


def _cleanup_gate(queue, token, final, now_epoch):
    """Return pending gates without treating expected retention/lease waits as errors."""
    tasks = {key: task for key, task in queue['tasks'].items() if task.get('region') == token}
    if set(tasks) != {r['key'] for r in final['chunk_receipts']}:
        raise ValueError('Cleanup task set differs from final product')
    if queue.get('phase') not in {'CLEANUP_WAIT', 'CLEANED'} or queue.get('cleanup_authorized') is not True:
        return None, 'cleanup not authorized or not in cleanup phase'
    deadline = queue.get('cleanup_not_before_epoch')
    completed = final.get('completed_epoch')
    if not isinstance(deadline, (int, float)) or not isinstance(completed, (int, float)) or not np.isfinite([deadline, completed]).all():
        raise ValueError('Missing finite cleanup delay timestamps')
    if deadline < completed + 600:
        raise ValueError('Cleanup deadline violates required 600-second retention')
    if now_epoch < deadline:
        return None, '600-second post-publication retention has not elapsed'
    for key, task in tasks.items():
        if task.get('kind') != 'episode' or task.get('input_status') not in {'ready', 'cleaned'} or task.get('prediction_status') != 'done':
            raise ValueError('Cleanup task is not fully complete: ' + key)
        if any(task.get(name) is not None for name in ('lease', 'job', 'pid')):
            return None, 'active task lease: ' + key
    return tasks, None


def cleanup_region(c, token, tasks, selection=None):
    """Delete only this trial's registered inputs after final validation and locks.

    Chunk prediction files are retained (small and useful for audit).  Never touch
    full-decade inputs, other regions, source archives or shared indexes.
    """
    import runtime
    token = _name(token)
    root = Path(c['root'])
    region = _read(root / 'manifests/regions.json')[token]
    queue = runtime.Queue(c)
    with runtime.locked(root / 'locks/products' / (region['region_id'] + '.lock')):
        final = validate_final(c, region, tasks, selection)
        eligible, reason = _cleanup_gate(queue.snapshot(), token, final, time.time())
        if reason:
            return {'cleaned': False, 'pending': True, 'reason': reason, 'region': token}
        with contextlib.ExitStack() as locks:
            try:
                for key in sorted(eligible):
                    locks.enter_context(runtime.locked(root / 'locks/tasks' / (key + '.lock'), False))
            except BlockingIOError:
                return {'cleaned': False, 'pending': True, 'reason': 'active task lock', 'region': token}
            with queue.edit() as current:
                eligible, reason = _cleanup_gate(current, token, final, time.time())
                if reason:
                    return {'cleaned': False, 'pending': True, 'reason': reason, 'region': token}
            plans = []
            for key in sorted(eligible):
                registration = _registration(c, key)
                if registration['identity']['region'] != region['region_id']:
                    raise ValueError('Cleanup registration belongs to another cube')
                path = Path(c['scratch']) / 'inputs' / key
                _owned_path(path, Path(c['scratch']) / 'inputs', must_exist=False)
                if path.exists() and not path.is_dir():
                    raise ValueError('Registered input is not a directory')
                plans.append((key, registration, path))
            for key, registration, path in plans:
                meta = root / 'manifests/tasks' / key
                record = {'key': key, 'region': token, 'deleted_path': str(path),
                          'final_output_sha256': final['sha256'],
                          'registration_sha256': _digest(registration)}
                intent = meta / 'cleanup_intent.json'
                if intent.exists() and _read(intent) != record:
                    raise ValueError('Cleanup identity changed during resume')
                _atomic(intent, record)
                _owned_path(path, Path(c['scratch']) / 'inputs', must_exist=False)
                if path.exists():
                    shutil.rmtree(path)
                _atomic(meta / 'cleanup.json', dict(record, completed=_now()))
                with queue.edit() as current:
                    eligible, reason = _cleanup_gate(current, token, final, time.time())
                    if reason:
                        raise ValueError('Cleanup gate changed under task locks: ' + reason)
                    current['tasks'][key].update(input_status='cleaned', reserved=0,
                                                cleaned_at=_now(), updated=_now())
        return {'cleaned': True, 'region': token, 'chunks': len(plans), 'final_sha256': final['sha256']}


INPUT_ARRAYS = {'hist_weather', 'hist_quasi_static', 'static_land', 'hist_fire',
                'future_weather', 'hist_fire_any_48h', 'hist_fire_delta_norm_48h'}


def _full_references(c, region, full_roots):
    """Identity-check optional read-only full-decade comparison sources."""
    fingerprint = _read(Path(c['root']) / 'manifests/deployment.json')['fingerprint']
    token = _region_token(c, region)
    references = []
    for value in full_roots:
        root = Path(value)
        if not (root / 'manifests/regions.json').exists():
            continue
        old_regions = _read(root / 'manifests/regions.json')
        if token not in old_regions:
            continue
        old = old_regions[token]
        if any(old.get(name) != region.get(name) for name in ('region_id', 'lat', 'lon', 'cube_sha256')):
            raise ValueError('Full-decade reference grid/cube identity differs')
        if _read(root / 'manifests/deployment.json')['fingerprint'] != fingerprint:
            raise ValueError('Full-decade reference model/preprocessing identity differs')
        references.append(root)
    return token, references


def compare_full_input_hashes(c, region, tasks, full_roots):
    """Compare retained per-day seven-tensor hashes; missing references stay pending.

    Complete full-decade inputs may have been deleted already.  Their durable
    input_days.json contains the same original daily checksums and is sufficient
    for this exact-value check.  Store fingerprints differ by date selection and
    are intentionally not compared.
    """
    tasks = _tasks(c, region, tasks)
    token, references = _full_references(c, region, full_roots)
    full_cache = {}
    comparisons = []
    missing = []
    for key, task in tasks.items():
        event_path = Path(c['root']) / 'manifests/tasks' / key / 'input_days.json'
        event_days = _read(event_path)
        if set(event_days) != set(task['dates']):
            raise ValueError('Event input journal is incomplete or has unexpected dates')
        for date in task['dates']:
            event = event_days[date]
            if set(event['arrays']) != INPUT_ARRAYS:
                raise ValueError('Event input journal does not contain the seven tensors')
            candidates = []
            for root in references:
                old_path = root / 'manifests/tasks' / (token + '_' + date[:7]) / 'input_days.json'
                if old_path not in full_cache:
                    full_cache[old_path] = _read(old_path) if old_path.exists() else {}
                old = full_cache[old_path].get(date)
                if old is not None:
                    candidates.append((old_path, old))
            if not candidates:
                missing.append(date)
                continue
            for old_path, old in candidates:
                if set(old['arrays']) != INPUT_ARRAYS:
                    raise ValueError('Full-decade journal does not contain the seven tensors')
                mismatched = [name for name in sorted(INPUT_ARRAYS)
                              if event['arrays'][name]['sha256'] != old['arrays'][name]['sha256']]
                comparisons.append({'date': date, 'key': key, 'reference': str(old_path),
                                    'identical': not mismatched, 'mismatched_arrays': mismatched})
    failed = [item for item in comparisons if not item['identical']]
    report = {'region_id': region['region_id'], 'checked': _now(),
              'status': 'failed' if failed else 'pending' if missing else 'passed',
              'selected_days': sum(len(t['dates']) for t in tasks.values()),
              'compared_days': len({item['date'] for item in comparisons}),
              'pending_dates': missing, 'comparisons': comparisons,
              'reason': 'Reference journals may still be building; absence is not equality.' if missing else None}
    _atomic(Path(c['root']) / 'reports/comparison' / (token + '_full_input_hashes.json'), report)
    return report


def compare_full_predictions(c, region, tasks, full_roots, selection=None):
    """Report floating differences on selected days only, without forcing equality."""
    final = validate_final(c, region, tasks, selection)
    token, references = _full_references(c, region, full_roots)
    found = []
    for root in references:
        receipt_path = root / 'reports/regions' / (region['region_id'] + '.json')
        if receipt_path.exists():
            receipt = _read(receipt_path)
            path = Path(receipt.get('path', ''))
            if receipt.get('validated') is not True or not path.is_relative_to(root / 'outputs'):
                raise ValueError('Full-decade final receipt is unvalidated or outside its output root')
            _owned_path(path, root)
            if _sha(path) != receipt.get('sha256'):
                raise ValueError('Full-decade final result checksum mismatch')
            found.append((root, path, receipt))
    report = {'region_id': region['region_id'], 'checked': _now(),
              'event_sha256': final['sha256'], 'status': 'pending', 'comparisons': [],
              'interpretation': 'Same-date bfloat16 predictions can differ with batch composition; no bitwise-equivalence assertion.'}
    if not found:
        report['reason'] = 'No validated full-decade prediction file is available yet.'
    for root, path, receipt in found:
        with nc.Dataset(final['path']) as event, nc.Dataset(path) as old:
            event.set_auto_maskandscale(False)
            old.set_auto_maskandscale(False)
            for name in ('deployment_fingerprint', 'checkpoint_sha256', 'model_commit', 'region_id',
                         'precision', 'history_mode', 'input_protocol'):
                if event.getncattr(name) != old.getncattr(name):
                    raise ValueError('Full-decade prediction metadata differs: ' + name)
            for axis in ('lat', 'lon'):
                if not np.array_equal(event[axis][:], old[axis][:]):
                    raise ValueError('Full-decade prediction crop mismatch')
            times = np.asarray(old['time'][:])
            if times.ndim != 1 or not len(times) or np.any(np.diff(times) <= 0):
                raise ValueError('Full-decade time axis is not unique and sorted')
            target = np.asarray(event['time'][:])
            positions = np.searchsorted(times, target)
            if np.any(positions >= len(times)) or not np.array_equal(times[positions], target):
                raise ValueError('Full-decade prediction is missing selected dates')
            values = {}
            for name in ('wisp_eos_fire_score', 'query_points_yx', 'query_logits', 'query_confidence'):
                differences = []
                changed = 0
                count = 0
                for index, old_index in enumerate(positions):
                    left = np.asarray(event[name][index])
                    right = np.asarray(old[name][int(old_index)])
                    if left.shape != right.shape or not np.isfinite(left).all() or not np.isfinite(right).all():
                        raise ValueError('Invalid comparison arrays: ' + name)
                    delta = left.astype('f8') - right.astype('f8')
                    differences.append(delta.reshape(-1))
                    changed += int(np.count_nonzero(left != right))
                    count += left.size
                delta = np.concatenate(differences)
                values[name] = {'max_abs': float(np.abs(delta).max()),
                                'rmse': float(np.sqrt(np.mean(delta * delta))),
                                'absolute_error_quantiles': dict(zip(['p50', 'p95', 'p99'],
                                    [float(x) for x in np.quantile(np.abs(delta), [.5, .95, .99])])),
                                'identical_fraction': 1.0 - changed / count}
            for name in ('wisp_eos_fire_score', 'query_confidence'):
                changes = {str(threshold): 0 for threshold in (.5, .8)}
                for index, old_index in enumerate(positions):
                    left, right = np.asarray(event[name][index]), np.asarray(old[name][int(old_index)])
                    for threshold in (.5, .8):
                        changes[str(threshold)] += int(np.count_nonzero((left >= threshold) != (right >= threshold)))
                values[name]['threshold_decision_changes_ge'] = changes
            changed_points = 0
            for index, old_index in enumerate(positions):
                left = np.rint(np.asarray(event['query_points_yx'][index]) * 95).astype('i4')
                right = np.rint(np.asarray(old['query_points_yx'][int(old_index)]) * 95).astype('i4')
                changed_points += int(np.any(left != right, axis=-1).sum())
            report['comparisons'].append({'reference': str(path), 'sha256': receipt['sha256'],
                                         'days': len(target), 'metrics': values,
                                         'queries_with_changed_nearest_pixel': changed_points})
        if _sha(path) != receipt['sha256']:
            raise ValueError('Full-decade result changed during comparison')
    if found:
        report['status'] = 'compared'
    _atomic(Path(c['root']) / 'reports/comparison' / (token + '_full_predictions.json'), report)
    return report
