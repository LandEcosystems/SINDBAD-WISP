"""Offline, non-destructive migration into the cube-first operational root.

The frozen deployment and scientific code remain unchanged. Input stores remain
at their registered scratch paths; only metadata and validated products copy.
"""
import argparse
import collections
import copy
import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from quiesce import OLD_ROOT, OLD_OPS, NEW_ROOT, atomic, now, owned_jobs, read, sha

sys.dont_write_bytecode = True  # Importing frozen modules must not change their trees.

PILOT_MONTHS = ['2014-01', '2015-06', '2016-02', '2017-07', '2018-10', '2019-12',
                '2020-02', '2021-03', '2022-07', '2022-08', '2023-06', '2023-12']


def month_tasks(c, token):
    start = datetime.datetime.strptime(c['start'][:10], '%Y-%m-%d').date()
    end = datetime.datetime.strptime(c['end_exclusive'][:10], '%Y-%m-%d').date()
    cursor = start.replace(day=1)
    result = {}
    while cursor < end:
        following = (cursor.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        day, dates = max(start, cursor), []
        while day < min(end, following):
            dates.append(day.isoformat() + 'T00:00:00')
            day += datetime.timedelta(days=1)
        month = cursor.strftime('%Y-%m')
        result[token + '_' + month] = {'region': token, 'dates': dates, 'kind': 'month',
            'priority': -100 + PILOT_MONTHS.index(month) if month in PILOT_MONTHS else 10,
            'input_status': 'queued', 'prediction_status': 'queued', 'status': 'queued',
            'reserved': c['month_reservation_bytes'], 'lease': None, 'job': None, 'pid': None}
        cursor = following
    if len(result) != 120 or sum(len(t['dates']) for t in result.values()) != 3641:
        raise ValueError('Cube-first admission requires the frozen 120 months / 3641 days')
    return result


def allocated(paths):
    """Filesystem allocated blocks, not apparent uncompressed Zarr tensor sizes."""
    result = {}
    paths = [Path(path) for path in paths if Path(path).exists()]
    for offset in range(0, len(paths), 100):
        batch = paths[offset:offset + 100]
        if any(path.is_symlink() for path in batch):
            raise ValueError('Refuse symlink in scratch accounting')
        p = subprocess.run(['du', '-s', '-B1', '--'] + [str(path) for path in batch],
                           text=True, capture_output=True, check=True, timeout=300)
        for line in p.stdout.splitlines():
            count, path = line.split('\t', 1)
            result[path] = int(count)
    return result


def copy_file(source, target):
    source, target = Path(source), Path(target)
    if source.is_symlink() or target.is_symlink():
        raise ValueError('Refuse metadata/product symlink: ' + str(source))
    digest = sha(source)
    if target.exists():
        if sha(target) != digest:
            raise ValueError('Migration destination already differs: ' + str(target))
        return digest
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.migrate.%s.tmp' % os.getpid())
    shutil.copy2(source, temporary)
    if sha(temporary) != digest:
        raise ValueError('Copy checksum mismatch: ' + str(source))
    os.replace(temporary, target)
    return digest


def copy_metadata(old_root, new_root, audit):
    copied = {}
    for folder in ['manifests', 'reports', 'qa']:
        source = old_root / folder
        for path in sorted(source.rglob('*')):
            if path.is_symlink():
                raise ValueError('Unexpected metadata symlink: ' + str(path))
            if not path.is_file():
                continue
            relative = path.relative_to(old_root)
            if folder == 'manifests' and relative.parts[1:2] == ('tasks',):
                copy_file(path, audit / 'original_task_metadata' / Path(*relative.parts[2:]))
                # Output receipts are published only after old and new validation.
                if path.name == 'output.json':
                    continue
            copied[str(relative)] = copy_file(path, new_root / relative)
    atomic(audit / 'metadata_copies.json', copied)


def input_audit(c, key, task, sample=False):
    from wisp_batch.common import digest, SHAPES
    from wisp_batch.store import InputStore
    path = Path(c['scratch']) / 'inputs' / key
    meta = Path(c['root']) / 'manifests/tasks' / key
    report = {'path': str(path), 'present': path.exists(), 'ready': False, 'sampled_days': []}
    if not path.exists():
        return report
    if path.is_symlink():
        raise ValueError('Input store is a symlink: ' + key)
    reg = read(meta / 'registration.json')
    if reg['key'] != key or reg['scratch_path'] != str(path) or reg['identity']['dates'] != task['dates']:
        raise ValueError('Input registration differs: ' + key)
    if reg['identity']['deployment'] != read(Path(c['root']) / 'manifests/deployment.json')['fingerprint']:
        raise ValueError('Input belongs to another scientific deployment: ' + key)
    store = InputStore(path, identity=reg['identity'])
    if store.dates != task['dates'] or store.fingerprint != digest(reg['identity']):
        raise ValueError('InputStore identity differs: ' + key)
    if store.shapes != {name: tuple(shape) for name, shape in SHAPES.items()}:
        raise ValueError('InputStore tensor shapes differ: ' + key)
    for name, shape in SHAPES.items():
        if tuple(store.g[name].shape) != (len(task['dates']),) + tuple(shape) or str(store.g[name].dtype) != 'float32':
            raise ValueError('Input tensor metadata differs: ' + key + '/' + name)
    receipts, indexes = [], []
    for index, day in enumerate(task['dates']):
        if not store.has(index):
            continue
        receipt = read(store.receipt(index))
        if receipt['date'] != day or receipt['fingerprint'] != store.fingerprint or set(receipt['arrays']) != set(SHAPES):
            raise ValueError('Input day receipt identity differs: ' + key)
        if any(not isinstance(value.get('sha256'), str) or len(value['sha256']) != 64
               for value in receipt['arrays'].values()):
            raise ValueError('Input receipt checksum missing: ' + key)
        receipts.append(receipt)
        indexes.append(index)
    if sample and indexes:
        index = indexes[0]
        store.read(index)  # Actual decompression and receipt checksum verification.
        report['sampled_days'].append(task['dates'][index])
    success, complete = path / '_SUCCESS.json', meta / 'input_complete.json'
    if success.exists() and complete.exists():
        marker, done = read(success), read(complete)
        by_date = {receipt['date']: receipt for receipt in receipts}
        if (len(receipts) != len(task['dates']) or marker['fingerprint'] != store.fingerprint
                or marker['days'] != len(task['dates']) or marker['receipt_digest'] != digest(receipts)
                or not done.get('validated') or done['fingerprint'] != store.fingerprint
                or done['days'] != len(task['dates']) or done['receipt_digest'] != digest(by_date)):
            raise ValueError('Input completion receipt differs: ' + key)
        report['ready'] = True
    report.update(fingerprint=store.fingerprint, retained_daily_receipts=len(receipts),
                  receipts_digest=digest(receipts), all_arrays_verified=False)
    return report


def capacity(c, q, inputs_allocated, quarantine_allocated):
    active = q['active_cube']
    parked = 0
    for key, task in q['tasks'].items():
        actual = inputs_allocated.get(str(Path(c['scratch']) / 'inputs' / key), 0)
        task['allocated_input_bytes_at_migration'] = actual
        if task.get('kind') == 'month' and task.get('region') == active:
            task['reserved'] = max(c['month_reservation_bytes'], actual)
        else:
            task['reserved'] = actual
            parked += actual
    known_paths = {str(Path(c['scratch']) / 'inputs' / key) for key in q['tasks']}
    unknown = {path: count for path, count in inputs_allocated.items() if path not in known_paths}
    extra = sum(unknown.values())
    q['quarantine_bytes'] = quarantine_allocated + extra
    total = sum(t['reserved'] for t in q['tasks'].values()) + q['quarantine_bytes'] + c['shared_cache_reserve_bytes']
    report = {'active_reserved_bytes': sum(t['reserved'] for t in q['tasks'].values()
              if t.get('kind') == 'month' and t.get('region') == active),
              'parked_allocated_bytes': parked, 'quarantine_allocated_bytes': quarantine_allocated,
              'unregistered_input_allocated_bytes': extra, 'unregistered_inputs': unknown,
              'shared_cache_reserve_bytes': c['shared_cache_reserve_bytes'],
              'total_reserved_bytes': total, 'budget_bytes': c['scratch_budget_bytes'],
              'free_bytes': shutil.disk_usage(c['scratch']).free,
              'minimum_free_bytes': c['scratch_min_free_bytes'], 'checked': now()}
    report['passed'] = (total <= c['scratch_budget_bytes'] and
                        report['free_bytes'] >= c['scratch_min_free_bytes'])
    report['excess_bytes'] = max(0, total - c['scratch_budget_bytes'])
    q.update(parked_allocated_bytes=parked, capacity_passed=report['passed'],
             active_reserved_bytes=report['active_reserved_bytes'])
    return report


def restore_missing_tasks(q, root):
    restored = []
    for meta in sorted((root / 'manifests/tasks').iterdir()):
        registration = meta / 'registration.json'
        if not registration.is_file() or meta.name in q['tasks']:
            continue
        reg = read(registration)
        key = meta.name
        tokens = key.split('_')
        token = tokens[1] if key.startswith('v_') else tokens[0]
        q['tasks'][key] = {'region': token, 'dates': reg['identity']['dates'],
                           'kind': 'validation' if key.startswith('v_') else 'month', 'priority': 10}
        restored.append(key)
    return restored


def admit_cube(c, token):
    """Called only after the preceding cube is finished; atomically reserve its successor."""
    import runtime
    if token not in read(Path(c['root']) / 'manifests/regions.json'):
        raise ValueError('Cube is not enrolled: ' + token)
    queue = runtime.Queue(c)
    scratch = Path(c['scratch'])
    allocations = allocated(list((scratch / 'inputs').iterdir()))
    quarantine = allocated([scratch / 'quarantine']).get(str(scratch / 'quarantine'), 0)
    with queue.edit() as q:
        if any(task.get('lease') for task in q['tasks'].values()):
            raise RuntimeError('Cannot admit a cube while workers own tasks')
        if q.get('active_cube') != token and q.get('phase') not in {'CLEANED', 'COMPLETE'}:
            raise RuntimeError('Previous cube has not completed consolidation and cleanup')
        for key, new_task in month_tasks(c, token).items():
            q['tasks'].setdefault(key, new_task)
            q['tasks'][key]['priority'] = new_task['priority']
        q.update(active_cube=token, phase='BUILD', admitted_at=now())
        for name in ['cleanup_authorized', 'cleanup_not_before_epoch', 'final_receipt_sha256',
                     'inputs_verified_at', 'cleaned_at']:
            q.pop(name, None)
        report = capacity(c, q, allocations, quarantine)
        atomic(Path(c['root']) / 'reports/admission_capacity.json', report)
        if not report['passed']:
            raise RuntimeError('Admission exceeds scratch capacity; see admission_capacity.json')
    return report


def activate_next(c):
    """Advance in the frozen 400-cube order; await the next acceptance without skipping."""
    import runtime
    from wisp_stream.pipeline import discover
    root = Path(c['root'])
    order = read(root / 'manifests/cube_order.json')
    source = Path(c['batch_root']) / 'sampling/named_production_regions.json'
    if sha(source) != order['catalog_sha256']:
        raise ValueError('Frozen 400-cube catalog changed')
    queue = runtime.Queue(c)
    with queue.edit() as q:
        if q['phase'] != 'CLEANED':
            raise RuntimeError('Next cube requires completed cleanup')
        completed = q.setdefault('completed_cubes', [])
        if q['active_cube'] not in completed:
            completed.append(q['active_cube'])
        pending = [token for token in order['tokens'] if token not in completed]
        if not pending:
            if len(completed) != 400:
                raise ValueError('Completion does not cover exactly 400 cubes')
            q.update(phase='COMPLETE', completed_at=now())
            return {'complete': True, 'completed_cubes': 400}
        token = pending[0]
    # Frozen discover verifies accepted source cube bytes and adds new QA metadata.
    regions = discover(c)
    if token not in regions:
        report = {'waiting_for_cube': token, 'completed_cubes': completed,
                  'phase': 'CLEANED', 'reason': 'next cube lacks validated acceptance', 'at': now()}
        atomic(root / 'reports/awaiting_next_cube.json', report)
        return report
    result = admit_cube(c, token)
    atomic(root / 'reports/next_cube_admitted.json', {'token': token, 'at': now(), 'capacity': result})
    return result


def migrate(old_root, old_ops, new_root, active_cube='T000830', sample_stores=3):
    old_root, old_ops, new_root = Path(old_root), Path(old_ops), Path(new_root)
    audit = new_root / 'code/migration'
    complete = audit / 'complete.json'
    if complete.exists():
        result = read(complete)
        if result['old_root'] != str(old_root) or result['new_root'] != str(new_root):
            raise ValueError('Completed migration belongs to another deployment')
        print(json.dumps({'already_complete': True, 'epoch': result['epoch']}), flush=True)
        return result  # Never reset queues, receipts, or work from a completed migration.
    if old_root.resolve() == new_root.resolve():
        raise ValueError('Migration requires a separate operational root')
    if owned_jobs(old_root, old_ops):
        raise RuntimeError('Old WISP jobs are still active')
    stop = read(audit / 'quiesce/complete.json')
    if not stop.get('passed') or stop['old_root'] != str(old_root) or stop['old_ops'] != str(old_ops):
        raise ValueError('A completed stop audit is required')
    old_config = old_root / 'config/deployment.json'
    original = read(old_config)
    if original['root'] != str(old_root):
        raise ValueError('Frozen configuration root differs')
    c = dict(original, root=str(new_root), config_path=str(old_config))
    sys.path.insert(0, c['app'])
    spec = importlib.util.spec_from_file_location('migration_old_control64', old_ops / 'control64.py')
    old_control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old_control)
    from products import validate_month
    from wisp_batch.common import digest
    old_queue_path = old_root / 'state/queue.json'
    old_queue_hash = sha(old_queue_path)
    epoch_path = audit / 'epoch.json'
    epoch = read(epoch_path) if epoch_path.exists() else {'epoch': uuid.uuid4().hex,
        'started': now(), 'old_root': str(old_root), 'new_root': str(new_root),
        'old_queue_sha256': old_queue_hash, 'old_config_sha256': sha(old_config)}
    if epoch['old_queue_sha256'] != old_queue_hash or epoch['old_config_sha256'] != sha(old_config):
        raise ValueError('Old deployment changed during migration')
    atomic(epoch_path, epoch)
    old_queue = read(old_queue_path)
    atomic(audit / 'queue_before.json', old_queue)
    copy_file(old_config, new_root / 'config/frozen_deployment.json')
    atomic(new_root / 'config/deployment.json', dict(original, root=str(new_root)))
    copy_metadata(old_root, new_root, audit)
    # Reuse known incompatible-host exclusions; every new CPU job still reruns
    # the frozen numerical probe before claiming any month.
    for path in (old_root / 'state/backend').glob('*.json'):
        copy_file(path, new_root / 'state/backend' / path.name)
    if read(new_root / 'manifests/deployment.json') != read(old_root / 'manifests/deployment.json'):
        raise ValueError('Scientific identity must remain byte-for-byte unchanged')
    catalog = Path(c['batch_root']) / 'sampling/named_production_regions.json'
    tokens = sorted('T%06d' % region['tile_id'] for region in read(catalog)['regions'])
    if len(tokens) != 400 or len(set(tokens)) != 400 or active_cube not in tokens:
        raise ValueError('Expected frozen catalog of exactly 400 distinct cubes')
    copy_file(catalog, new_root / 'manifests/named_production_regions.json')
    atomic(new_root / 'manifests/cube_order.json', {'tokens': [active_cube] + [t for t in tokens if t != active_cube],
           'catalog_sha256': sha(catalog), 'rule': 'T000830 first, then stable numeric tile order; await acceptance without skipping'})
    q = copy.deepcopy(old_queue)
    restored = restore_missing_tasks(q, new_root)
    if active_cube not in read(new_root / 'manifests/regions.json'):
        raise ValueError('Active cube is not enrolled: ' + active_cube)
    for key, new_task in month_tasks(c, active_cube).items():
        if key in q['tasks'] and q['tasks'][key]['dates'] != new_task['dates']:
            raise ValueError('Existing task dates differ: ' + key)
        q['tasks'].setdefault(key, new_task)
        q['tasks'][key]['priority'] = new_task['priority']
    q.update(active_cube=active_cube, phase='BUILD', migration_epoch=epoch['epoch'],
             operations_release=str(new_root / 'code'), lock_protocol='POSIX_lockf_cube_first_v1',
             completed_cubes=[])
    input_reports, output_reports = {}, {}
    sampled = 0
    for index, (key, task) in enumerate(sorted(q['tasks'].items(), key=lambda kv: (kv[1].get('region') != active_cube, kv[0]))):
        if not key or Path(key).name != key or key in {'.', '..'}:
            raise ValueError('Unsafe ledger key: ' + key)
        report = input_audit(c, key, task, sample=sampled < sample_stores)
        if report.get('sampled_days'):
            sampled += 1
        input_reports[key] = report
        meta = new_root / 'manifests/tasks' / key
        old_meta = old_root / 'manifests/tasks' / key
        task.update(input_status='ready' if report['ready'] else 'queued', prediction_status='queued',
                    lease=None, job=None, pid=None, host=None, stage=None, cleanup_pending=False,
                    updated=now(), migration_epoch=epoch['epoch'])
        if report.get('fingerprint'):
            task['input_fingerprint'] = report['fingerprint']
        old_receipt = old_meta / 'output.json'
        if old_receipt.exists():
            receipt = read(old_receipt)
            old_validation = old_control.validate_output(original, key, receipt)
            reg = read(old_meta / 'registration.json')
            if receipt['input_fingerprint'] != digest(reg['identity']):
                raise ValueError('Output input fingerprint differs from registration: ' + key)
            source = Path(receipt['path'])
            relative = source.relative_to(old_root)
            if relative.parts[0] != 'monthly':
                raise ValueError('Output outside old monthly directory: ' + key)
            destination = new_root / relative
            copy_file(source, destination)
            migrated_receipt = dict(receipt, path=str(destination))
            atomic(meta / 'output.json', migrated_receipt)
            new_validation = validate_month(c, key, migrated_receipt)
            output_reports[key] = {'old': old_validation, 'new': new_validation}
            task.update(prediction_status='done', output_sha256=receipt['sha256'],
                        output_input_fingerprint=receipt['input_fingerprint'])
        else:
            # Retain resumable partial predictions and their daily journal together.
            registration = old_meta / 'registration.json'
            if registration.exists():
                region = read(registration)['identity']['region']
                partial = old_root / 'monthly' / region / (key + '.partial.nc')
                if partial.exists():
                    copy_file(partial, new_root / partial.relative_to(old_root))
                else:
                    # A crash can land between final rename and output receipt.
                    # Retain those bytes as unpublished; frozen MonthOutput.has
                    # must verify its journal before it can publish a receipt.
                    unpublished = old_root / 'monthly' / region / (key + '.nc')
                    if unpublished.exists():
                        destination = new_root / 'monthly' / region / (key + '.partial.nc')
                        copy_file(unpublished, destination)
                        task['retained_unpublished_output'] = str(destination)
        task['status'] = ('done' if task['prediction_status'] == 'done' and task['input_status'] == 'ready'
                          else 'ready' if task['input_status'] == 'ready' else 'queued')
        if index % 25 == 0:
            print(json.dumps({'audited_tasks': index + 1, 'total': len(q['tasks']), 'at': now()}), flush=True)
    atomic(audit / 'retained_inputs.json', input_reports)
    atomic(audit / 'validated_outputs.json', output_reports)
    scratch = Path(c['scratch'])
    allocations = allocated(list((scratch / 'inputs').iterdir()))
    quarantine = allocated([scratch / 'quarantine']).get(str(scratch / 'quarantine'), 0)
    report = capacity(c, q, allocations, quarantine)
    atomic(audit / 'scratch_capacity.json', report)
    atomic(audit / 'parked_allocated.json', allocations)
    if not report['passed']:
        print(json.dumps({'capacity_blocked_before_claim': True, **report}), flush=True)
        raise RuntimeError('960 GiB cube reservation plus parked inputs/quarantine/cache exceeds capacity; no claim allowed and nothing deleted')
    # Reconcile previously published source inventories without changing old files.
    sources = read(new_root / 'manifests/source_files.json')
    for path in (new_root / 'manifests/tasks').glob('*/source_files.json'):
        for name, value in read(path).items():
            if name in sources and sources[name] != value:
                raise ValueError('Frozen source inventory disagreement: ' + name)
            sources[name] = value
    atomic(new_root / 'manifests/source_files.json', sources)
    if owned_jobs(old_root, old_ops) or sha(old_queue_path) != old_queue_hash:
        raise RuntimeError('Old run changed before migration commit')
    if (new_root / 'state/queue.json').exists():
        existing = read(new_root / 'state/queue.json')
        if existing != q and existing.get('migration_epoch') != epoch['epoch']:
            raise RuntimeError('New queue already contains unrelated work')
        if any(t.get('lease') or t.get('job') for t in existing['tasks'].values()):
            raise RuntimeError('New queue has live owners; refuse migration reset')
    atomic(new_root / 'state/queue.json', q)
    atomic(audit / 'queue_after.json', q)
    result = {**epoch, 'passed': True, 'completed': now(), 'active_cube': active_cube,
              'phase': 'BUILD', 'tasks': len(q['tasks']), 'restored_keys': restored,
              'input_status_counts': dict(collections.Counter(t['input_status'] for t in q['tasks'].values())),
              'prediction_status_counts': dict(collections.Counter(t['prediction_status'] for t in q['tasks'].values())),
              'validated_outputs': len(output_reports), 'sampled_input_stores': sampled,
              'input_validation': 'all receipt identities and metadata; bounded decompressed day samples; full active cube verification required before prediction',
              'capacity': report, 'queue_after_sha256': sha(new_root / 'state/queue.json'),
              'scientific_deployment_sha256': sha(new_root / 'manifests/deployment.json')}
    atomic(complete, result)
    print(json.dumps(result, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-root', type=Path, default=OLD_ROOT)
    parser.add_argument('--old-ops', type=Path, default=OLD_OPS)
    parser.add_argument('--new-root', type=Path, default=NEW_ROOT)
    parser.add_argument('--active-cube', default='T000830')
    parser.add_argument('--sample-stores', type=int, default=3)
    args = parser.parse_args()
    lock = args.new_root / 'code/migration/migration.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+') as stream:
        fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        migrate(args.old_root, args.old_ops, args.new_root, args.active_cube, args.sample_stores)


if __name__ == '__main__':
    main()
