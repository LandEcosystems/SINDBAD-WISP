"""Operational coordination shared by the legacy continuations and event pilot."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

LEGACY_ROOT = Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/wisp_eos_cube_first_v1')
GPU_SUBMIT_LOCK = LEGACY_ROOT / 'campaigns/dual120_v1/submit.lock'
GPU_NAMES = {'wisp1c-gpu', 'wisp2c-gpu', 'wispev-gpu'}


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify_release(code):
    code = Path(code)
    r = read(code / 'release.json')
    for name, expected in r['files'].items():
        if sha(code / name) != expected:
            raise ValueError('Event code release changed: ' + name)
    for name, field in [('config/deployment.json', 'config_sha256'),
                        ('manifests/selection.json', 'selection_sha256')]:
        if field in r and sha(code.parent / name) != r[field]:
            raise ValueError('Event deployment changed: ' + name)
    return r


def slurm_jobs():
    result = subprocess.run(['squeue', '-h', '-r', '-u', os.environ.get('USER', 'ybai'),
                             '-o', '%i|%j|%T'], check=True, text=True,
                            capture_output=True, timeout=30)
    return [dict(zip(('id', 'name', 'state'), row.split('|')))
            for row in result.stdout.splitlines() if row]


def gpu_busy(rows=None):
    return any(row['name'] in GPU_NAMES for row in (slurm_jobs() if rows is None else rows))


def continuation_transition(state, assigned):
    if state['phase'] != 'CLEANED' or state['active_cube'] != assigned[0]:
        raise ValueError('Only the original current cube can finish under this continuation')
    completed = state.get('completed_cubes', [])
    if completed not in ([], assigned[:1]):
        raise ValueError('Legacy campaign already advanced; do not reset it')
    return assigned[:1], assigned[1:]
