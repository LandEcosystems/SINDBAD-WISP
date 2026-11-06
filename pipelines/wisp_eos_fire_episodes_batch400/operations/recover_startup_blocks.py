"""Recover only manifest-pinned CPU qualification blocks; dry-run by default.

The manifest is a reviewed, immutable list, not a query evaluated during apply.
No frozen code, task queue, input data, Slurm submission or cancellation is changed.
Stop the campaign controller and let target-region workers finish before apply.
"""
import argparse
import contextlib
import json
from pathlib import Path
import re

from recover_node import (DEFAULT_ROOT, FILES, atomic, controllers, digest, locked,
                          now, original, read, scheduler_rows, snapshot, sync_dir)

ERROR_PREFIX = 'CPU nonlinear math differs from frozen reference: '


def canonical_sha(value):
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode())


def freeze_manifest(root, tokens, nodes, recovery_id, release_sha256):
    """Return a manifest for caller review; never write anything."""
    root = Path(root).resolve()
    if digest((root / 'code/release.json').read_bytes()) != release_sha256:
        raise ValueError('Frozen release SHA256 mismatch')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', recovery_id):
        raise ValueError('Unsafe recovery ID')
    tokens = list(tokens)
    if not tokens or len(set(tokens)) != len(tokens):
        raise ValueError('Empty or duplicate region list')
    nodes = sorted(set(nodes))
    if not nodes or any(not re.fullmatch(r'[A-Za-z0-9_-]+', n) for n in nodes):
        raise ValueError('Unsafe or empty node list')
    state = read(root / 'state/campaign.json')
    regions = {}
    for token in sorted(tokens):
        if not re.fullmatch(r'T[0-9]{6}', token):
            raise ValueError('Unsafe region ID')
        rr = root / 'regions' / token
        error = read(rr / FILES['stage_error'])
        node = error.get('error', '').removeprefix(ERROR_PREFIX) if hasattr(str, 'removeprefix') else error.get('error', '')[len(ERROR_PREFIX):]
        if node not in nodes or error.get('error') != ERROR_PREFIX + node:
            raise ValueError('Error is not on the explicit node list: ' + token)
        regions[token] = {
            'node': node, 'error': ERROR_PREFIX + node,
            'files_sha256': {name: digest((rr / relative).read_bytes())
                             for name, relative in FILES.items()},
            'queue_sha256': digest((rr / 'state/queue.json').read_bytes()),
            'region_state_sha256': canonical_sha(state['regions'][token])}
    manifest = {'schema': 1, 'recovery_id': recovery_id, 'created': now(),
                'root': str(root), 'release_sha256': release_sha256,
                'nodes': nodes, 'regions': regions}
    prepare(root, manifest)
    return manifest


def validate_identity(root, manifest, release_sha256):
    if (manifest.get('schema') != 1 or manifest.get('root') != str(root)
            or manifest.get('release_sha256') != release_sha256
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', manifest.get('recovery_id', ''))
            or not manifest.get('regions') or not manifest.get('nodes')):
        raise ValueError('Recovery manifest identity mismatch')
    if digest((root / 'code/release.json').read_bytes()) != release_sha256:
        raise ValueError('Frozen release SHA256 mismatch')
    for token, item in manifest['regions'].items():
        if (not re.fullmatch(r'T[0-9]{6}', token)
                or item.get('node') not in manifest['nodes']
                or item.get('error') != ERROR_PREFIX + item['node']
                or set(item.get('files_sha256', {})) != set(FILES)):
            raise ValueError('Unsafe manifest entry: ' + token)


def prepare(root, manifest):
    validate_identity(root, manifest, manifest['release_sha256'])
    state = read(root / 'state/campaign.json')
    regions = {}
    for token, expected in manifest['regions'].items():
        rr = root / 'regions' / token
        files = {name: snapshot(rr / relative) for name, relative in FILES.items()}
        for name, archived in files.items():
            if archived['sha256'] != expected['files_sha256'][name]:
                raise ValueError('Manifest-pinned file changed: ' + token + '/' + name)
        queue_bytes = (rr / 'state/queue.json').read_bytes()
        if digest(queue_bytes) != expected['queue_sha256']:
            raise ValueError('Manifest-pinned queue changed: ' + token)
        queue = json.loads(queue_bytes)
        before = state['regions'][token]
        if canonical_sha(before) != expected['region_state_sha256']:
            raise ValueError('Manifest-pinned region state changed: ' + token)
        error = json.loads(original(files['stage_error']))
        if (error.get('kind') != 'cpu-startup' or error.get('token') != token
                or error.get('transient') is not False
                or error.get('error') != expected['error']
                or before.get('status') != 'BLOCKED'
                or before.get('error') != expected['error']
                or queue.get('phase') != 'BUILD'):
            raise ValueError('Not an exact CPU qualification block: ' + token)
        # STOP must be the controller's plain block signal, never a user stop.
        if original(files['stop']).strip() not in (b'', expected['error'].encode()):
            raise ValueError('Unrecognized STOP contents: ' + token)
        key = str(queue['active_wave']) + ':cpu-startup'
        failures = json.loads(original(files['stage_failures']))
        if (type(failures.get(key)) is not int or failures[key] < 1
                or failures[key] != error.get('attempts')):
            raise ValueError('Startup error/counter mismatch: ' + token)
        if any(t.get('lease') is not None for t in queue['tasks'].values()):
            raise ValueError('Target region still has leased tasks: ' + token)
        after = dict(before)
        after.pop('error', None)
        after['status'] = 'ACTIVE' if token in state['lanes'] else 'READY'
        if after['status'] == 'READY':
            after.pop('lane', None)
        regions[token] = dict(before=before, after=after, files=files,
                              wave=queue['active_wave'], counter_key=key,
                              queue_sha256=expected['queue_sha256'])
    return dict(schema=1, manifest=manifest, manifest_sha256=canonical_sha(manifest),
                root=str(root), created=now(), phase='PREPARED', regions=regions,
                campaign_before=snapshot(root / 'state/campaign.json'))


def validate_resume(root, manifest, journal):
    if (journal.get('schema') != 1 or journal.get('root') != str(root)
            or journal.get('manifest') != manifest
            or journal.get('manifest_sha256') != canonical_sha(manifest)
            or set(journal.get('regions', {})) != set(manifest['regions'])
            or journal.get('phase') not in ('PREPARED', 'COMPLETE')):
        raise ValueError('Recovery journal identity changed')
    state = read(root / 'state/campaign.json')
    for token, item in journal['regions'].items():
        rr = root / 'regions' / token
        if digest((rr / 'state/queue.json').read_bytes()) != item['queue_sha256']:
            raise ValueError('Queue changed during recovery: ' + token)
        if state['regions'][token] not in (item['before'], item['after']):
            raise ValueError('Region changed outside recovery: ' + token)
        if item['after']['status'] != ('ACTIVE' if token in state['lanes'] else 'READY'):
            raise ValueError('Region lane changed: ' + token)
        for name, relative in FILES.items():
            archived = original(item['files'][name])
            if digest(archived) != manifest['regions'][token]['files_sha256'][name]:
                raise ValueError('Archive differs from pinned manifest: ' + token)
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


def target_jobs(root, tokens, rows):
    found = []
    for row in rows:
        path = root / 'state/submissions' / (row['id'] + '.json')
        registered = read(path) if path.exists() else {}
        if registered.get('token') in tokens:
            found.append(row)
    return found


def recover(root, manifest, *, apply=False, release_sha256=None, slurm_rows=None):
    root = Path(root).resolve()
    if release_sha256 is None:
        if apply:
            raise ValueError('--apply requires --release-sha256')
        release_sha256 = manifest['release_sha256']
    validate_identity(root, manifest, release_sha256)
    journal_path = root / 'reports/operations' / (manifest['recovery_id'] + '.json')
    def inspect():
        rows = scheduler_rows() if slurm_rows is None else slurm_rows
        return controllers(root, rows), target_jobs(root, set(manifest['regions']), rows)
    if not apply:
        live, workers = inspect()
        journal = read(journal_path) if journal_path.exists() else prepare(root, manifest)
        validate_resume(root, manifest, journal)
        return dict(journal, dry_run=True, controllers=live, target_jobs=workers,
                    apply_ready=not (live or workers), audit_path=str(journal_path))
    with contextlib.ExitStack() as locks:
        for relative in ('locks/controller.lock', 'locks/submit.lock', 'locks/campaign.lock'):
            locks.enter_context(locked(root / relative))
        live, workers = inspect()
        if live or workers:
            raise ValueError('Controller or target-region jobs still active or pending')
        for token in sorted(manifest['regions']):
            for relative in ('locks/stage_failures.lock', 'locks/queue.lock'):
                locks.enter_context(locked(root / 'regions' / token / relative))
        validate_identity(root, manifest, release_sha256)
        journal = read(journal_path) if journal_path.exists() else prepare(root, manifest)
        validate_resume(root, manifest, journal)
        if journal['phase'] == 'COMPLETE':
            return journal
        if not journal_path.exists():
            atomic(journal_path, journal)
        return finish(root, journal, journal_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--release-sha256')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = recover(args.root, read(args.manifest), apply=args.apply,
                     release_sha256=args.release_sha256)
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ('regions', 'campaign_before', 'manifest')}, indent=2))
    print('regions:', len(result['regions']))


if __name__ == '__main__':
    main()
