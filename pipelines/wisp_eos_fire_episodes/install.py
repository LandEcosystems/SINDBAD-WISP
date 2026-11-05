"""Stage an immutable event release, then hand over only the legacy controllers.

Staging never submits or cancels anything. --attach requires the two previously
identified legacy controller jobs to have terminated. Producer leases are retained.
"""
import argparse
import copy
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from coordination import LEGACY_ROOT, read, sha, slurm_jobs
from selection import validate_manifest

ROOT = LEGACY_ROOT.parent / 'wisp_eos_fire_episodes_v1'
SCRATCH = Path('/Net/Groups/BGI/tscratch/ybai/wisp_eos_fire_episodes_v1')
PYTHON = '/Net/Groups/BGI/work_3/sindbad/project/sindbad_wisp_eos_batch_test_v1/env/bin/python'


def atomic(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n'); os.replace(tmp, path)


def choose_parity(selection):
    result = {}
    for token, region in selection['regions'].items():
        tasks = sorted((t for t in selection['tasks'].values() if t['region'] == token),
                       key=lambda t: t['dates'][0])
        # First prediction, first block's trailing boundary, final selected day,
        # then a cross-month boundary (or an interior block boundary).
        candidates = [tasks[0]['dates'][0], tasks[0]['dates'][-1], tasks[-1]['dates'][-1]]
        cross = [d for t in tasks for d in t['dates'][1:]
                 if d[8:10] == '01' and d[:7] != t['dates'][0][:7]]
        candidates += cross + [t['dates'][0] for t in tasks[1:]]
        result[token] = list(dict.fromkeys(candidates))[:4]
    return result


def launcher(template, command, private_cache):
    text = template.replace('/Net/Groups/BGI/tscratch/ybai/wisp_eos_batch1_v1/cache', str(private_cache))
    return text[:text.rfind('exec ')] + 'exec ' + command + '\n'


def stage(source, selection_dir):
    if (ROOT / 'code/release.json').exists():
        raise FileExistsError('An immutable event release already exists; do not overwrite live code')
    selection = read(selection_dir / 'selection.json'); validate_manifest(selection)
    if selection['summary']['selected_days'] != 390 or len(selection['tasks']) != 68:
        raise ValueError('Unexpected approved experiment size')
    for folder in ['code', 'config', 'manifests', 'logs', 'reports', 'state', 'locks', 'qa', 'outputs', 'inputs']:
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    if any(SCRATCH.iterdir()):
        raise ValueError('New isolated scratch must be empty at first deployment')
    c = read(LEGACY_ROOT / 'config/frozen_deployment.json')
    original_scratch = Path(c['scratch'])
    c.pop('config_path', None)
    c.update(root=str(ROOT), scratch=str(SCRATCH), scratch_budget_bytes=128 * 2**30,
             shared_cache_reserve_bytes=8 * 2**30)
    atomic(ROOT / 'config/deployment.json', c)
    for name in ['deployment.json', 'source_files.json']:
        shutil.copy2(LEGACY_ROOT / 'manifests' / name, ROOT / 'manifests' / name)
    for f in selection_dir.iterdir():
        if f.is_file(): shutil.copy2(f, ROOT / 'manifests' / f.name)
    regions = read(LEGACY_ROOT / 'manifests/regions.json')
    regions = {token: regions[token] for token in selection['regions']}
    for token, r in regions.items():
        if r != selection['regions'][token]['cube_record']:
            raise ValueError('Selection cube registry changed')
        shutil.copytree(LEGACY_ROOT / 'qa' / token, ROOT / 'qa' / token, dirs_exist_ok=True)
    atomic(ROOT / 'manifests/regions.json', regions)
    parity = choose_parity(selection)
    atomic(ROOT / 'manifests/parity_dates.json', parity)
    operations = dict(full_roots=[str(LEGACY_ROOT), str(LEGACY_ROOT / 'lanes/lane_b')],
                      selection_identity=selection['selection_sha256'],
                      cpu_jobs_max_including_controller=32, producers_per_job=2,
                      partitions=['work', 'small', 'gpu'], gpu_jobs_shared_max=1,
                      retrospective_event_conditioned=True, annual_false_positive_evaluation=False)
    atomic(ROOT / 'manifests/operations.json', operations)
    tasks = copy.deepcopy(selection['tasks'])
    for token_index, token in enumerate(['T000830', 'T000001']):
        own = sorted((k for k,t in tasks.items() if t['region'] == token), key=lambda k: tasks[k]['dates'][0])
        for i, key in enumerate(own):
            tasks[key].update(status='queued', input_status='queued', prediction_status='queued',
                              priority=i*2+token_index, reserved=0,
                              parity_dates=[d for d in parity[token] if d in tasks[key]['dates']])
    atomic(ROOT / 'state/queue.json', dict(phase='BUILD', tasks=tasks,
           selection_sha256=selection['selection_sha256'], quarantine_bytes=0,
           created=dt.datetime.now(dt.timezone.utc).isoformat()))
    # Copy only immutable, checked index files, under the original writer's lock.
    index_dest = SCRATCH / 'cache/fire_index'; index_dest.mkdir(parents=True)
    copied = {}
    for receipt in sorted((original_scratch / 'cache/fire_index').glob('*.json')):
        with (LEGACY_ROOT / 'locks' / ('index_' + receipt.stem + '.lock')).open('a+') as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX)
            r = read(receipt); data = receipt.with_suffix('.parquet')
            if data.stat().st_size != r['index_bytes'] or sha(data) != r['index_sha256']:
                raise ValueError('Original fire index is not audited: ' + str(data))
            for f in [receipt, data]:
                shutil.copy2(f, index_dest / f.name)
                if sha(f) != sha(index_dest / f.name):
                    raise ValueError('Private index copy changed')
                copied[f.name] = sha(f)
            atomic(ROOT / 'manifests/fire_indices' / receipt.name, r)
            fcntl.lockf(lock, fcntl.LOCK_UN)
    if len(copied) != 24:
        raise ValueError('Expected 2013–2023 plus NOAA-20 index pairs')
    atomic(ROOT / 'reports/fire_index_copy.json', dict(passed=True, files=copied,
           original_cache=str(original_scratch / 'cache/fire_index'), private_cache=str(index_dest)))
    for f in source.iterdir():
        if f.suffix in {'.py', '.md'}:
            shutil.copy2(f, ROOT / 'code' / f.name)
    template = (LEGACY_ROOT / 'code/job.sh').read_text()
    commands = {'job.sh': PYTHON + ' -u ' + str(ROOT / 'code/controller.py') + ' "$1"'}
    for lane in ['a', 'b']:
        commands['continue_' + lane + '.sh'] = (PYTHON + ' -u ' + str(ROOT / 'code/continuation.py') +
            ' control --manifest ' + str(LEGACY_ROOT / 'campaigns/dual120_v1/manifest.json') + ' --lane ' + lane)
    for name, command in commands.items():
        target = ROOT / 'code' / name
        # Legacy continuation workers retain their original caches; only the
        # controller process itself uses the small event operational cache.
        target.write_text(launcher(template, command, SCRATCH / 'cache')); target.chmod(0o775)
    commit = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                            check=True, capture_output=True, text=True).stdout.strip()
    release = dict(created=dt.datetime.now(dt.timezone.utc).isoformat(), git_commit=commit,
                   scientific_deployment_sha256=sha(ROOT / 'manifests/deployment.json'),
                   config_sha256=sha(ROOT / 'config/deployment.json'),
                   selection_sha256=sha(ROOT / 'manifests/selection.json'),
                   files={f.name: sha(f) for f in sorted((ROOT / 'code').iterdir()) if f.is_file()})
    atomic(ROOT / 'code/release.json', release)
    print('STAGED', ROOT, flush=True)


def attach():
    from coordination import verify_release
    verify_release(ROOT / 'code')
    current = slurm_jobs()
    if any(j['name'] in {'wisp1c-control', 'wisp2c-control'} for j in current):
        raise ValueError('Stop only the two legacy controllers before attaching; retain workers')
    # The two old queue locks protect live producers. Do not change any task.
    report = {}
    for lane, root, token in [('a', LEGACY_ROOT, 'T000830'),
                              ('b', LEGACY_ROOT / 'lanes/lane_b', 'T000001')]:
        with (root / 'locks/controller.lock').open('a+') as controller_lock:
            fcntl.lockf(controller_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (root / 'locks/queue.lock').open('a+') as queue_lock:
                fcntl.lockf(queue_lock, fcntl.LOCK_EX)
                path = root / 'state/queue.json'; state = read(path)
                if state['active_cube'] != token or state.get('completed_cubes'):
                    raise ValueError('Legacy lane advanced; do not reset')
                before = copy.deepcopy(state['tasks'])
                atomic(ROOT / 'reports' / ('legacy_queue_' + lane + '_before_handover.json'), state)
                state.update(event_continuation_release_sha256=sha(ROOT / 'code/release.json'),
                             defer_after_current=True)
                atomic(path, state)
                after = read(path)
                if after['tasks'] != before:
                    raise ValueError('Live tasks or leases changed during handover')
                report[lane] = dict(active_cube=token, retained_leases=sum(bool(t.get('lease')) for t in before.values()),
                                    task_payload_unchanged=True, workers_cancelled=False)
                fcntl.lockf(queue_lock, fcntl.LOCK_UN)
            fcntl.lockf(controller_lock, fcntl.LOCK_UN)
    atomic(ROOT / 'reports/handover.json', dict(passed=True, lanes=report,
           deferred_cubes=14, original_campaign_complete=False,
           at=dt.datetime.now(dt.timezone.utc).isoformat()))
    print('ATTACHED', ROOT, flush=True)


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('command', choices=['stage','attach'])
    p.add_argument('--source', type=Path, default=Path(__file__).resolve().parent)
    p.add_argument('--selection-dir', type=Path, default=Path(__file__).resolve().parent / 'selection_frozen_v1')
    a=p.parse_args()
    stage(a.source.resolve(), a.selection_dir.resolve()) if a.command=='stage' else attach()
