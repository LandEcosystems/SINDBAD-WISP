"""Bound a frozen cube-first deployment to an audited, finite campaign.

Scientific code and active producer/GPU scripts are imported unchanged from
RUN_ROOT/code. This wrapper replaces only controller renewal and next-cube
admission, not worker execution, normalization, inference or cleanup.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

DEFAULT_ROOT = Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/wisp_eos_cube_first_v1')
PYTHON = '/Net/Groups/BGI/work_3/sindbad/project/sindbad_wisp_eos_batch_test_v1/env/bin/python'


def read(p):
    return json.loads(Path(p).read_text())


def sha(p):
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic(p, value):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    t = p.with_name(p.name + '.tmp.' + str(os.getpid()))
    t.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    os.replace(t, p)


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def token_order(current, enrolled, original_order, completed, additional):
    if additional < 1 or current not in enrolled:
        raise ValueError('A current enrolled cube and positive additional count are required')
    rest = [t for t in original_order if t in enrolled and t != current and t not in completed]
    if len(rest) < additional:
        raise ValueError('Too few already enrolled cubes; do not silently broaden selection')
    result = [current] + rest[:additional]
    if len(set(result)) != len(result):
        raise ValueError('Duplicate campaign cube')
    return result


def check_cube(record, full_hash=False):
    path = Path(record['cube_path'])
    stat = path.stat()
    expected = record['cube_stat']
    if stat.st_size != expected['bytes'] or stat.st_mtime_ns != expected['mtime_ns']:
        raise ValueError('Accepted cube identity changed: ' + str(path))
    ap = Path(record['acceptance_path'])
    if sha(ap) != record['cube_acceptance_sha256']:
        raise ValueError('Acceptance record changed: ' + str(ap))
    a = read(ap)
    if (not a.get('passed') or a.get('errors') or a['file_sha256'] != record['cube_sha256']
            or a['path'] != str(path) or a['bytes'] != stat.st_size):
        raise ValueError('Cube acceptance is invalid: ' + str(path))
    if full_hash and sha(path) != record['cube_sha256']:
        raise ValueError('Cube checksum mismatch: ' + str(path))


def prepare(root, manifest, additional):
    root, manifest = Path(root).resolve(), Path(manifest).resolve()
    if manifest.exists():
        raise FileExistsError('Campaign is immutable: ' + str(manifest))
    q = read(root / 'state/queue.json')
    if q['phase'] not in {'BUILD', 'INPUT_VERIFY', 'PREDICT', 'MERGE', 'CLEANUP_WAIT', 'CLEANED'}:
        raise ValueError('No active cube to retain')
    enrolled = read(root / 'manifests/regions.json')
    order = read(root / 'manifests/cube_order.json')
    tokens = token_order(q['active_cube'], enrolled, order['tokens'], q.get('completed_cubes', []), additional)
    c = read(root / 'config/frozen_deployment.json')
    records = []
    for token in tokens:
        src = enrolled[token]
        keys = ['region_id', 'country', 'country_iso3', 'administrative_region', 'nearest_place',
                'center_lat', 'center_lon', 'cube_path', 'cube_sha256', 'cube_stat', 'cube_acceptance_sha256']
        rec = {k: src[k] for k in keys}
        rec.update(token=token, acceptance_path=str(Path(c['batch_root']) / 'reports/acceptance' / (src['region_id'] + '.json')))
        check_cube(rec, full_hash=True)
        records.append(rec)
        print('CHECKED', token, src['country'], src['nearest_place'], flush=True)
    m = {'schema': 1, 'campaign_id': manifest.parent.name, 'created_utc': utc(),
         'run_root': str(root), 'initial_cube': q['active_cube'], 'additional_count': additional,
         'tokens': tokens, 'records': records,
         'selection': 'Current cube first; next already enrolled and accepted cubes in the existing frozen catalog order; no outcome-based selection',
         'scientific_deployment_sha256': sha(root / 'manifests/deployment.json'),
         'engine_release_sha256': sha(root / 'code/release.json'),
         'original_order_sha256': sha(root / 'manifests/cube_order.json'),
         'catalog_sha256': order['catalog_sha256'],
         'resources': {'cpu_jobs_max': 60, 'cpu_per_job': 4, 'memory_gib_per_job': 16,
                       'producers_per_job': 2, 'all_jobs_max': 120, 'partitions': ['work', 'small', 'gpu'],
                       'gpu': 'A100', 'gpu_batch_size': 8},
         'first_decade_end_to_end_validated_at_freeze': False,
         'start': c['start'], 'end_exclusive': c['end_exclusive'],
         'verified_inputs': 'Original acceptance plus exact NetCDF SHA256 and size/mtime checked for all selected cubes'}
    atomic(manifest, m)
    fields = ['token', 'country', 'administrative_region', 'nearest_place', 'center_lat', 'center_lon', 'cube_path', 'cube_sha256']
    with manifest.with_suffix('.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
        writer.writeheader(); writer.writerows(records)
    manifest.with_suffix('.sha256').write_text(sha(manifest) + '\n')
    return m


def load_manifest(root, path):
    path = Path(path).resolve(); root = Path(root).resolve()
    m = read(path)
    if path.with_suffix('.sha256').read_text().strip() != sha(path):
        raise ValueError('Campaign manifest checksum changed')
    if m['run_root'] != str(root):
        raise ValueError('Wrong operational root')
    if len(m['tokens']) != m['additional_count'] + 1 or len(set(m['tokens'])) != len(m['tokens']):
        raise ValueError('Wrong campaign cardinality')
    if m['tokens'][0] != m['initial_cube'] or [r['token'] for r in m['records']] != m['tokens']:
        raise ValueError('Campaign records/order disagree')
    for rel, field in [('code/release.json', 'engine_release_sha256'),
                       ('manifests/deployment.json', 'scientific_deployment_sha256'),
                       ('manifests/cube_order.json', 'original_order_sha256')]:
        if sha(root / rel) != m[field]:
            raise ValueError('Deployment identity changed: ' + rel)
    return m


def engine(root):
    c = read(Path(root) / 'config/frozen_deployment.json')
    sys.path.insert(0, c['app'])
    sys.path.insert(0, str(Path(root) / 'code'))
    import runtime, controller, migrate
    config = runtime.config()
    if Path(config['root']).resolve() != Path(root).resolve():
        raise ValueError('Engine points to another deployment')
    runtime.verify_execution(config)
    runtime.install(config)
    return runtime, controller, migrate, config


def next_token(state, tokens):
    if state['phase'] != 'CLEANED' or state['active_cube'] not in tokens:
        raise ValueError('Campaign advance requires a cleaned authorized cube')
    completed = list(state.get('completed_cubes', []))
    if any(t not in tokens for t in completed):
        raise ValueError('Out-of-campaign completion found')
    if state['active_cube'] not in completed:
        completed.append(state['active_cube'])
    if completed != tokens[:len(completed)]:
        raise ValueError('Campaign order was skipped or changed')
    remaining = [t for t in tokens if t not in completed]
    return completed, remaining[0] if remaining else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'attach', 'submit-control', 'control', 'status'])
    parser.add_argument('--run-root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--additional', type=int, default=15)
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.run_root, args.manifest, args.additional)
        return
    root = args.run_root.resolve(); manifest = args.manifest.resolve()
    m = load_manifest(root, manifest)
    code_id = read(manifest.parent / 'campaign_code.json')
    for name, expected in code_id['files'].items():
        if sha(manifest.parent / name) != expected:
            raise ValueError('Campaign code changed: ' + name)
    rt, ctl, migration, c = engine(root)
    q = rt.Queue(c); fingerprint = sha(manifest)
    if args.command == 'attach':
        with rt.locked(root / 'locks/controller.lock', False):
            if any(j['kind'] == 'control' for j in ctl.jobs(c)):
                raise ValueError('Stop only the previous controller before attaching')
            with q.edit() as state:
                if state['active_cube'] != m['initial_cube'] or state.get('completed_cubes'):
                    raise ValueError('Live campaign moved since selection; do not reset work')
                existing = state.get('campaign_manifest_sha256')
                if existing and existing != fingerprint:
                    raise ValueError('Another campaign already attached')
                state.update(campaign_id=m['campaign_id'], campaign_manifest_sha256=fingerprint,
                             campaign_manifest=str(manifest), campaign_attached_at=utc())
        print('ATTACHED', m['campaign_id'], len(m['tokens']), flush=True)
        return

    def binding():
        state = q.snapshot()
        if state.get('campaign_manifest_sha256') != fingerprint or state['active_cube'] not in m['tokens']:
            raise ValueError('Live queue is outside this campaign')
        return state

    binding()
    original_submit = ctl.submit

    def submit(config, kind, dependency=None):
        binding()
        if kind != 'control':
            return original_submit(config, kind, dependency)
        current = ctl.jobs(config)
        if len(current) >= ctl.TOTAL_CAP:
            raise RuntimeError('120-job campaign limit reached')
        controls = [j for j in current if j['kind'] == 'control']
        if dependency:
            if len(controls) != 1 or controls[0]['id'] != str(dependency):
                raise ValueError('Only the current controller may renew itself')
        elif controls:
            raise ValueError('A controller is already active')
        command = ['sbatch', '--parsable', '--account=science', '--qos=normal',
                   '--job-name=wisp1c-control', '--partition=work,small,gpu', '--cpus-per-task=1',
                   '--mem=2G', '--time=12:00:00', '--chdir=' + str(root),
                   '--output=' + str(root / 'logs/campaign_control_%j.out'),
                   '--error=' + str(root / 'logs/campaign_control_%j.err')]
        if dependency:
            command += ['--dependency=afterany:' + str(dependency)]
        command += [str(manifest.parent / 'run_control.sh')]
        p = subprocess.run(command, text=True, capture_output=True, check=True, timeout=45)
        jid = p.stdout.strip().split(';')[0]
        if not jid.isdigit():
            raise ValueError('Invalid sbatch response')
        atomic(root / 'state/submissions' / (jid + '.json'),
               {'job': jid, 'name': 'wisp1c-control', 'kind': 'control', 'args': command, 'at': utc(),
                'release_sha256': sha(root / 'code/release.json'), 'campaign_manifest_sha256': fingerprint,
                'campaign_code_sha256': sha(manifest.parent / 'campaign_code.json')})
        print('SUBMITTED', jid, 'campaign-control', flush=True)
        return jid

    def advance(config):
        binding()
        # Keep the original catalog/400-unit audit intact; restrict execution here.
        catalog = Path(config['batch_root']) / 'sampling/named_production_regions.json'
        if sha(catalog) != m['catalog_sha256']:
            raise ValueError('Frozen population catalog changed')
        with q.edit() as state:
            completed, token = next_token(state, m['tokens'])
            state['completed_cubes'] = completed
            if token is None:
                state.update(phase='COMPLETE', completed_at=utc(), campaign_completed=True)
        if token is None:
            atomic(manifest.parent / 'complete.json',
                   {'passed': True, 'campaign_manifest_sha256': fingerprint, 'completed_cubes': completed, 'at': utc()})
            return {'complete': True, 'completed_cubes': len(completed)}
        rec = next(r for r in m['records'] if r['token'] == token)
        check_cube(rec, full_hash=True)
        result = migration.admit_cube(config, token)
        atomic(root / 'reports/next_cube_admitted.json',
               {'token': token, 'at': utc(), 'campaign_manifest_sha256': fingerprint, 'capacity': result})
        return result

    ctl.submit = submit
    migration.activate_next = advance
    if args.command == 'submit-control':
        submit(c, 'control')
    elif args.command == 'status':
        ctl.status(c, ctl.jobs(c))
    else:
        try:
            ctl.control(c)
        except Exception as exc:
            atomic(manifest.parent / 'controller_error.json', {'error': str(exc), 'at': utc()})
            raise


if __name__ == '__main__':
    main()
