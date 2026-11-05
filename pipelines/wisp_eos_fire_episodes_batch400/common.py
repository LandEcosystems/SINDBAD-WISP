"""Small durable operational primitives; no model or preprocessing changes."""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path

BASE = Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube')
DEFAULT_ROOT = BASE / 'wisp_eos_fire_episodes_batch1_v2'
DEFAULT_SCRATCH = Path('/Net/Groups/BGI/tscratch/ybai/wisp_eos_fire_episodes_batch1_v2')
LEGACY = BASE / 'wisp_eos_cube_first_v1'
PILOT = BASE / 'wisp_eos_fire_episodes_v1'
PYTHON = '/Net/Groups/BGI/work_3/sindbad/project/sindbad_wisp_eos_batch_test_v1/env/bin/python'
GIB = 2**30
PREFIX = 'w4e-'
TERMINAL = {'COMPLETE', 'NO_OBSERVED_FIRE', 'REUSED'}

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def read(path):
    return json.loads(Path(path).read_text())

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8*1024*1024), b''):
            h.update(block)
    return h.hexdigest()

def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, sort_keys=True, indent=2, allow_nan=False)
        f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)

@contextlib.contextmanager
def locked(path, blocking=True):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as f:
        fcntl.lockf(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try: yield
        finally: fcntl.lockf(f, fcntl.LOCK_UN)

@contextlib.contextmanager
def state_edit(root):
    root = Path(root)
    with locked(root/'locks/campaign.lock'):
        state = read(root/'state/campaign.json')
        yield state
        atomic(root/'state/campaign.json', state)

def verify_release(root):
    root = Path(root)
    r = read(root/'code/release.json')
    for name, expected in r['files'].items():
        path = (root/'code'/name).resolve()
        if not path.is_relative_to((root/'code').resolve()) or sha(path) != expected:
            raise ValueError('Immutable release changed: '+name)
    if sha(root/'config/campaign.json') != r['campaign_config_sha256']:
        raise ValueError('Campaign configuration changed')
    if sha(root/'manifests/named_production_regions.json') != r['catalog_sha256']:
        raise ValueError('Frozen 400-region catalog changed')
    for name,expected in r.get('source_files',{}).items():
        p=(root/name).resolve()
        if not p.is_relative_to(root.resolve()) or sha(p)!=expected:
            raise ValueError('Frozen campaign source registry changed: '+name)
    return r

def region_config(root, token):
    import re
    if not re.fullmatch(r'T\d{6}', token): raise ValueError('Invalid token')
    c = read(Path(root)/'regions'/token/'config/deployment.json')
    if Path(c['root']).resolve() != (Path(root)/'regions'/token).resolve():
        raise ValueError('Region root mismatch')
    return c

def producer_target(pending_chunks, running_producers, cap=30):
    """Pending includes leased work, so do not oversubmit a nearly finished wave."""
    import math
    return max(0, min(cap, math.ceil(pending_chunks/2))-running_producers)

def ready_order(regions):
    return sorted((t for t, r in regions.items() if r['status']=='READY'),
                  key=lambda t: (-regions[t]['selected_days'], t))

def legacy_gpu_handover_ready(legacy=LEGACY):
    """Legacy code does not recognize our new job name: await its final GPU use."""
    for r in [Path(legacy), Path(legacy)/'lanes/lane_b']:
        s = read(r/'state/queue.json')
        if s.get('phase') != 'COMPLETE' or not (s.get('campaign_paused') or s.get('defer_after_current')):
            return False
    return True
