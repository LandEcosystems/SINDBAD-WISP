"""Quiescent repair of CVFS client-local flock lost updates.

Run on the login node only after every campaign job has checkpointed or is held.
The queue is the durable authority for missing budget leases. Reconstruct a
lease only for a scheduler-confirmed terminated owner, under its task lock.
No tensor, prediction, date selection or completion flag is synthesized.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def restore_missing_writer(budget, key, task, path, confirmed_dead):
    """Caller holds the queue's task lock and has stopped all campaign writers."""
    lease = task.get('input_budget_lease')
    if (task.get('stage') != 'cpu' or not task.get('lease') or
            str(task.get('job')) not in confirmed_dead or
            not re.fullmatch(r'[0-9a-f]{32}', str(lease))):
        raise ValueError('No terminated CPU queue owner for repair: ' + key)
    _, relative = budget._store(path)
    receipt = budget.root / 'state/actual_input_budget_settled' / (lease + '.json')
    with budget._edit() as state:
        writer = state['writers'].get(key)
        if writer:
            if writer['lease'] != lease or writer['store'] != relative:
                raise ValueError('Conflicting budget owner: ' + key)
            return False
        if receipt.exists():
            settled = json.loads(receipt.read_text())
            if settled.get('key') != key or settled.get('lease') != lease:
                raise ValueError('Conflicting budget settlement: ' + key)
            return False
        if any(w['store'] == relative for w in state['writers'].values()):
            raise ValueError('Input path belongs to another budget owner')
        maximum = int(task['reserved'])
        if maximum <= 0:
            raise ValueError('Missing original writer bound')
        state['writers'][key] = dict(
            store=relative, lease=lease, maximum_store_bytes=maximum,
            owner={k: task.get(k) for k in ('job', 'pid', 'host', 'region')},
            acquired_at=time.time(), reconstructed_from_terminal_queue=True)
    return True


def main(root, source):
    root, source = root.resolve(), source.resolve()
    sys.path.insert(0, str(root / 'code/batch400'))
    from common import read, atomic, locked, sha, now, region_config
    spec = importlib.util.spec_from_file_location('disk_budget', source / 'disk_budget.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['disk_budget'] = module
    spec.loader.exec_module(module)
    op = root / 'operations/lock_recovery_20261108'
    op.mkdir(exist_ok=True)
    with locked(root / 'locks/upgrade.lock'), locked(root / 'locks/controller.lock', False), \
            locked(root / 'locks/submit.lock'):
        rows = subprocess.check_output(
            ['squeue', '-u', os.environ.get('USER', 'ybai'), '-h', '-o', '%i|%j|%T|%R'], text=True)
        for row in rows.splitlines():
            jid, name, state, reason = row.split('|', 3)
            if not name.startswith('w4e-'):
                continue
            reg = read(root / 'state/submissions' / (jid + '.json'))
            if reg['name'] != name or state != 'PENDING' or 'JobHeldUser' not in reason:
                raise ValueError('Campaign not quiescent: ' + row)
        cfg = read(root / 'config/campaign.json')
        budget = module.ActualInputBudget(root, cfg['scratch'],
                                          cfg['backlog_budget_bytes'], scan_interval_seconds=None)
        before = read(budget.path)
        backup = op / 'ledger_before_repair.json'
        if not backup.exists():
            atomic(backup, before)
        queues = {p.parents[1].name: read(p) for p in (root / 'regions').glob('*/state/queue.json')}
        ids = {str(t['job']) for q in queues.values() for t in q['tasks'].values()
               if t.get('lease') and t.get('job')}
        ids |= {str(w['owner']['job']) for w in before['writers'].values()
                if w.get('owner', {}).get('job')}
        terminal = {}
        if ids:
            rows = subprocess.check_output(['sacct', '-X', '-n', '-P', '-j', ','.join(sorted(ids)),
                                            '-o', 'JobIDRaw,State'], text=True)
            for row in rows.splitlines():
                fields = row.split('|')
                if len(fields) > 1 and fields[1].split() and fields[1].split()[0] in {
                    'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY',
                    'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE'}:
                    terminal[fields[0]] = fields[1]
        if ids - terminal.keys():
            raise ValueError('Owners without terminal scheduler proof: ' + str(ids - terminal.keys()))
        atomic(op / 'terminal_owner_proof.json', dict(at=now(), owners=terminal))
        restored = []
        for token, queue in queues.items():
            affected = [k for k, t in queue['tasks'].items() if t.get('lease')]
            if not affected:
                continue
            backup = op / 'queues_before' / (token + '.json')
            if not backup.exists():
                atomic(backup, queue)
            for key in affected:
                task = queue['tasks'][key]
                if task.get('stage') != 'cpu' or not task.get('input_budget_lease'):
                    continue
                with locked(root / 'regions' / token / 'locks/tasks' / (key + '.lock'), False):
                    path = Path(cfg['scratch']) / 'regions' / token / 'inputs' / key
                    if restore_missing_writer(budget, key, task, path, terminal):
                        restored.append(key)
                    budget.release(key, task['input_budget_lease'])
        # Includes ledger-only claims interrupted before their queue commit.
        orphans = budget.reconcile_stopped_jobs(terminal)
        import adapter
        for token, queue in queues.items():
            if any(t.get('lease') for t in queue['tasks'].values()):
                adapter.recover(region_config(root, token), set())
        remaining = [(p.parents[1].name, key) for p in (root / 'regions').glob('*/state/queue.json')
                     for key, t in read(p)['tasks'].items() if t.get('lease')]
        if remaining:
            raise ValueError('Unrecovered task owners: ' + str(remaining))
        # Lost inter-node updates can affect byte totals too: audit once while stopped.
        view = budget.snapshot(refresh=True)
        atomic(op / 'journal_repair.json', dict(at=now(), restored=restored,
               ledger_only_recovered=orphans, budget=view,
               source_sha256=sha(source / 'disk_budget.py'), no_remaining_task_leases=True))
        print(json.dumps(dict(restored=restored, ledger_only_recovered=orphans, budget=view)), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    main(args.root, args.source)
