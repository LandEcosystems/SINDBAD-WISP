"""Stage a second operational lane without modifying or stopping active workers.

After staging, stop ONLY the old campaign controller, then run --attach. Existing
Granada task leases remain untouched. No input tensor is copied or removed.
"""
import argparse
import copy
import os
from pathlib import Path
import shutil
from campaign import read, sha, atomic, utc, engine, check_cube
from dual_campaign import partitions


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', required=True, type=Path)
    p.add_argument('--original-manifest', required=True, type=Path)
    p.add_argument('--attach', action='store_true')
    args = p.parse_args()
    a = args.root.resolve(); b = a / 'lanes/lane_b'
    out = a / 'campaigns/dual120_v1'
    mp = out / 'manifest.json'
    original = read(args.original_manifest)
    lanes = partitions(original['tokens'])
    rt, ctl, migration, config = engine(a)
    if args.attach:
        m = read(mp)
        # This is only A's old controller lock; active month writers keep running.
        with rt.locked(a / 'locks/controller.lock', False):
            if any(j['kind'] == 'control' for j in ctl.jobs(config)):
                raise ValueError('Stop the original controller before attaching')
            with rt.Queue(config).edit() as q:
                if q['active_cube'] != lanes['a'][0] or q['phase'] != 'BUILD' or q.get('completed_cubes'):
                    raise ValueError('Granada advanced; recheck rather than reset')
                atomic(out / 'queue_a_before_attach.json', q)
                owners_before = {k: copy.deepcopy(t) for k, t in q['tasks'].items() if t.get('lease')}
                if any(t['region'] in lanes['b'] for t in q['tasks'].values() if t.get('lease')):
                    raise ValueError('Lane B already has an owner in A')
                q.update(dual_manifest_sha256=sha(mp), dual_lane='a', dual_attached_at=utc())
                if owners_before != {k: t for k, t in q['tasks'].items() if t.get('lease')}:
                    raise ValueError('Live leases changed during attach')
                atomic(out / 'handover.json', dict(passed=True, retained_leases=len(owners_before),
                       active_cube=q['active_cube'], at=utc(), old_workers_interrupted=False))
            # B has no workers yet; independent state and locks.
            bp = b / 'state/queue.json'; q = read(bp)
            if any(t.get('lease') for t in q['tasks'].values()):
                raise ValueError('B already running')
            q.update(dual_manifest_sha256=sha(mp), dual_lane='b')
            atomic(bp, q)
        print('ATTACHED', mp, flush=True)
        return
    if mp.exists():
        raise FileExistsError('Immutable dual deployment already staged')
    check_cube(original['records'][1], full_hash=True)
    out.mkdir(parents=True, exist_ok=True)
    for folder in ['code', 'config', 'manifests', 'qa', 'logs', 'reports', 'state', 'locks']:
        (b / folder).mkdir(parents=True, exist_ok=True)
    release = read(a / 'code/release.json')
    for name, expected in release['files'].items():
        if sha(a / 'code' / name) != expected:
            raise ValueError('Existing engine changed')
        shutil.copy2(a / 'code' / name, b / 'code' / name)
    launcher = b / 'code/job.sh'
    launcher.write_text(launcher.read_text().replace(str(a / 'code/controller.py'), str(b / 'code/controller.py')))
    release['files']['job.sh'] = sha(launcher)
    release.update(created=utc(), parent_release_sha256=sha(a / 'code/release.json'),
                   change='Only job.sh operational controller path; Python worker code unchanged')
    atomic(b / 'code/release.json', release)
    for f in (a / 'config').glob('*.json'):
        shutil.copy2(f, b / 'config' / f.name)
    # Source/region metadata is small and copied; tensors stay in shared scratch.
    for f in (a / 'manifests').glob('*.json'):
        shutil.copy2(f, b / 'manifests' / f.name)
    for token in lanes['b']:
        source = a / 'qa' / token
        if source.exists():
            shutil.copytree(source, b / 'qa' / token, dirs_exist_ok=True)
    for folder in ['fire_indices']:
        if (a / 'manifests' / folder).exists():
            shutil.copytree(a / 'manifests' / folder, b / 'manifests' / folder, dirs_exist_ok=True)
    if (a / 'state/backend').exists():
        shutil.copytree(a / 'state/backend', b / 'state/backend', dirs_exist_ok=True)
    # Shared index cache must use exactly the same lock inodes as active workers.
    lockfiles = list((a / 'locks').glob('index_*.lock'))
    if not lockfiles:
        raise ValueError('Expected existing shared fire-index locks')
    for f in lockfiles:
        dest = b / 'locks' / f.name
        if not dest.exists():
            dest.symlink_to(f)
    atomic(out / 'shared_index_locks.json', {f.name: str(f) for f in lockfiles})
    with rt.Queue(config).edit() as old:
        if any(t.get('lease') and t['region'] in lanes['b'] for t in old['tasks'].values()):
            raise ValueError('Cannot transfer owned months')
        b_tasks = {}
        for token in lanes['b']:
            for key, task in migration.month_tasks(config, token).items():
                src = a / 'manifests/tasks' / key
                for name in ['registration.json', 'input_complete.json', 'source_files.json']:
                    if (src / name).exists():
                        dest = b / 'manifests/tasks' / key / name
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src / name, dest)
                task['reserved'] = 0
                b_tasks[key] = task
        atomic(b / 'state/queue.json', dict(tasks=b_tasks, active_cube=lanes['b'][0], phase='COMPLETE',
               completed_cubes=[], quarantine_bytes=0, staged_not_started=True))
    # Inherit the validated dual-process resource pilot, not a fictitious new run.
    pilot = read(a / 'reports/pilot_passed.json')
    if not pilot['passed'] or pilot['release_sha256'] != sha(a / 'code/release.json'):
        raise ValueError('Original pilot missing or changed')
    for name in release['files']:
        if name != 'job.sh' and sha(a / 'code' / name) != sha(b / 'code' / name):
            raise ValueError('Cannot inherit pilot across Python code changes')
    atomic(out / 'inherited_pilot_original.json', pilot)
    pilot.update(release_sha256=sha(b / 'code/release.json'), inherited_from=str(a / 'reports/pilot_passed.json'),
                 inherited_reason='Identical Python code, CPU/memory/thread settings; only launcher path changed',
                 second_cube_already_validated=False)
    atomic(b / 'reports/pilot_passed.json', pilot)
    for name in ['campaign.py', 'dual_campaign.py']:
        shutil.copy2(Path(__file__).parent / name, out / name)
    for key in lanes:
        text = (a / 'code/job.sh').read_text()
        pos = text.rfind('exec ')
        text = text[:pos] + ('exec /Net/Groups/BGI/work_3/sindbad/project/sindbad_wisp_eos_batch_test_v1/env/bin/python -u '
                            + str(out / 'dual_campaign.py') + ' control --manifest ' + str(mp) + ' --lane ' + key + '\n')
        target = out / ('control_' + key + '.sh'); target.write_text(text); target.chmod(0o775)
    m = dict(schema=1, created=utc(), original_manifest=str(args.original_manifest.resolve()),
             original_manifest_sha256=sha(args.original_manifest), original_tokens=original['tokens'],
             cpu_jobs_max=120, producers_per_job=2, scratch_budget_bytes=2 * 2**40,
             gpu_jobs_max=1, control_and_validation_jobs_separate=True,
             lanes={k: dict(root=str(r), tokens=lanes[k], release_sha256=sha(r / 'code/release.json'))
                    for k, r in [('a', a), ('b', b)]},
             wrapper_files={name: sha(out / name) for name in ['campaign.py', 'dual_campaign.py', 'control_a.sh', 'control_b.sh']})
    atomic(mp, m); mp.with_suffix('.sha256').write_text(sha(mp) + '\n')
    print('STAGED', mp, flush=True)


if __name__ == '__main__':
    main()
