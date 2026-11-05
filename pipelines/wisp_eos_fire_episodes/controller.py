"""Retrospective event experiment: isolated short inputs, one coordinated GPU."""
import argparse
from collections import Counter
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import runtime as rt
from wisp_batch.common import atomic_json, file_hash
from coordination import GPU_SUBMIT_LOCK, gpu_busy, slurm_jobs

CPU_CAP = 32  # ALL event non-GPU Slurm jobs, including the controller.
PILOT_CAP = 2
PREFIX = 'wispev-'


def jobs(c):
    result = []
    for row in slurm_jobs():
        if not row['name'].startswith(PREFIX):
            continue
        path = Path(c['root']) / 'state/submissions' / (row['id'] + '.json')
        if not path.exists():
            raise ValueError('Unregistered event job: ' + row['id'])
        r = rt.read_json(path)
        if r['name'] != row['name'] or r['job'] != row['id']:
            raise ValueError('Event job identity differs')
        result.append(dict(row, kind=r['kind']))
    return result


def submit(c, kind, dependency=None):
    # Both legacy continuation controllers acquire this exact inode before sbatch.
    with rt.locked(GPU_SUBMIT_LOCK):
        current = jobs(c)
        if kind == 'control':
            controls = [j for j in current if j['kind'] == 'control']
            if controls and (not dependency or len(controls) != 1 or controls[0]['id'] != str(dependency)):
                raise ValueError('An event controller already exists')
        if kind == 'gpu' and gpu_busy():
            return None
        if kind != 'gpu' and sum(j['kind'] != 'gpu' for j in current) >= CPU_CAP:
            return None
        cpu, mem, part, wall = {
            'control': (1, '2G', 'work,small,gpu', '12:00:00'),
            'cpu': (4, '16G', 'work,small,gpu', '24:00:00'),
            'verify': (4, '16G', 'work,small,gpu', '24:00:00'),
            'gpu': (8, '64G', 'gpu', '24:00:00'),
            'merge': (2, '8G', 'work,small,gpu', '12:00:00'),
            'cleanup': (1, '4G', 'work,small,gpu', '12:00:00'),
        }[kind]
        root = Path(c['root']); name = PREFIX + kind
        args = ['sbatch', '--parsable', '--account=science', '--qos=normal',
                '--job-name=' + name, '--partition=' + part,
                '--cpus-per-task=' + str(cpu), '--mem=' + mem, '--time=' + wall,
                '--chdir=' + str(root), '--output=' + str(root / 'logs' / (kind + '_%j.out')),
                '--error=' + str(root / 'logs' / (kind + '_%j.err'))]
        if kind == 'gpu':
            args += ['--gres=gpu:A100:1']
        if dependency:
            args += ['--dependency=afterany:' + str(dependency)]
        args += [str(rt.HERE / 'job.sh'), kind]
        r = subprocess.run(args, check=True, text=True, capture_output=True, timeout=45)
        jid = r.stdout.strip().split(';')[0]
        if not jid.isdigit():
            raise ValueError('Unexpected sbatch response')
        atomic_json(root / 'state/submissions' / (jid + '.json'),
                    dict(job=jid, name=name, kind=kind, args=args, at=rt.now(),
                         release_sha256=file_hash(rt.HERE / 'release.json')))
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
    pilot_jobs = sorted(qualified_jobs, key=lambda r: r['at'])[:PILOT_CAP]
    if len(pilot_jobs) < PILOT_CAP:
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
        raise RuntimeError('Event producer failed: ' + str(codes))



def verify_inputs(c):
    import numpy as np
    from wisp_batch.store import InputStore
    from products import compare_full_input_hashes
    root = Path(c['root']); q = rt.Queue(c); state = q.snapshot()
    selection = rt.read_json(root / 'manifests/selection.json')
    if state['phase'] != 'INPUT_VERIFY' or set(state['tasks']) != set(selection['tasks']):
        raise ValueError('Not the selected input verification phase')
    observed = {}; reports = []
    for key, task in sorted(state['tasks'].items()):
        if task['input_status'] != 'ready' or task.get('lease'):
            raise ValueError('Input is not complete and unowned: ' + key)
        with rt.locked(root / 'locks/tasks' / (key + '.lock'), False):
            store = InputStore(rt.common.task_paths(c, key)[0])
            if not (store.path / '_SUCCESS.json').exists() or store.fingerprint != task['input_fingerprint']:
                raise ValueError('Input fingerprint mismatch')
            if list(store.dates) != task['dates']:
                raise ValueError('Input date manifest mismatch')
            for i in range(len(store.dates)):
                arrays = store.read(i)  # Enforces the per-array write receipt hashes.
                if any(not np.isfinite(a).all() for a in arrays.values()):
                    raise ValueError('Nonfinite model input')
                del arrays
            observed.setdefault(task['region'], []).extend(store.dates)
            reports.append(dict(key=key, days=len(store.dates), fingerprint=store.fingerprint,
                                success_sha256=file_hash(store.path / '_SUCCESS.json')))
        print('INPUT_VERIFIED', key, len(task['dates']), flush=True)
    for token, r in selection['regions'].items():
        if sorted(observed.get(token, [])) != sorted(r['selected_dates']):
            raise ValueError('Selected days missing or duplicated: ' + token)
    parity_dates = rt.read_json(root / 'manifests/parity_dates.json')
    checked = [rt.read_json(p) for p in (root / 'reports/input_parity').glob('*.json')]
    for token, days in parity_dates.items():
        for day in days:
            if not any(r.get('passed') and r.get('date') == day and r.get('key', '').startswith(token + '_') for r in checked):
                raise ValueError('Boundary parity evidence missing: ' + token + ' ' + day)
    regions = rt.read_json(root / 'manifests/regions.json')
    old = rt.read_json(root / 'manifests/operations.json')['full_roots']
    comparison = {token: compare_full_input_hashes(c, regions[token], state['tasks'], old)
                  for token in selection['regions']}
    if any(r['status'] == 'failed' for r in comparison.values()):
        raise ValueError('Event inputs differ from full-decade retained tensor hashes')
    atomic_json(root / 'reports/inputs_verified.json',
                dict(passed=True, days=sum(map(len, observed.values())), chunks=reports,
                     full_run_comparison=comparison, at=rt.now()))
    with q.edit() as d:
        if d['phase'] != 'INPUT_VERIFY':
            raise ValueError('Input phase changed')
        d.update(phase='PREDICT', inputs_verified_at=rt.now())


def gpu(c):
    import numpy as np
    import torch
    from wisp_stream import pipeline as p
    from wisp_batch.store import InputStore
    root = Path(c['root']); selection = rt.read_json(root / 'manifests/selection.json')
    torch.manual_seed(42); np.random.seed(42)
    hardware = torch.cuda.get_device_name(0)
    if 'A100' not in hardware:
        raise ValueError('This pilot requires A100')
    model = p.BatchedEOS(c['checkpoint'], c['vendor_root']); model.model.requires_grad_(False)
    # Eight dates, four per cube. These are NOT eight independent regions.
    examples = []; members = []
    for token in ['T000830', 'T000001']:
        dates = selection['regions'][token]['selected_dates']
        chosen = [dates[round(i * (len(dates) - 1) / 3)] for i in range(4)]
        for day in chosen:
            key, task = next((k, t) for k, t in selection['tasks'].items()
                             if t['region'] == token and day in t['dates'])
            store = InputStore(rt.common.task_paths(c, key)[0])
            examples.append((store, store.dates.index(day)))
            members.append(dict(token=token, day=day, key=key))
    p.mixed_check(c, model, examples, hardware)
    atomic_json(root / 'reports/batch_gate_members.json',
                dict(regions=2, distinct_dates_per_region=4, members=members,
                     meaning='Eight dates, not eight regions', at=rt.now()))
    # The unchanged worker reuses this already-loaded frozen model.
    original = p.BatchedEOS
    p.BatchedEOS = lambda *args, **kwargs: model
    try:
        p.gpu_worker(c)
    finally:
        p.BatchedEOS = original


def merge(c):
    from products import consolidate, compare_full_predictions
    root = Path(c['root']); q = rt.Queue(c); state = q.snapshot()
    selection = rt.read_json(root / 'manifests/selection.json')
    regions = rt.read_json(root / 'manifests/regions.json')
    old = rt.read_json(root / 'manifests/operations.json')['full_roots']
    if state['phase'] != 'MERGE':
        raise ValueError('Not in merge phase')
    receipts = {}
    for token in selection['regions']:
        tasks = {k: t for k, t in state['tasks'].items() if t['region'] == token}
        receipts[token] = consolidate(c, regions[token], tasks, selection['regions'][token])
        compare_full_predictions(c, regions[token], tasks, old)
    atomic_json(root / 'reports/products_verified.json', dict(passed=True, regions=receipts, at=rt.now()))
    with q.edit() as d:
        d.update(phase='CLEANUP_WAIT', cleanup_authorized=True,
                 cleanup_not_before_epoch=time.time() + 600)


def cleanup(c):
    from products import cleanup_region
    q = rt.Queue(c); started = time.monotonic()
    selection = rt.read_json(Path(c['root']) / 'manifests/selection.json')
    while time.monotonic() - started < 10 * 3600 and not rt.STOP:
        state = q.snapshot()
        if state['phase'] != 'CLEANUP_WAIT':
            return
        if time.time() < state['cleanup_not_before_epoch']:
            time.sleep(30); continue
        pending = False
        for token, region_selection in selection['regions'].items():
            tasks = {k: t for k, t in state['tasks'].items() if t['region'] == token}
            receipt = cleanup_region(c, token, tasks, region_selection)
            pending = pending or receipt.get('pending', False)
        if pending:
            time.sleep(30)
            continue
        with q.edit() as d:
            if not all(t['input_status'] == 'cleaned' for t in d['tasks'].values()):
                raise ValueError('Cleanup did not finish all event input tasks')
            d.update(phase='REFERENCE_WAIT', products_completed_at=rt.now())
        return


def refresh_comparisons(c):
    from products import compare_full_input_hashes, compare_full_predictions
    root = Path(c['root']); state = rt.Queue(c).snapshot()
    regions = rt.read_json(root / 'manifests/regions.json')
    old = rt.read_json(root / 'manifests/operations.json')['full_roots']
    reports = {}
    for token, region in regions.items():
        inputs = compare_full_input_hashes(c, region, state['tasks'], old)
        if inputs['status'] == 'failed':
            raise ValueError('Retained full-run input parity failed: ' + token)
        outputs = compare_full_predictions(c, region, state['tasks'], old)
        reports[token] = dict(inputs=inputs['status'], predictions=outputs['status'])
    passed = all(r == dict(inputs='passed', predictions='compared') for r in reports.values())
    atomic_json(root / 'reports/full_run_comparison.json', dict(complete=passed, regions=reports, at=rt.now()))
    from report import write_report
    write_report(c, query_sacct=True)
    if passed:
        with rt.Queue(c).edit() as d:
            d.update(phase='COMPLETE', completed_at=rt.now())
    return passed



def status(c, current):
    state = rt.Queue(c).snapshot(); tasks = list(state['tasks'].values())
    result = dict(at=rt.now(), phase=state['phase'], tasks=len(tasks),
                  selected_days=sum(len(t['dates']) for t in tasks),
                  input_tasks=dict(Counter(t['input_status'] for t in tasks)),
                  predicted_tasks=dict(Counter(t['prediction_status'] for t in tasks)),
                  cpu_jobs_max_including_control=CPU_CAP, jobs=current,
                  pilot_passed=(Path(c['root']) / 'reports/pilot_passed.json').exists(),
                  reserved_bytes=sum(t.get('reserved', 0) for t in tasks),
                  scratch_budget_bytes=c['scratch_budget_bytes'])
    atomic_json(Path(c['root']) / 'reports/progress.json', result)
    print(__import__('json').dumps(result), flush=True)
    return result


def control(c, once=False):
    q = rt.Queue(c); root = Path(c['root']); started = time.monotonic()
    with rt.locked(root / 'locks/controller.lock', False):
        while time.monotonic() - started < 10 * 3600 and not rt.STOP and not (root / 'state/STOP').exists():
            current = jobs(c); rt.recover(c, {j['id'] for j in current})
            state = q.snapshot(); tasks = list(state['tasks'].values())
            if (root / 'reports/stage_error.json').exists():
                raise ValueError('Stage error needs inspection; no blind repeat')
            blocked = {k: t for k, t in state['tasks'].items()
                       if 'blocked' in (t['input_status'], t['prediction_status'])}
            if blocked:
                atomic_json(root / 'reports/blocked.json', dict(at=rt.now(), tasks=blocked))
            elif state['phase'] == 'BUILD':
                if all(t['input_status'] == 'ready' for t in tasks):
                    with q.edit() as d:
                        d['phase'] = 'INPUT_VERIFY'
                else:
                    cap = CPU_CAP - 2 if pilot_gate(c) else PILOT_CAP  # Keep one renewal slot below total32.
                    pending = sum(t['input_status'] in {'queued', 'building'} for t in tasks)
                    target = min(cap, math.ceil(pending / 2))
                    for _ in range(max(0, target - sum(j['kind'] == 'cpu' for j in current))):
                        if submit(c, 'cpu') is None:
                            break
            elif state['phase'] in {'INPUT_VERIFY', 'PREDICT', 'MERGE', 'CLEANUP_WAIT'}:
                kind = dict(INPUT_VERIFY='verify', PREDICT='gpu', MERGE='merge', CLEANUP_WAIT='cleanup')[state['phase']]
                if state['phase'] == 'PREDICT' and all(t['prediction_status'] == 'done' for t in tasks):
                    with q.edit() as d:
                        d['phase'] = 'MERGE'
                elif not any(j['kind'] == kind for j in current):
                    submit(c, kind)
            elif state['phase'] == 'REFERENCE_WAIT':
                refreshed = root / 'reports/full_run_comparison.json'
                if not refreshed.exists() or time.time() - refreshed.stat().st_mtime >= 300:
                    refresh_comparisons(c)
            elif state['phase'] == 'COMPLETE':
                from report import write_report
                write_report(c, query_sacct=True)
                status(c, current); return
            else:
                raise ValueError('Unknown event phase: ' + state['phase'])
            status(c, jobs(c))
            if once:
                return
            time.sleep(30)
        if not rt.STOP and not (root / 'state/STOP').exists():
            if submit(c, 'control', dependency=os.environ.get('SLURM_JOB_ID')) is None:
                raise RuntimeError('Controller renewal was not admitted; explicit recovery required')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['submit-control', 'control', 'once', 'cpu', 'cpu-one', 'verify', 'gpu', 'merge', 'cleanup', 'status'])
    a = parser.parse_args(); c = rt.config(); rt.verify_execution(c); rt.install(c)
    try:
        if a.command == 'submit-control': submit(c, 'control')
        elif a.command in {'control', 'once'}: control(c, once=a.command == 'once')
        elif a.command == 'cpu': cpu(c)
        elif a.command == 'cpu-one': rt.cpu_one(c)
        elif a.command == 'verify': verify_inputs(c)
        elif a.command == 'gpu': gpu(c)
        elif a.command == 'merge': merge(c)
        elif a.command == 'cleanup': cleanup(c)
        else: status(c, jobs(c))
    except rt.StopAtCheckpoint:
        return
    except Exception as exc:
        import traceback
        atomic_json(Path(c['root']) / 'reports/errors' / (a.command + '_' + str(time.time_ns()) + '.json'),
                    dict(command=a.command, at=rt.now(), error=str(exc), traceback=traceback.format_exc()))
        if a.command in {'verify', 'gpu', 'merge', 'cleanup'}:
            atomic_json(Path(c['root']) / 'reports/stage_error.json', dict(command=a.command, error=str(exc), at=rt.now()))
        raise


if __name__ == '__main__':
    main()
