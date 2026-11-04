"""Durable cube products and explicitly gated cleanup for cube-first operations.

This module does not change the frozen scientific writer or its identity.  It
merges its monthly files and keeps every source month until cleanup is permitted.
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
TOTAL_DAYS = 3641
MONTHS = 120
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


def expected_dates(c):
    start = np.datetime64(c['start'], 's')
    end = np.datetime64(c['end_exclusive'], 's')
    if np.isnat(start) or np.isnat(end) or int(start.astype('int64')) % DAY or int(end.astype('int64')) % DAY:
        raise ValueError('Expected dates must be finite midnight boundaries')
    dates = np.arange(start, end, np.timedelta64(1, 'D')).astype('int64')
    frozen = np.arange(np.datetime64('2014-01-12', 's'), np.datetime64('2024-01-01', 's'),
                       np.timedelta64(1, 'D')).astype('int64')
    if len(dates) != TOTAL_DAYS or not np.array_equal(dates, frozen):
        raise ValueError('Expected full cube axis is 2014-01-12 through 2023-12-31 (3641 days)')
    return dates


def validate_date_axis(actual, expected):
    actual, expected = np.asarray(actual), np.asarray(expected)
    if actual.ndim != 1 or expected.ndim != 1 or not len(expected):
        raise ValueError('Invalid empty or multidimensional time axis')
    if actual.dtype.kind not in 'iu' or not np.array_equal(actual, expected):
        raise ValueError('Output time mismatch: gaps, duplicates, order, or dates differ')
    if np.any(expected % DAY) or np.any(np.diff(expected) != DAY):
        raise ValueError('Expected time axis is not consecutive UTC midnights')


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


def validate_month(c, key, receipt):
    """Verify a registered, immutable monthly result under the new run root."""
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
    validate_date_axis(dates, dates)
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


def _final_paths(c, region):
    region_id = _name(region['region_id'])
    root = Path(c['root'])
    return (root / 'outputs' / region_id / ('wisp_eos_q10_' + region_id + '_20140112_20231231.nc'),
            root / 'reports/regions' / (region_id + '.json'))


def _validate_month_sequence(c, region, task_keys, receipts):
    if len(task_keys) != MONTHS or len(set(task_keys)) != MONTHS or len(receipts) != MONTHS:
        raise ValueError('Exactly 120 unique validated months are required')
    dates = []
    months = []
    for key, receipt in zip(task_keys, receipts):
        if receipt['key'] != key:
            raise ValueError('Monthly receipt key mismatch')
        identity = _registration(c, key)['identity']
        if identity['region'] != region['region_id'] or any(identity[a] != region[a] for a in ('lat', 'lon')):
            raise ValueError('Monthly registration belongs to a different region')
        axis = np.asarray(identity['dates'], dtype='datetime64[s]')
        month = axis.astype('datetime64[M]')
        if not len(axis) or not np.all(month == month[0]):
            raise ValueError('Monthly dates span more than one month')
        months.append(str(month[0]))
        dates.extend(axis.astype('int64').tolist())
    if len(set(months)) != MONTHS:
        raise ValueError('Duplicate monthly calendar interval')
    validate_date_axis(np.asarray(dates, dtype='i8'), expected_dates(c))


def _equal_arrays(left, right):
    a, b = np.asarray(left), np.asarray(right)
    return a.dtype == b.dtype and a.shape == b.shape and a.tobytes() == b.tobytes()


def _compare_source(out, src, offset):
    if set(out.variables) != set(src.variables) or set(out.dimensions) != set(src.dimensions):
        raise ValueError('Monthly schema mismatch')
    n = len(src.dimensions['time'])
    for name, var in src.variables.items():
        dest = out[name]
        if dest.dimensions != var.dimensions or dest.dtype != var.dtype:
            raise ValueError('Monthly variable schema differs: ' + name)
        if 'time' in var.dimensions:
            index = [slice(None)] * var.ndim
            index[var.dimensions.index('time')] = slice(offset, offset + n)
            merged = dest[tuple(index)]
        else:
            merged = dest[...]
        if not _equal_arrays(merged, var[...]):
            raise ValueError('Merged values differ from source: ' + name)


def _stream_merge(c, region, receipts, temporary):
    """Copy and read back every variable, then compare again after close/reopen."""
    pos = 0
    with nc.Dataset(temporary, 'w') as out:
        out.set_auto_maskandscale(False)
        for number, receipt in enumerate(receipts):
            with nc.Dataset(receipt['path']) as src:
                src.set_auto_maskandscale(False)
                if number == 0:
                    for name, dim in src.dimensions.items():
                        out.createDimension(name, TOTAL_DAYS if name == 'time' else len(dim))
                    out.setncatts({k: src.getncattr(k) for k in src.ncattrs()
                                   if k not in {'fingerprint', 'input_fingerprint', 'hardware'}})
                    out.setncatts({'monthly_receipts_sha256': _digest(receipts),
                                   'hardware': ','.join(sorted({x['hardware'] for x in receipts}))})
                    for name, var in src.variables.items():
                        fill = var.getncattr('_FillValue') if '_FillValue' in var.ncattrs() else False
                        target = out.createVariable(name, var.dtype, var.dimensions,
                                                    zlib=True, complevel=3, fill_value=fill)
                        target.setncatts({k: var.getncattr(k) for k in var.ncattrs() if k != '_FillValue'})
                        target.set_auto_maskandscale(False)
                if set(src.variables) != set(out.variables) or set(src.dimensions) != set(out.dimensions):
                    raise ValueError('Monthly schema mismatch')
                n = len(src.dimensions['time'])
                for name, var in src.variables.items():
                    if var.dimensions != out[name].dimensions or var.dtype != out[name].dtype:
                        raise ValueError('Monthly variable schema differs: ' + name)
                    if 'time' in var.dimensions:
                        index = [slice(None)] * var.ndim
                        index[var.dimensions.index('time')] = slice(pos, pos + n)
                        out[name][tuple(index)] = var[...]
                    elif number == 0:
                        out[name][...] = var[...]
                _compare_source(out, src, pos)
                pos += n
        if pos != TOTAL_DAYS:
            raise ValueError('Merged date axis incomplete')
        out.sync()
    with nc.Dataset(temporary) as out:
        _validate_dataset(out, c, region, expected_dates(c))
        pos = 0
        for receipt in receipts:
            if _sha(receipt['path']) != receipt['sha256']:
                raise ValueError('Monthly source changed during merge')
            with nc.Dataset(receipt['path']) as src:
                src.set_auto_maskandscale(False)
                _compare_source(out, src, pos)
                pos += len(src.dimensions['time'])
    with Path(temporary).open('rb') as f:
        os.fsync(f.fileno())


def validate_final(c, region):
    """Reopen and fully verify the final file, independent of deleted months."""
    path, receipt_path = _final_paths(c, region)
    root = Path(c['root'])
    _owned_path(path, root)
    _owned_path(receipt_path, root)
    receipt = _read(receipt_path)
    receipts = receipt.get('monthly_receipts', [])
    if (receipt.get('validated') is not True or receipt.get('path') != str(path)
            or receipt.get('region_id') != region['region_id'] or receipt.get('days') != TOTAL_DAYS
            or receipt.get('months') != MONTHS or len(receipts) != MONTHS
            or receipt.get('all_merged_variables_verified') is not True
            or len({r['key'] for r in receipts}) != MONTHS
            or sum(r['days'] for r in receipts) != TOTAL_DAYS
            or receipt.get('monthly_receipts_sha256') != _digest(receipts)):
        raise ValueError('Final product receipt is inconsistent')
    for item in receipts:
        key = _name(item['key'])
        if item.get('validated') is not True or item.get('path') != str(root / 'monthly' / region['region_id'] / (key + '.nc')):
            raise ValueError('Final receipt references an unowned or unvalidated source month')
    _validate_month_sequence(c, region, [item['key'] for item in receipts], receipts)
    dates = expected_dates(c)
    if receipt.get('date_start') != '2014-01-12T00:00:00' or receipt.get('date_end') != '2023-12-31T00:00:00':
        raise ValueError('Final receipt date interval is incorrect')
    if receipt.get('deployment_fingerprint') != _read(root / 'manifests/deployment.json')['fingerprint']:
        raise ValueError('Final receipt scientific deployment mismatch')
    if _sha(path) != receipt.get('sha256'):
        raise ValueError('Final product checksum mismatch')
    with nc.Dataset(path) as ds:
        _validate_dataset(ds, c, region, dates)
        if ds.getncattr('monthly_receipts_sha256') != receipt['monthly_receipts_sha256']:
            raise ValueError('Final monthly manifest mismatch')
    if _sha(path) != receipt['sha256']:
        raise ValueError('Final product changed during validation')
    return receipt


def consolidate(c, region, task_keys):
    """Atomically publish a fully checked cube, retaining all monthly files."""
    import runtime
    path, receipt_path = _final_paths(c, region)
    root = Path(c['root'])
    with runtime.locked(root / 'locks/products' / (region['region_id'] + '.lock')):
        if receipt_path.exists():
            return validate_final(c, region)
        keys = sorted(task_keys)
        receipts = [_read(root / 'manifests/tasks' / _name(k) / 'output.json') for k in keys]
        for key, receipt in zip(keys, receipts):
            validate_month(c, key, receipt)
        _validate_month_sequence(c, region, keys, receipts)
        path.parent.mkdir(parents=True, exist_ok=True)
        _owned_path(path, root, must_exist=False)
        temporary = path.with_suffix('.partial.nc')
        _owned_path(temporary, root, must_exist=False)
        _stream_merge(c, region, receipts, temporary)
        os.replace(temporary, path)
        # Reopen the published name before the final, atomic receipt is written.
        with nc.Dataset(path) as ds:
            _validate_dataset(ds, c, region, expected_dates(c))
        receipt = {'schema': 1, 'validated': True, 'path': str(path), 'sha256': _sha(path),
                   'region_id': region['region_id'], 'days': TOTAL_DAYS, 'months': MONTHS,
                   'date_start': '2014-01-12T00:00:00', 'date_end': '2023-12-31T00:00:00',
                   'deployment_fingerprint': _read(root / 'manifests/deployment.json')['fingerprint'],
                   'monthly_receipts': receipts, 'monthly_receipts_sha256': _digest(receipts),
                   'all_merged_variables_verified': True, 'completed': _now(),
                   'completed_epoch': time.time()}
        _atomic(receipt_path, receipt)
        return validate_final(c, region)


def _cleanup_gate(q, token, final, now_epoch):
    if q.get('active_cube') != token or q.get('phase') != 'CLEANUP_WAIT':
        raise ValueError('Cube is not in CLEANUP_WAIT')
    if q.get('cleanup_authorized') is not True:
        raise ValueError('Cube cleanup has not been authorized')
    deadline = q.get('cleanup_not_before_epoch')
    completed = final.get('completed_epoch')
    if not isinstance(deadline, (int, float)) or not isinstance(completed, (int, float)) or not np.isfinite([deadline, completed]).all():
        raise ValueError('Missing finite cleanup delay timestamps')
    if deadline < completed + 600 or now_epoch < deadline:
        raise ValueError('The 600-second final-product retention window has not elapsed')
    tasks = {k: t for k, t in q['tasks'].items() if t.get('region') == token and t.get('kind', 'month') == 'month'}
    if len(tasks) != MONTHS or set(tasks) != {r['key'] for r in final['monthly_receipts']}:
        raise ValueError('Cleanup requires exactly the 120 final-product tasks')
    for key, task in tasks.items():
        if task.get('input_status') not in {'ready', 'cleaned'} or task.get('prediction_status') != 'done':
            raise ValueError('Cleanup task is not fully complete: ' + key)
        if any(task.get(name) is not None for name in ('lease', 'job', 'pid')):
            raise ValueError('Cleanup task still has a worker lease: ' + key)
    return tasks


def cleanup_cube(c, token):
    """Delete only registered inputs and this root's monthly products, after delay.

    The task locks cover validation, deletion, and state publication.  Queue locks
    are held only for state reads/writes, in the same task-before-queue order as
    workers.  Cleanup progress is recorded for safe retry after interruption.
    """
    import runtime
    token = _name(token)
    root = Path(c['root'])
    region = _read(root / 'manifests/regions.json')[token]
    queue = runtime.Queue(c)
    with runtime.locked(root / 'locks/products' / (region['region_id'] + '.lock')):
        final = validate_final(c, region)
        snapshot = queue.snapshot()
        if snapshot.get('active_cube') == token and snapshot.get('phase') == 'CLEANED':
            return {'cleaned': True, 'region': token, 'final_sha256': final['sha256']}
        tasks = _cleanup_gate(snapshot, token, final, time.time())
        with contextlib.ExitStack() as locks:
            for key in sorted(tasks):
                locks.enter_context(runtime.locked(root / 'locks/tasks' / (key + '.lock'), False))
            with queue.edit() as q:
                _cleanup_gate(q, token, final, time.time())
                receipt_path = _final_paths(c, region)[1]
                if q.get('final_receipt_sha256') and q['final_receipt_sha256'] != _sha(receipt_path):
                    raise ValueError('Cleanup final receipt changed since authorization')
            # Resolve and check every removal target before deleting any target.
            plans = []
            for receipt in final['monthly_receipts']:
                key = _name(receipt['key'])
                registration = _registration(c, key)
                if registration['identity']['region'] != region['region_id']:
                    raise ValueError('Cleanup registration belongs to another cube')
                input_path = Path(c['scratch']) / 'inputs' / key
                _owned_path(input_path, Path(c['scratch']) / 'inputs', must_exist=False)
                if input_path.exists() and not input_path.is_dir():
                    raise ValueError('Registered input is not a directory')
                monthly = root / 'monthly' / region['region_id'] / (key + '.nc')
                if receipt['path'] != str(monthly):
                    raise ValueError('Cleanup monthly output is not owned by this run')
                _owned_path(monthly, root, must_exist=False)
                if monthly.exists() and _sha(monthly) != receipt['sha256']:
                    raise ValueError('Monthly output changed before cleanup')
                plans.append((key, receipt, registration, input_path, monthly))
            for key, receipt, registration, input_path, monthly in plans:
                meta = root / 'manifests/tasks' / key
                record = {'key': key, 'region': token, 'deleted_path': str(input_path),
                          'deleted_monthly_path': str(monthly), 'output_sha256': receipt['sha256'],
                          'final_output_sha256': final['sha256'],
                          'registration_sha256': _digest(registration)}
                intent_path = meta / 'cleanup_intent.json'
                if intent_path.exists() and _read(intent_path) != record:
                    raise ValueError('Cleanup identity changed during resume')
                _atomic(intent_path, record)
                # Recheck paths immediately before destructive operations.
                _owned_path(input_path, Path(c['scratch']) / 'inputs', must_exist=False)
                if input_path.exists():
                    shutil.rmtree(input_path)
                _owned_path(monthly, root, must_exist=False)
                if monthly.exists():
                    monthly.unlink()
                completed = _now()
                _atomic(meta / 'cleanup.json', dict(record, completed=completed))
                with queue.edit() as q:
                    _cleanup_gate(q, token, final, time.time())
                    q['tasks'][key].update(input_status='cleaned', reserved=0,
                                           cleaned_at=completed, updated=completed)
            with queue.edit() as q:
                current = _cleanup_gate(q, token, final, time.time())
                if not all(t['input_status'] == 'cleaned' for t in current.values()):
                    raise ValueError('Cleanup did not complete every month')
                q.update(phase='CLEANED', cleaned_at=_now())
        return {'cleaned': True, 'region': token, 'months': MONTHS, 'final_sha256': final['sha256']}
