"""60 jobs x 2 month producers, one complete cube at a time."""
import argparse
from collections import Counter
import datetime
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import runtime as rt
from wisp_batch.common import atomic_json, digest, file_hash

CPU_CAP = 60
PILOT_CAP = 6
TOTAL_CAP = 120
PREFIX = 'wisp1c-'


def jobs(c):
    rows = subprocess.run(['squeue', '-h', '-r', '-u', os.environ.get('USER', 'ybai'), '-o', '%i|%j|%T'],
                          check=True, text=True, capture_output=True, timeout=30).stdout
    owned = []
    for row in rows.splitlines():
        jid, name, state = row.split('|')
        if name.startswith('wispb1-'):
            raise RuntimeError('Old WISP worker/controller still active: ' + jid)
        if not name.startswith(PREFIX):
            continue
        reg = Path(c['root']) / 'state/submissions' / (jid + '.json')
        if not reg.exists():
            raise RuntimeError('Unregistered cube-first job: ' + jid)
        r = rt.read_json(reg)
        if r['job'] != jid or r['name'] != name:
            raise ValueError('Job registry mismatch')
        owned.append({'id': jid, 'kind': r['kind'], 'name': name, 'state': state})
    return owned


def submit(c, kind, dependency=None):
    current = jobs(c)
    if len(current) >= TOTAL_CAP:
        raise RuntimeError('120 queued/running WISP job limit reached')
    if kind == 'cpu' and sum(j['kind'] == 'cpu' for j in current) >= CPU_CAP:
        raise RuntimeError('60 CPU job limit reached')
    cpu, mem, part, wall = {
        'control': (1, '2G', 'work,small,gpu', '12:00:00'),
        'cpu': (4, '16G', 'work,small,gpu', '24:00:00'),
        'verify': (4, '16G', 'work,small,gpu', '24:00:00'),
        'gpu': (8, '64G', 'gpu', '24:00:00'),
        'merge': (2, '8G', 'work,small,gpu', '12:00:00'),
        'cleanup': (1, '4G', 'work,small,gpu', '12:00:00'),
    }[kind]
    root = Path(c['root'])
    name = PREFIX + kind
    args = ['sbatch', '--parsable', '--account=science', '--qos=normal', '--job-name=' + name,
            '--partition=' + part, '--cpus-per-task=' + str(cpu), '--mem=' + mem, '--time=' + wall,
            '--chdir=' + str(root), '--output=' + str(root / 'logs' / (kind + '_%j.out')),
            '--error=' + str(root / 'logs' / (kind + '_%j.err'))]
    if kind == 'gpu':
        args += ['--gres=gpu:A100:1']
    if dependency:
        args += ['--dependency=afterany:' + str(dependency)]
    if kind == 'cpu':
        rejected = [p.stem for p in (root / 'state/backend').glob('*.json') if not rt.read_json(p)['passed']]
        if rejected:
            args += ['--exclude=' + ','.join(sorted(rejected))]
    args += [str(rt.HERE / 'job.sh'), kind]
    result = subprocess.run(args, check=True, text=True, capture_output=True, timeout=45)
    jid = result.stdout.strip().split(';')[0]
    if not jid.isdigit():
        raise ValueError('Unexpected sbatch result')
    atomic_json(root / 'state/submissions' / (jid + '.json'),
                {'job': jid, 'name': name, 'kind': kind, 'args': args, 'at': rt.now(),
                 'release_sha256': file_hash(rt.HERE / 'release.json')})
    print('SUBMITTED', jid, kind, flush=True)
    return jid


def pilot_gate(c):
    root = Path(c['root'])
    result_path = root / 'reports/pilot_passed.json'
    if result_path.exists():
        result = rt.read_json(result_path)
        if not result.get('passed') or result['release_sha256'] != file_hash(rt.HERE / 'release.json'):
            raise ValueError('Pilot gate belongs to another code release')
        return True
    reports = [rt.read_json(p) for p in (root / 'state/job_resources').glob('*.json')]
    registrations = [rt.read_json(p) for p in (root / 'state/submissions').glob('*.json')]
    qualified_jobs = []
    for r in registrations:
        qp = root / 'reports/qualification' / (r['job'] + '.json')
        if r['kind'] == 'cpu' and qp.exists() and rt.read_json(qp).get('passed'):
            qualified_jobs.append(r)
    pilot_jobs = sorted(qualified_jobs, key=lambda r: r['at'])[:6]
    if len(pilot_jobs) < 6:
        return False
    ids = {r['job'] for r in pilot_jobs}
    resource = {r['job']: r for r in reports if r['job'] in ids}
    if any(r.get('max_aggregate_rss_bytes', 0) > 12 * 2**30 or r.get('resource_error') for r in resource.values()):
        raise ValueError('Pilot resource gate failed; no expansion authorized')
    workers = [rt.read_json(p) for p in (root / 'state/workers').glob('cpu_*.json')]
    qualified = []
    for jid in ids:
        members = [r for r in workers if r.get('job') == jid and len(r.get('fresh_days', [])) >= 3]
        if len(members) < 2 or jid not in resource or resource[jid].get('max_aggregate_rss_bytes', 0) <= 0:
            return False
        for member in members:
            p = root / 'reports/pilot_parity' / (jid + '_' + str(member['pid']) + '.json')
            if not p.exists() or not rt.read_json(p)['passed']:
                return False
        qualified.extend(members)
    result = {'passed': True, 'jobs': sorted(ids), 'producers': len(qualified),
              'minimum_new_days_per_producer': 3, 'resources': resource,
              'seven_input_direct_reference_checks': True,
              'release_sha256': file_hash(rt.HERE / 'release.json'), 'completed': rt.now()}
    atomic_json(result_path, result)
    return True


def cpu(c):
    from wisp_stream.backend import qualify
    import psutil
    qualification = qualify(c)
    atomic_json(Path(c['root']) / 'reports/qualification' / (os.environ.get('SLURM_JOB_ID', 'local') + '.json'), qualification)
    children = [subprocess.Popen([sys.executable, '-u', str(rt.HERE / 'controller.py'), 'cpu-one']) for _ in range(2)]
    jid = os.environ.get('SLURM_JOB_ID', 'local')
    started = time.monotonic()
    peak = 0
    observed_cpu = {}
    forwarded = False
    while any(p.poll() is None for p in children):
        if rt.STOP and not forwarded:
            for p in children:
                if p.poll() is None:
                    p.send_signal(signal.SIGTERM)
            forwarded = True
        processes = [psutil.Process()] + psutil.Process().children(recursive=True)
        rss = 0
        for p in processes:
            try:
                rss += p.memory_info().rss
                t = p.cpu_times()
                observed_cpu[p.pid] = t.user + t.system
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        peak = max(peak, rss)
        elapsed = time.monotonic() - started
        atomic_json(Path(c['root']) / 'state/job_resources' / (jid + '.json'),
                    {'job': jid, 'at': rt.now(), 'allocated_cpu': 4, 'allocated_memory_gib': 16,
                     'max_aggregate_rss_bytes': peak, 'current_aggregate_rss_bytes': rss,
                     'cpu_seconds': sum(observed_cpu.values()), 'elapsed_seconds': elapsed,
                     'cpu_efficiency_fraction': sum(observed_cpu.values()) / max(1, elapsed * 4),
                     'children': [p.pid for p in children], 'child_exit_codes': [p.poll() for p in children],
                     'resource_error': any(p.poll() not in (None, 0) for p in children)})
        time.sleep(10)
    codes = [p.wait() for p in children]
    report_path = Path(c['root']) / 'state/job_resources' / (jid + '.json')
    r = rt.read_json(report_path)
    r.update(completed=rt.now(), child_exit_codes=codes, resource_error=any(codes))
    atomic_json(report_path, r)
    if any(codes):
        raise RuntimeError('Month producer failed: ' + str(codes))


def verify_inputs(c):
    import numpy as np
    import pandas as pd
    from wisp_batch.store import InputStore
    q = rt.Queue(c)
    state = q.snapshot()
    if state['phase'] != 'INPUT_VERIFY':
        raise ValueError('Not in input verification phase')
    token = state['active_cube']
    tasks = [(k, t) for k, t in sorted(state['tasks'].items()) if t['region'] == token and t['kind'] == 'month']
    if len(tasks) != 120 or any(t['input_status'] != 'ready' or t.get('lease') for _, t in tasks):
        raise ValueError('120 ready, unowned months required')
    dates, months = [], []
    for key, task in tasks:
        with rt.locked(Path(c['root']) / 'locks/tasks' / (key + '.lock')):
            store = InputStore(rt.common.task_paths(c, key)[0])
            if not (store.path / '_SUCCESS.json').exists() or store.fingerprint != task['input_fingerprint']:
                raise ValueError('Input completion/fingerprint mismatch')
            if list(store.dates) != task['dates']:
                raise ValueError('Input month dates differ')
            for i in range(len(store.dates)):
                arrays = store.read(i)
                if any(not np.isfinite(a).all() for a in arrays.values()):
                    raise ValueError('Nonfinite model input')
                del arrays
            if task.get('prediction_status') == 'done':
                from products import validate_month
                r = rt.read_json(Path(c['root']) / 'manifests/tasks' / key / 'output.json')
                if r['input_fingerprint'] != store.fingerprint:
                    raise ValueError('Reused prediction was computed from different inputs')
                validate_month(c, key, r)
            dates.extend(store.dates)
            months.append({'key': key, 'path': str(store.path), 'input_fingerprint': store.fingerprint,
                           'days': len(store.dates), 'success_sha256': file_hash(store.path / '_SUCCESS.json')})
            print('INPUT_VERIFIED', key, len(store.dates), flush=True)
    expected = pd.date_range(c['start'], c['end_exclusive'], inclusive='left').strftime('%Y-%m-%dT00:00:00').tolist()
    if dates != expected or len(dates) != 3641:
        raise ValueError('Decade input dates incomplete/duplicated')
    report = {'passed': True, 'token': token, 'days': len(dates), 'months': months,
              'weather_first_hour': '2014-01-08T00:00:00', 'weather_last_hour': '2023-12-31T23:00:00',
              'normalization': rt.common.identity(c)['normalization'], 'completed': rt.now()}
    atomic_json(Path(c['root']) / 'inputs' / token / 'decade_manifest.json', report)
    with q.edit() as d:
        if d['active_cube'] != token or d['phase'] != 'INPUT_VERIFY':
            raise ValueError('Phase changed during input verification')
        d.update(phase='PREDICT', inputs_verified_at=rt.now())


def merge(c):
    from products import consolidate
    q = rt.Queue(c)
    d = q.snapshot()
    if d['phase'] != 'MERGE':
        raise ValueError('Not in merge phase')
    token = d['active_cube']
    region = rt.read_json(Path(c['root']) / 'manifests/regions.json')[token]
    keys = sorted(k for k, t in d['tasks'].items() if t['region'] == token and t['kind'] == 'month')
    r = consolidate(c, region, keys)
    with q.edit() as state:
        state.update(phase='CLEANUP_WAIT', cleanup_authorized=True,
                     cleanup_not_before_epoch=max(time.time(), r['completed_epoch']) + 600,
                     final_receipt_sha256=file_hash(Path(c['root']) / 'reports/regions' / (region['region_id'] + '.json')))


def cleanup(c):
    from products import cleanup_cube
    started = time.monotonic()
    while time.monotonic() - started < 11 * 3600 and not rt.STOP:
        state = rt.Queue(c).snapshot()
        if state['phase'] != 'CLEANUP_WAIT':
            return
        if time.time() < state['cleanup_not_before_epoch']:
            time.sleep(min(30, max(1, state['cleanup_not_before_epoch'] - time.time())))
            continue
        cleanup_cube(c, state['active_cube'])
        if rt.Queue(c).snapshot()['phase'] == 'CLEANED':
            return
        time.sleep(30)


def status(c, current):
    d = rt.Queue(c).snapshot()
    tasks = [t for t in d['tasks'].values() if t['region'] == d['active_cube'] and t['kind'] == 'month']
    result = {'at': rt.now(), 'active_cube': d['active_cube'], 'phase': d['phase'],
              'input_months': dict(Counter(t['input_status'] for t in tasks)),
              'prediction_months': dict(Counter(t['prediction_status'] for t in tasks)),
              'cpu_jobs_limit': CPU_CAP, 'all_jobs_limit': TOTAL_CAP, 'jobs': current,
              'pilot_passed': (Path(c['root']) / 'reports/pilot_passed.json').exists(),
              'reserved_bytes': sum(t.get('reserved', 0) for t in d['tasks'].values()),
              'quarantine_bytes': d.get('quarantine_bytes', 0), 'scratch_budget_bytes': c['scratch_budget_bytes']}
    atomic_json(Path(c['root']) / 'reports/progress.json', result)
    print(json.dumps(result), flush=True)
    return result


def control(c, once=False):
    q = rt.Queue(c)
    root = Path(c['root'])
    start = time.monotonic()
    with rt.locked(root / 'locks/controller.lock', False):
        while time.monotonic() - start < 10.5 * 3600 and not rt.STOP and not (root / 'state/STOP').exists():
            current = jobs(c)
            rt.recover(c, {j['id'] for j in current})
            d = q.snapshot()
            tasks = [t for t in d['tasks'].values() if t['region'] == d['active_cube'] and t['kind'] == 'month']
            blocked = [t for t in tasks if 'blocked' in (t['input_status'], t['prediction_status'])]
            if blocked:
                status(c, current)
                atomic_json(root / 'reports/blocked.json', {'at': rt.now(), 'tasks': blocked})
            elif d['phase'] == 'BUILD':
                passed = pilot_gate(c)
                cap = CPU_CAP if passed else PILOT_CAP
                if all(t['input_status'] == 'ready' for t in tasks) and len(tasks) == 120:
                    with q.edit() as state:
                        state['phase'] = 'INPUT_VERIFY'
                else:
                    pending = sum(t['input_status'] in {'queued', 'building'} for t in tasks)
                    target = min(cap, math.ceil(pending / 2))
                    count = sum(j['kind'] == 'cpu' for j in current)
                    for _ in range(max(0, target - count)):
                        submit(c, 'cpu')
            elif d['phase'] in {'INPUT_VERIFY', 'PREDICT', 'MERGE', 'CLEANUP_WAIT'}:
                kind = {'INPUT_VERIFY': 'verify', 'PREDICT': 'gpu', 'MERGE': 'merge', 'CLEANUP_WAIT': 'cleanup'}[d['phase']]
                if d['phase'] == 'PREDICT' and all(t['prediction_status'] == 'done' for t in tasks):
                    with q.edit() as state:
                        state['phase'] = 'MERGE'
                elif not any(j['kind'] == kind for j in current):
                    # Deterministic stage errors halt the controller rather than retry forever.
                    if (root / 'reports/stage_error.json').exists():
                        raise ValueError('A stage failed; inspect stage_error.json before resuming')
                    submit(c, kind)
            elif d['phase'] == 'CLEANED':
                from migrate import activate_next
                activate_next(c)
            elif d['phase'] == 'COMPLETE':
                status(c, current)
                return
            status(c, jobs(c))
            if once:
                return
            time.sleep(30)
        if not rt.STOP and not (root / 'state/STOP').exists():
            submit(c, 'control', dependency=os.environ.get('SLURM_JOB_ID'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['submit-control', 'control', 'once', 'cpu', 'cpu-one', 'verify', 'gpu', 'merge', 'cleanup', 'status'])
    args = parser.parse_args()
    c = rt.config()
    rt.verify_execution(c)
    rt.install(c)
    try:
        if args.command == 'submit-control':
            submit(c, 'control')
        elif args.command in {'control', 'once'}:
            control(c, once=args.command == 'once')
        elif args.command == 'cpu':
            cpu(c)
        elif args.command == 'cpu-one':
            rt.cpu_one(c)
        elif args.command == 'verify':
            verify_inputs(c)
        elif args.command == 'gpu':
            from wisp_stream.pipeline import gpu_worker
            gpu_worker(c)
        elif args.command == 'merge':
            merge(c)
        elif args.command == 'cleanup':
            cleanup(c)
        else:
            status(c, jobs(c))
    except rt.StopAtCheckpoint:
        return
    except Exception as exc:
        import traceback
        atomic_json(Path(c['root']) / 'reports/errors' / (args.command + '_' + str(time.time_ns()) + '.json'),
                    {'command': args.command, 'at': rt.now(), 'error': str(exc), 'traceback': traceback.format_exc()})
        if args.command in {'verify', 'gpu', 'merge', 'cleanup'}:
            atomic_json(Path(c['root']) / 'reports/stage_error.json', {'command': args.command, 'error': str(exc), 'at': rt.now()})
        raise


if __name__ == '__main__':
    main()
