"""Audited recovery of the seven node-r6-he05 startup blocks; dry-run by default.

Standalone standard-library tool. It never edits frozen code, task queues, or
inputs, and never submits/cancels jobs. Stop the controller before --apply.
"""
import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess

NODE = 'node-r6-he05'
ERROR = 'CPU nonlinear math differs from frozen reference: ' + NODE
TOKENS = ('T000771', 'T000809', 'T000828', 'T000904', 'T000924', 'T000952', 'T001355')
DEFAULT_ROOT = Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/wisp_eos_fire_episodes_batch1_v2')
FILES = {'stage_error': 'reports/stage_error.json',
         'stage_failures': 'state/stage_failures.json', 'stop': 'state/STOP'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    data = (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    with temp.open('wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.lockf(handle, fcntl.LOCK_UN)


def scheduler_rows():
    result = subprocess.run(['squeue', '-h', '-r', '-u', os.environ.get('USER', 'ybai'),
                             '-o', '%i|%j|%T'], check=True, universal_newlines=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    return [dict(zip(('id', 'name', 'state'), line.split('|')))
            for line in result.stdout.splitlines() if line]


def controllers(root, rows):
    found = []
    for row in rows:
        registry = root / 'state/submissions' / (row['id'] + '.json')
        registered = read(registry) if registry.exists() else {}
        if row['name'] == 'w4e-control' or registered.get('kind') == 'control':
            found.append(row)
        elif row['name'].startswith('w4e-') and not registered:
            raise ValueError('Unregistered campaign job: ' + row['id'])
    return found


def snapshot(path):
    data = path.read_bytes()
    return {'sha256': digest(data), 'base64': base64.b64encode(data).decode()}


def original(record):
    data = base64.b64decode(record['base64'], validate=True)
    if digest(data) != record['sha256']:
        raise ValueError('Recovery archive checksum mismatch')
    return data


def prepare(root, release_sha256):
    state = read(root / 'state/campaign.json')
    regions = {}
    for token in TOKENS:
        rr = root / 'regions' / token
        error = read(rr / FILES['stage_error'])
        queue = read(rr / 'state/queue.json')
        before = state['regions'][token]
        if (error.get('error') != ERROR or error.get('kind') != 'cpu-startup'
                or error.get('token') != token or error.get('transient') is not False
                or queue['phase'] != 'BUILD' or before.get('status') != 'BLOCKED'
                or before.get('error') != ERROR):
            raise ValueError('Region is not the exact node startup block: ' + token)
        key = str(queue['active_wave']) + ':cpu-startup'
        failures = read(rr / FILES['stage_failures'])
        if not isinstance(failures.get(key), int) or failures[key] < 1:
            raise ValueError('Missing matching startup counter: ' + token)
        if failures[key] != error.get('attempts'):
            raise ValueError('Startup error/counter mismatch: ' + token)
        after = dict(before)
        after.pop('error', None)
        after['status'] = 'ACTIVE' if token in state['lanes'] else 'READY'
        if after['status'] == 'READY':
            after.pop('lane', None)
        regions[token] = {'before': before, 'after': after, 'counter_key': key,
                          'wave': queue['active_wave'], 'queue_sha256': digest((rr / 'state/queue.json').read_bytes()),
                          'files': {name: snapshot(rr / path) for name, path in FILES.items()}}
    return {'schema': 1, 'root': str(root), 'node': NODE, 'error': ERROR,
            'release_sha256': release_sha256, 'created': now(), 'phase': 'PREPARED',
            'regions': regions, 'campaign_before': snapshot(root / 'state/campaign.json')}


def validate_resume(root, journal, release_sha256):
    if (journal.get('schema') != 1 or journal.get('root') != str(root)
            or journal.get('node') != NODE or journal.get('error') != ERROR
            or journal.get('release_sha256') != release_sha256
            or set(journal.get('regions', {})) != set(TOKENS)):
        raise ValueError('Recovery manifest identity or frozen release changed')
    state = read(root / 'state/campaign.json')
    for token, item in journal['regions'].items():
        rr = root / 'regions' / token
        queue = read(rr / 'state/queue.json')
        if queue['phase'] != 'BUILD' or queue['active_wave'] != item['wave']:
            raise ValueError('Region phase/wave changed: ' + token)
        if state['regions'][token] not in (item['before'], item['after']):
            raise ValueError('Region state changed outside this recovery: ' + token)
        expected_status = 'ACTIVE' if token in state['lanes'] else 'READY'
        if item['after']['status'] != expected_status:
            raise ValueError('Region lane ownership changed: ' + token)
        for name, relative in FILES.items():
            archived = original(item['files'][name])
            path = rr / relative
            if name == 'stage_failures':
                before = json.loads(archived)
                after = dict(before)
                after.pop(item['counter_key'])
                if read(path) not in (before, after):
                    raise ValueError('Stage counters changed: ' + token)
            elif path.exists() and path.read_bytes() != archived:
                raise ValueError('Recovery target changed: ' + str(path))


def finish(root, journal, journal_path):
    # PREPARED is a durable write-ahead intent containing every original byte.
    # A crash between removals and campaign update is safe to resume.
    for token, item in journal['regions'].items():
        rr = root / 'regions' / token
        failures = json.loads(original(item['files']['stage_failures']))
        failures.pop(item['counter_key'])
        if read(rr / FILES['stage_failures']) != failures:
            atomic(rr / FILES['stage_failures'], failures)
        for name in ('stage_error', 'stop'):
            target = rr / FILES[name]
            if target.exists():
                target.unlink()
                sync_dir(target.parent)
    state = read(root / 'state/campaign.json')
    for token, item in journal['regions'].items():
        state['regions'][token] = item['after']
    atomic(root / 'state/campaign.json', state)
    journal.update(phase='COMPLETE', completed=now())
    atomic(journal_path, journal)
    return journal


def recover(root, *, apply=False, release_sha256=None, slurm_rows=None):
    root = Path(root).resolve()
    current_sha = digest((root / 'code/release.json').read_bytes())
    if release_sha256 is not None and current_sha != release_sha256:
        raise ValueError('Frozen release SHA256 differs from requested release')
    if apply and not release_sha256:
        raise ValueError('--apply requires --release-sha256 from the reviewed dry-run')
    journal_path = root / 'reports/operations' / ('recover_' + NODE + '.json')
    if not apply:
        live = controllers(root, scheduler_rows() if slurm_rows is None else slurm_rows)
        journal = read(journal_path) if journal_path.exists() else prepare(root, current_sha)
        validate_resume(root, journal, current_sha)
        return dict(journal, dry_run=True, controllers=live, apply_ready=not live,
                    audit_path=str(journal_path))
    with contextlib.ExitStack() as locks:
        for relative in ('locks/controller.lock', 'locks/submit.lock', 'locks/campaign.lock'):
            locks.enter_context(locked(root / relative))
        live = controllers(root, scheduler_rows() if slurm_rows is None else slurm_rows)
        if live:
            raise ValueError('Controller still active or pending: ' + ','.join(r['id'] for r in live))
        for token in TOKENS:
            rr = root / 'regions' / token
            for relative in ('locks/stage_failures.lock', 'locks/queue.lock'):
                locks.enter_context(locked(rr / relative))
        if digest((root / 'code/release.json').read_bytes()) != current_sha:
            raise ValueError('Frozen release changed while acquiring locks')
        journal = read(journal_path) if journal_path.exists() else prepare(root, current_sha)
        validate_resume(root, journal, current_sha)
        if journal['phase'] == 'COMPLETE':
            return journal
        if not journal_path.exists():
            atomic(journal_path, journal)
        return finish(root, journal, journal_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--release-sha256')
    args = parser.parse_args()
    result = recover(args.root, apply=args.apply, release_sha256=args.release_sha256)
    summary = {key: value for key, value in result.items()
               if key not in {'regions', 'campaign_before'}}
    summary['regions'] = {token: {'before': item['before']['status'],
                                 'after': item['after']['status'],
                                 'counter_key': item['counter_key']}
                          for token, item in result['regions'].items()}
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
