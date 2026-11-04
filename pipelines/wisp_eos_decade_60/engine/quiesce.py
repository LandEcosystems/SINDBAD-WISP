"""Stop only verified jobs belonging to the previous WISP operations release.

Run on the Slurm login host. ``--mode start`` stops schedulers and returns;
``--mode poll`` advances the persisted stop once; ``--mode run`` polls to completion.
No old run files, inputs, outputs, or registrations are modified.
"""
import argparse
import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

BASE = Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube')
OLD_ROOT = BASE / 'shift_v3_60km/wisp_eos_q10_v1'
OLD_OPS = BASE / 'operations/wisp_production64_20261104_v3'
NEW_ROOT = BASE / 'wisp_eos_cube_first_v1'


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.%s.tmp' % os.getpid())
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    os.replace(temp, path)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(2 ** 20), b''):
            h.update(block)
    return h.hexdigest()


def run(args):
    return subprocess.run(args, text=True, capture_output=True, check=True, timeout=30).stdout


def owned_jobs(old_root=OLD_ROOT, old_ops=OLD_OPS):
    """Require both an immutable submission record and the live Slurm command."""
    old_root, old_ops = Path(old_root), Path(old_ops)
    result = []
    rows = run(['squeue', '-h', '-r', '-u', getpass.getuser(), '-o', '%i|%j|%T'])
    for row in rows.splitlines():
        fields = row.split('|')
        if len(fields) != 3:
            raise ValueError('Unexpected squeue row: ' + row)
        jid, name, state = fields
        registration = old_root / 'state/submissions' / (jid + '.json')
        if not registration.exists():
            if name.startswith('wispb1-'):
                raise ValueError('Unregistered WISP job; refuse cancellation: ' + row)
            continue
        record = read(registration)
        if record.get('controller_release') != str(old_ops):
            if name.startswith('wispb1-'):
                raise ValueError('Live WISP job belongs to another release: ' + jid)
            continue
        kind = record.get('kind')
        args = record.get('args', [])
        expected = [str(old_ops / 'job64.sh'), str(old_root / 'config/deployment.json'), kind]
        if (not jid.isdigit() or str(record.get('job')) != jid or
                kind not in {'control', 'cleanup', 'cpu', 'gpu'} or
                name != 'wispb1-' + kind or args[-3:] != expected):
            raise ValueError('Submission identity does not match: ' + jid)
        detail = run(['scontrol', 'show', 'job', '-o', jid])
        command = re.search(r'(?:^|\s)Command=(\S+)', detail)
        owner = re.search(r'(?:^|\s)UserId=([^\s(]+)', detail)
        if not command or command.group(1) != str(old_ops / 'job64.sh'):
            raise ValueError('Slurm command does not match old release: ' + jid)
        if not owner or owner.group(1) != getpass.getuser():
            raise ValueError('Slurm owner does not match: ' + jid)
        result.append({'id': jid, 'kind': kind, 'state': state,
                       'registration_sha256': sha(registration), 'slurm': detail.strip()})
    return result


def checkpoints(old_root):
    """Heartbeat updates follow committed InputStore receipts; GPU journal is atomic."""
    old_root = Path(old_root)
    result = {}
    for path in (old_root / 'state/workers').glob('*.json'):
        item = read(path)
        jid = str(item.get('job'))
        if item.get('kind') == 'cpu':
            result.setdefault(jid, []).append([item.get('task'), item.get('date'), item.get('completed')])
    tasks = read(old_root / 'state/queue.json')['tasks']
    for key, task in tasks.items():
        jid = str(task.get('job'))
        if task.get('status') == 'predicting':
            journal = old_root / 'manifests/tasks' / key / 'prediction_days.json'
            result.setdefault(jid, []).append([key, sha(journal) if journal.is_file() else None])
    return {jid: sorted(items, key=repr) for jid, items in result.items()}


def cancel(jobs, state, audit, reason):
    if not jobs:
        return
    ids = [job['id'] for job in jobs]
    entry = {'at': now(), 'ids': ids, 'reason': reason, 'verified_jobs': jobs}
    state.setdefault('actions', []).append(entry)
    atomic(audit / 'state.json', state)  # Record intent before scancel.
    entry['stdout'] = run(['scancel'] + ids)
    entry['sent'] = now()
    atomic(audit / 'state.json', state)
    print(json.dumps({'cancelled': ids, 'reason': reason, 'at': now()}), flush=True)


def receipt_inventory(old_root, scratch):
    result = {}
    for base, pattern in [(Path(old_root) / 'manifests/tasks', '*/*.json'),
                          (Path(scratch) / 'inputs', '*/receipts/*.json'),
                          (Path(scratch) / 'inputs', '*/_SUCCESS.json')]:
        for path in base.glob(pattern):
            if path.is_symlink():
                raise ValueError('Unexpected receipt symlink: ' + str(path))
            result[str(path)] = {'sha256': sha(path), 'bytes': path.stat().st_size}
    return result


def step(old_root, old_ops, audit, grace=900, quick=False):
    old_root, old_ops, audit = Path(old_root), Path(old_ops), Path(audit)
    jobs = owned_jobs(old_root, old_ops)
    path = audit / 'state.json'
    if path.exists():
        state = read(path)
        if state['old_root'] != str(old_root) or state['old_ops'] != str(old_ops):
            raise ValueError('Stop audit belongs to another deployment')
    else:
        state = {'old_root': str(old_root), 'old_ops': str(old_ops), 'started': now(),
                 'deadline_epoch': time.time() + min(900, max(0, grace)),
                 'initial_jobs': jobs, 'baseline': checkpoints(old_root), 'actions': []}
        atomic(path, state)
    # Repeated discovery catches successor jobs submitted while the controller stops.
    schedulers = [job for job in jobs if job['kind'] in {'control', 'cleanup'}]
    if schedulers:
        cancel(schedulers, state, audit, 'stop controller and janitor before workers')
        state['quiet_observations'] = 0
        state.pop('empty_since_epoch', None)
        atomic(path, state)
        return False
    current = checkpoints(old_root)
    eligible = []
    for job in jobs:
        jid = job['id']
        if (quick or time.time() >= state['deadline_epoch'] or job['state'] == 'PENDING'
                or (jid in current and current[jid] != state['baseline'].get(jid))):
            eligible.append(job)
    cancel(eligible, state, audit, 'quick stop preserving committed checkpoints' if quick
           else 'daily checkpoint observed, pending, or 15 minute deadline reached')
    if jobs:
        state['quiet_observations'] = 0
        state.pop('empty_since_epoch', None)
        state['last_jobs'] = jobs
        atomic(path, state)
        print(json.dumps({'waiting_jobs': len(jobs), 'deadline_epoch': state['deadline_epoch'], 'at': now()}), flush=True)
        return False
    # Require two separated empty scheduler observations before publishing completion.
    previous_empty = state.get('empty_since_epoch')
    if previous_empty is None:
        state['empty_since_epoch'] = time.time()
        atomic(path, state)
        return False
    if time.time() - previous_empty < 2:
        return False
    c = read(old_root / 'config/deployment.json')
    receipts = receipt_inventory(old_root, c['scratch'])
    if owned_jobs(old_root, old_ops):
        state.pop('empty_since_epoch', None)
        atomic(path, state)
        return False
    atomic(audit / 'receipts_after_stop.json', receipts)
    atomic(audit / 'queue_after_stop.json', read(old_root / 'state/queue.json'))
    state.update(passed=True, completed=now(), remaining_jobs=[],
                 queue_sha256=sha(old_root / 'state/queue.json'),
                 receipts_inventory_sha256=sha(audit / 'receipts_after_stop.json'),
                 receipt_count=len(receipts))
    atomic(path, state)
    atomic(audit / 'complete.json', state)
    print(json.dumps({'passed': True, 'receipts_preserved': len(receipts), 'at': now()}), flush=True)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-root', type=Path, default=OLD_ROOT)
    parser.add_argument('--old-ops', type=Path, default=OLD_OPS)
    parser.add_argument('--audit-dir', type=Path, default=NEW_ROOT / 'code/migration/quiesce')
    parser.add_argument('--mode', choices=['start', 'poll', 'run'], default='run')
    parser.add_argument('--grace-seconds', type=int, default=900)
    parser.add_argument('--quick', action='store_true')
    args = parser.parse_args()
    while True:
        done = step(args.old_root, args.old_ops, args.audit_dir, args.grace_seconds, args.quick)
        if done or args.mode != 'run':
            return 0 if done else 2
        time.sleep(5)


if __name__ == '__main__':
    raise SystemExit(main())
