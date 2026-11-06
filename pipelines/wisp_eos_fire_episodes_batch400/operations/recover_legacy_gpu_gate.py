"""Restore an existing, identity-matched GPU qualification receipt; never fabricate one."""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


HARDWARE = 'NVIDIA A100-SXM4-80GB'
SCIENTIFIC_ID = 'b71168043d75105c9cb436dd64c32d8311c4ab4de635366af445e5976834623f'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def atomic_bytes(path, data, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # Atomic no-clobber publication: a concurrent writer can never be overwritten.
            os.link(name, str(path))
        else:
            os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inspect(parent, lane, receipt_sha256):
    parent, lane = Path(parent).resolve(), Path(lane).resolve()
    if lane != parent / 'lanes/lane_b':
        raise ValueError('Only the existing lane_b migration is in scope')
    source = parent / 'reports/mixed_batch_A100.json'
    target = lane / 'reports/mixed_batch_A100.json'
    raw = source.read_bytes()
    actual_sha = digest(raw)
    if not receipt_sha256 or actual_sha != receipt_sha256:
        raise ValueError('Existing qualification receipt SHA256 mismatch')
    qualification = json.loads(raw)
    source_manifest = read(parent / 'manifests/deployment.json')
    target_manifest = read(lane / 'manifests/deployment.json')
    if source_manifest != target_manifest:
        raise ValueError('Scientific deployment manifests differ')
    if source_manifest.get('fingerprint') != SCIENTIFIC_ID:
        raise ValueError('Unexpected scientific identity')
    if (qualification.get('passed') is not True or
            qualification.get('deployment') != SCIENTIFIC_ID or
            qualification.get('hardware') != HARDWARE or
            len(qualification.get('members', [])) != 8 or
            len(qualification.get('comparisons_to_b1', [])) != 8 or
            len(qualification.get('b1_repeat', [])) != 8):
        raise ValueError('Source receipt is not a complete matching A100 batch qualification')
    environments = list((lane / 'reports').glob('gpu_environment_*.json'))
    if not environments:
        raise ValueError('No actual lane GPU environment to check')
    environment_path = max(environments, key=lambda p: p.stat().st_mtime_ns)
    environment = read(environment_path)
    if (environment.get('hardware') != HARDWARE or
            environment.get('model', {}).get('checkpoint_sha256') != source_manifest.get('checkpoint_sha256') or
            environment.get('model', {}).get('model_commit') != source_manifest.get('model_commit') or
            environment.get('environment', {}).get('packages') != source_manifest.get('packages')):
        raise ValueError('Actual lane GPU environment does not match qualification deployment')
    if target.exists() and target.read_bytes() != raw:
        raise ValueError('Refusing to overwrite a different existing target receipt')
    return raw, {
        'source': str(source), 'target': str(target),
        'source_sha256': actual_sha, 'target_sha256': actual_sha,
        'scientific_id': SCIENTIFIC_ID, 'hardware': HARDWARE,
        'source_manifest_sha256': digest((parent / 'manifests/deployment.json').read_bytes()),
        'target_manifest_sha256': digest((lane / 'manifests/deployment.json').read_bytes()),
        'actual_gpu_environment': str(environment_path),
        'actual_gpu_environment_sha256': digest(environment_path.read_bytes()),
        'original_qualified_at': qualification.get('completed'),
        'original_members': qualification['members'],
        'operation': 'reuse_existing_qualification_receipt_without_numeric_or_queue_changes',
    }


def recover(parent, lane, audit, receipt_sha256, apply=False):
    raw, evidence = inspect(parent, lane, receipt_sha256)
    if not apply:
        return dict(evidence, dry_run=True, target_already_present=Path(evidence['target']).exists())
    audit = Path(audit)
    audit.parent.mkdir(parents=True, exist_ok=True)
    with (audit.parent / (audit.name + '.lock')).open('a+') as lock:
        fcntl.lockf(lock, fcntl.LOCK_EX)
        raw, evidence = inspect(parent, lane, receipt_sha256)
        if audit.exists():
            prior = read(audit)
            for key in ('source', 'target', 'source_sha256', 'target_sha256', 'scientific_id'):
                if prior.get(key) != evidence[key]:
                    raise ValueError('Recovery audit belongs to a different receipt or target')
            if prior.get('phase') == 'COMPLETE':
                if not Path(evidence['target']).exists():
                    raise ValueError('Completed recovery target has disappeared')
                return prior
        else:
            prior = dict(evidence, phase='PREPARED', prepared_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            atomic_bytes(audit, (json.dumps(prior, indent=2) + '\n').encode())
        target = Path(evidence['target'])
        if not target.exists():
            try:
                atomic_bytes(target, raw, exclusive=True)
            except FileExistsError:
                if target.read_bytes() != raw:
                    raise ValueError('Concurrent different qualification receipt refused')
        if target.read_bytes() != raw:
            raise ValueError('Published receipt bytes failed verification')
        prior.update(phase='COMPLETE', completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        atomic_bytes(audit, (json.dumps(prior, indent=2) + '\n').encode())
        return prior


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--parent', required=True)
    p.add_argument('--lane', required=True)
    p.add_argument('--audit', required=True)
    p.add_argument('--receipt-sha256', required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    print(json.dumps(recover(args.parent, args.lane, args.audit, args.receipt_sha256, args.apply), indent=2))
