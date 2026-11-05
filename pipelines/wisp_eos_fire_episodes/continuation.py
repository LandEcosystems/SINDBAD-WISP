"""Continue only the two active decades; defer the other fourteen cubes.

A separate controller generation imports the unchanged full-decade worker engine.
It retains task leases and scientific identities, and shares the GPU submission
mutex with the event experiment. Original sixteen-cube completion is never claimed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
# Import the immutable legacy campaign helpers without editing the legacy release.
LEGACY_ROOT = Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/wisp_eos_cube_first_v1')
sys.path.insert(0, str(LEGACY_ROOT / 'campaigns/dual120_v1'))
from campaign import read, sha, atomic, utc, check_cube, engine, next_token
sys.path.insert(0, str(Path(__file__).resolve().parent))
from coordination import gpu_busy, verify_release, continuation_transition


def partitions(tokens):
    if len(tokens) != len(set(tokens)) or len(tokens) != 16:
        raise ValueError('Expected the original 16 unique cubes')
    return {'a': tokens[::2], 'b': tokens[1::2]}


def keep_allocation(path, lane, b_tokens):
    token = Path(path).name.split('_')[0]
    return (token in b_tokens) if lane == 'b' else (token not in b_tokens)


def validate_lanes(manifest):
    split = partitions(manifest['original_tokens'])
    if {k: v['tokens'] for k, v in manifest['lanes'].items()} != split:
        raise ValueError('Lane ownership changed')
    if len({v['root'] for v in manifest['lanes'].values()}) != 2:
        raise ValueError('Lanes must have independent roots')
    if manifest['cpu_jobs_max'] != 120 or manifest['scratch_budget_bytes'] != 2 * 2**40:
        raise ValueError('Unauthorized resource settings')
    return split


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['control', 'submit-control', 'admit', 'status'])
    p.add_argument('--manifest', required=True, type=Path)
    p.add_argument('--lane', required=True, choices=['a', 'b'])
    args = p.parse_args()
    verify_release(Path(__file__).resolve().parent)
    mp = args.manifest.resolve(); m = read(mp); split = validate_lanes(m)
    if sha(mp) != mp.with_suffix('.sha256').read_text().strip():
        raise ValueError('Dual manifest changed')
    for name, expected in m['wrapper_files'].items():
        if sha(mp.parent / name) != expected:
            raise ValueError('Wrapper changed: ' + name)
    for lane in m['lanes'].values():
        if sha(Path(lane['root']) / 'code/release.json') != lane['release_sha256']:
            raise ValueError('Lane engine release changed')
    original = read(m['original_manifest'])
    if sha(m['original_manifest']) != m['original_manifest_sha256']:
        raise ValueError('Original campaign changed')
    root = Path(m['lanes'][args.lane]['root'])
    rt, ctl, migration, c = engine(root)
    if c['scratch_budget_bytes'] != 2**40:
        raise ValueError('Each lane must retain its original 1 TiB ceiling')
    q = rt.Queue(c)
    shared = mp.parent

    def all_jobs():
        rows = subprocess.run(['squeue', '-h', '-r', '-u', 'ybai', '-o', '%i|%j|%T'],
                              check=True, text=True, capture_output=True, timeout=30).stdout
        result = []
        for row in rows.splitlines():
            jid, name, state = row.split('|')
            if name.startswith('wispb1-'):
                raise ValueError('Legacy WISP job remains active: ' + jid)
            if not name.startswith(('wisp1c-', 'wisp2c-')):
                continue
            matches = []
            for key, lane in m['lanes'].items():
                f = Path(lane['root']) / 'state/submissions' / (jid + '.json')
                if f.exists():
                    r = read(f)
                    if r['job'] != jid or r['name'] != name:
                        raise ValueError('Job identity mismatch: ' + jid)
                    matches.append((key, r))
            if not matches:
                # Another controller may be between sbatch and its atomic registration.
                for attempt in range(20):
                    time.sleep(0.25)
                    for key, lane in m['lanes'].items():
                        f = Path(lane['root']) / 'state/submissions' / (jid + '.json')
                        if f.exists():
                            r = read(f)
                            if r['job'] != jid or r['name'] != name:
                                raise ValueError('Job identity mismatch: ' + jid)
                            matches.append((key, r))
                    if matches:
                        break
            if len(matches) != 1:
                raise ValueError('Unregistered/ambiguous WISP job: ' + jid)
            key, r = matches[0]
            result.append(dict(id=jid, name=name, state=state, lane=key, kind=r['kind']))
        return result

    def jobs(config):
        return [j for j in all_jobs() if j['lane'] == args.lane]

    old_submit = ctl.submit
    ctl.jobs = jobs
    if args.lane == 'b':
        ctl.PREFIX = 'wisp2c-'

    def submit(config, kind, dependency=None):
        with rt.locked(shared / 'submit.lock'):
            active = all_jobs()
            if kind == 'cpu' and sum(j['kind'] == 'cpu' for j in active) >= 120:
                return None
            if kind == 'gpu' and gpu_busy():
                return None  # Retry later; never claim a second GPU.
            if kind != 'control':
                return old_submit(config, kind, dependency)
            controls = [j for j in active if j['lane'] == args.lane and j['kind'] == 'control']
            if controls and (not dependency or len(controls) != 1 or controls[0]['id'] != str(dependency)):
                raise ValueError('Lane controller already active')
            name = ctl.PREFIX + 'control'
            command = ['sbatch', '--parsable', '--account=science', '--qos=normal',
                       '--job-name=' + name, '--partition=work,small,gpu', '--cpus-per-task=1',
                       '--mem=2G', '--time=12:00:00', '--chdir=' + str(root),
                       '--output=' + str(root / 'logs/dual_control_%j.out'),
                       '--error=' + str(root / 'logs/dual_control_%j.err')]
            if dependency:
                command += ['--dependency=afterany:' + str(dependency)]
            command += [str(Path(__file__).resolve().parent / ('continue_' + args.lane + '.sh'))]
            response = subprocess.run(command, check=True, text=True, capture_output=True, timeout=45)
            jid = response.stdout.strip().split(';')[0]
            if not jid.isdigit():
                raise ValueError('Invalid submission response')
            atomic(root / 'state/submissions' / (jid + '.json'), dict(job=jid, name=name,
                   kind='control', args=command, at=utc(), dual_manifest_sha256=sha(mp)))
            print('SUBMITTED', jid, args.lane, kind, flush=True)
            return jid

    def admit(config, token):
        if token != split[args.lane][0]:
            raise ValueError('Subsequent legacy cubes are paused by the event experiment')
        if token not in split[args.lane]:
            raise ValueError('Cross-lane cube admission prohibited')
        rec = next(r for r in original['records'] if r['token'] == token)
        check_cube(rec, full_hash=True)
        with rt.locked(shared / 'admission.lock'):
            scratch = Path(config['scratch'])
            paths = [v for v in (scratch / 'inputs').iterdir() if keep_allocation(v, args.lane, split['b'])]
            allocations = migration.allocated(paths)
            quarantine = (migration.allocated([scratch / 'quarantine']).get(str(scratch / 'quarantine'), 0)
                          if args.lane == 'a' else 0)
            with q.edit() as state:
                if any(t.get('lease') for t in state['tasks'].values()):
                    raise ValueError('Cannot advance a lane with active owners')
                if state.get('phase') not in {'CLEANED', 'COMPLETE'}:
                    raise ValueError('Previous cube must finish cleanup')
                for key, task in migration.month_tasks(config, token).items():
                    state['tasks'].setdefault(key, task)
                    state['tasks'][key]['priority'] = task['priority']
                state.update(active_cube=token, phase='BUILD', admitted_at=utc())
                for name in ['cleanup_authorized', 'cleanup_not_before_epoch', 'final_receipt_sha256',
                             'inputs_verified_at', 'cleaned_at']:
                    state.pop(name, None)
                report = migration.capacity(config, state, allocations, quarantine)
                atomic(root / 'reports/admission_capacity.json', report)
                if not report['passed']:
                    raise ValueError('Lane capacity exceeded; global budget is not permission to overfill one lane')
        return report

    def advance(config):
        with q.edit() as state:
            completed, deferred = continuation_transition(state, split[args.lane])
            state.update(completed_cubes=completed, phase='COMPLETE',
                         campaign_completed=False, campaign_paused=True,
                         deferred_cubes=deferred, stopped_after_current_at=utc())
        atomic(Path(__file__).resolve().parent.parent / 'reports' /
               ('legacy_lane_' + args.lane + '_paused.json'),
               dict(passed=True, completed=completed, deferred=deferred,
                    original_campaign_complete=False, at=utc()))

    ctl.submit = submit
    migration.activate_next = advance
    state = q.snapshot()
    if state.get('dual_manifest_sha256') != sha(mp):
        raise ValueError('Lane not attached to this dual manifest')
    if state['active_cube'] not in split[args.lane]:
        raise ValueError('Active cube outside lane')
    if args.command == 'admit':
        print(json.dumps(admit(c, state['active_cube'])), flush=True)
    elif args.command == 'submit-control':
        submit(c, 'control')
    elif args.command == 'status':
        ctl.status(c, jobs(c))
        print(json.dumps(all_jobs()), flush=True)
    else:
        try:
            ctl.control(c)
        except Exception as exc:
            atomic(shared / ('error_' + args.lane + '.json'), dict(error=str(exc), at=utc()))
            raise


if __name__ == '__main__':
    main()
