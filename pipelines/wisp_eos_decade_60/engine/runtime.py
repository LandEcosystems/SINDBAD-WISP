"""Cube-first operational state. Frozen numerical modules remain read-only."""
import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
import uuid

from wisp_stream import common
from wisp_batch.common import atomic_json, file_hash

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OLD_ROOT = Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/shift_v3_60km/wisp_eos_q10_v1')
CONFIG = OLD_ROOT / 'config/deployment.json'
_original_verify = common.verify_code
_claims = {}
_held = set()
STOP = False
STARTED = time.monotonic()


class StopAtCheckpoint(BaseException):
    pass


def now():
    return common.now()


def read_json(path):
    return json.loads(Path(path).read_text())


@contextlib.contextmanager
def locked(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ident = (os.getpid(), str(path.absolute()))
    if ident in _held:
        raise RuntimeError('Nested POSIX lock is unsafe: ' + str(path))
    with path.open('a+') as f:
        fcntl.lockf(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        _held.add(ident)
        try:
            yield
        finally:
            _held.remove(ident)
            fcntl.lockf(f, fcntl.LOCK_UN)


def verify_execution(c):
    original = common.config(CONFIG)
    d = _original_verify(original)
    expected = dict(original, root=str(ROOT))
    if c != expected:
        raise ValueError('Only operational output root may differ from frozen configuration')
    if read_json(ROOT / 'manifests/deployment.json') != d:
        raise ValueError('Frozen scientific deployment identity differs')
    release = read_json(HERE / 'release.json')
    for name, digest in release['files'].items():
        if file_hash(HERE / name) != digest:
            raise ValueError('Operational release changed: ' + name)
    return d


def config():
    original = common.config(CONFIG)
    return dict(original, root=str(ROOT))


def process_id():
    return (os.environ.get('SLURM_JOB_ID', 'local'), os.getpid(), socket.gethostname())


def compatible_status(t):
    if t.get('prediction_status') == 'done' and t.get('input_status') in {'ready', 'cleaned'}:
        return 'done'
    return {'queued': 'queued', 'building': 'building', 'ready': 'ready', 'blocked': 'blocked', 'cleaned': 'done'}.get(t.get('input_status'), 'queued')


class Queue:
    def __init__(self, c):
        self.c = c
        self.root = Path(c['root'])
        self.path = self.root / 'state/queue.json'

    @contextlib.contextmanager
    def edit(self):
        with locked(self.root / 'locks/queue.lock'):
            d = read_json(self.path)
            if not isinstance(d.get('tasks'), dict):
                raise ValueError('Invalid queue')
            yield d
            atomic_json(self.path, d)

    def snapshot(self):
        with locked(self.root / 'locks/queue.lock'):
            return read_json(self.path)

    def claim(self, stage):
        if STOP or (self.root / 'state/STOP').exists():
            return None
        with self.edit() as d:
            if d['phase'] != {'cpu': 'BUILD', 'gpu': 'PREDICT'}[stage]:
                return None
            reserved = sum(t.get('reserved', 0) for t in d['tasks'].values())
            total = reserved + d.get('quarantine_bytes', 0) + self.c['shared_cache_reserve_bytes']
            if total > self.c['scratch_budget_bytes']:
                raise RuntimeError('Scratch reservation exceeds budget')
            for key, t in sorted(d['tasks'].items(), key=lambda kv: (kv[1].get('priority', 10), kv[0])):
                if t.get('region') != d['active_cube'] or t.get('kind') != 'month' or t.get('lease'):
                    continue
                eligible = t.get('input_status') == 'queued' if stage == 'cpu' else t.get('input_status') == 'ready' and t.get('prediction_status') == 'queued'
                if not eligible:
                    continue
                if stage == 'cpu' and shutil.disk_usage(self.c['scratch']).free < self.c['scratch_min_free_bytes']:
                    raise RuntimeError('Filesystem free-space guard reached')
                lease = uuid.uuid4().hex
                job, pid, host = process_id()
                t.update(job=job, pid=pid, host=host, lease=lease, stage=stage,
                         claim_generation=t.get('claim_generation', 0) + 1, updated=now())
                if stage == 'cpu':
                    t.update(input_status='building', status='building')
                else:
                    t.update(prediction_status='predicting', status='predicting')
                _claims[(str(self.root), key)] = lease
                return key, dict(t)
        return None

    def update(self, key, **kw):
        with self.edit() as d:
            t = d['tasks'][key]
            expected = _claims.get((str(self.root), key))
            if not expected or t.get('lease') != expected or (str(t.get('job')), t.get('pid'), t.get('host')) != process_id():
                raise RuntimeError('Stale worker update refused: ' + key)
            status = kw.pop('status', None)
            kw.pop('reserved', None)  # Prediction completion never frees input.
            if status == 'ready' and t['stage'] == 'cpu':
                t['input_status'] = 'ready'
            elif status == 'done' and t['stage'] == 'gpu':
                t['prediction_status'] = 'done'
            elif status == 'blocked':
                t['input_status' if t['stage'] == 'cpu' else 'prediction_status'] = 'blocked'
            elif status == 'queued':
                t['input_status' if t['stage'] == 'cpu' else 'prediction_status'] = 'queued'
            elif status is not None:
                raise ValueError('Invalid stage transition')
            t.update(kw, updated=now())
            t['status'] = compatible_status(t)
            if status is not None:
                t.update(job=None, pid=None, lease=None, stage=None)
                _claims.pop((str(self.root), key), None)


def request_cleanup(c, key, receipt):
    # Called by frozen GPU worker. Validate only; no per-month deletion.
    from products import validate_month
    report = validate_month(c, key, receipt)
    atomic_json(Path(c['root']) / 'manifests/tasks' / key / 'prediction_verified.json', report)


def heartbeat(c, kind, **kw):
    tag = os.environ.get('SLURM_JOB_ID', 'local') + '_' + str(os.getpid())
    p = Path(c['root']) / 'state/workers' / (kind + '_' + tag + '.json')
    old = read_json(p) if p.exists() else {}
    fresh = old.get('fresh_days', [])
    if kind == 'cpu' and kw.get('date'):
        record = {'task': kw['task'], 'date': kw['date']}
        if record not in fresh:
            fresh.append(record)
        # First new date of each pilot producer: compare rolling output to a
        # fresh full-window call through the frozen reader and assembler.
        parity = Path(c['root']) / 'reports/pilot_parity' / (tag + '.json')
        if not (Path(c['root']) / 'reports/pilot_passed.json').exists() and not parity.exists():
            verify_pilot_day(c, kw['task'], kw['date'], parity)
    atomic_json(p, {'kind': kind, 'job': os.environ.get('SLURM_JOB_ID'), 'pid': os.getpid(),
                   'time': time.time(), 'utc': now(), 'fresh_days': fresh, **kw})
    if STOP or (Path(c['root']) / 'state/STOP').exists() or time.monotonic() - STARTED > 23 * 3600:
        raise StopAtCheckpoint()


def verify_pilot_day(c, key, day, report_path):
    import numpy as np
    from wisp_stream.sources import IndexedReader
    from wisp_med.normalization import normalize_physical_window
    from wisp_med.eos import assemble_inputs
    from wisp_batch.store import InputStore
    from wisp_batch.common import array_hash
    region = read_json(Path(c['root']) / 'manifests/regions.json')[key.split('_')[0]]
    reader = IndexedReader(c, region)
    raw = reader.read_window(day)
    normalized = normalize_physical_window(raw, c['provenance_root'])
    reference = assemble_inputs(normalized, c['vendor_root'])
    store = InputStore(common.task_paths(c, key)[0])
    actual = store.read(store.dates.index(day))
    checks = {name: bool(np.array_equal(tensor.numpy(), actual[name])) for name, tensor in reference.items()}
    report = {'passed': all(checks.values()), 'key': key, 'date': day,
              'checks': checks, 'arrays': {k: array_hash(v) for k, v in actual.items()},
              'reference': 'frozen IndexedReader.read_window + normalize + assembler, no rolling reuse',
              'completed': now()}
    atomic_json(report_path, report)
    if not report['passed']:
        raise ValueError('Pilot direct-window reference mismatch: ' + key)


def install(c):
    from wisp_stream import pipeline, scheduler, sources
    from numcodecs import blosc
    blosc.set_nthreads(1)
    for mod in [common, pipeline, scheduler, sources]:
        mod.locked = locked
        if hasattr(mod, 'Queue'):
            mod.Queue = Queue
        if hasattr(mod, 'verify_code'):
            mod.verify_code = verify_execution
    pipeline.safe_cleanup = request_cleanup
    pipeline.worker_heartbeat = heartbeat
    common.worker_heartbeat = heartbeat

    def stop_handler(sig, frame):
        global STOP
        STOP = True
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)


def cpu_one(c):
    from wisp_stream.pipeline import build_task
    q = Queue(c)
    while not STOP and time.monotonic() - STARTED < 23 * 3600:
        item = q.claim('cpu')
        if not item:
            return
        key, task = item
        try:
            with locked(Path(c['root']) / 'locks/tasks' / (key + '.lock')):
                build_task(c, key, task)
        except StopAtCheckpoint:
            q.update(key, status='queued', interrupted_at_checkpoint=now())
            return
        except Exception as exc:
            import traceback
            transient = isinstance(exc, OSError) and exc.errno in {errno.EIO, errno.ESTALE, errno.ETIMEDOUT, errno.ECONNRESET, errno.ENETUNREACH}
            q.update(key, status='blocked', failed_stage='cpu', error=str(exc), transient=transient,
                     attempts=task.get('attempts', 0) + 1, retry_after=time.time() + 120)
            atomic_json(Path(c['root']) / 'reports/errors' / (key + '_' + str(time.time_ns()) + '.json'),
                        {'at': now(), 'error': str(exc), 'traceback': traceback.format_exc()})
            raise


def recover(c, active_ids):
    q = Queue(c)
    snapshot = q.snapshot()
    missing = {str(t['job']) for t in snapshot['tasks'].values() if t.get('lease') and t.get('job') and str(t['job']) not in active_ids}
    terminal = {}
    if missing:
        p = subprocess.run(['sacct', '-X', '-n', '-P', '-j', ','.join(sorted(missing)), '-o', 'JobIDRaw,State'], text=True, capture_output=True, check=True, timeout=30)
        for row in p.stdout.splitlines():
            a = row.split('|')
            if len(a) > 1 and a[1].split()[0] in {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE'}:
                terminal[a[0]] = a[1]
    for key, t in snapshot['tasks'].items():
        if t.get('region') != snapshot['active_cube']:
            continue
        expired = t.get('lease') and str(t.get('job')) in terminal
        retry = t.get('transient') and t.get('attempts', 0) < 3 and time.time() >= t.get('retry_after', 0) and 'blocked' in (t.get('input_status'), t.get('prediction_status'))
        if not (expired or retry):
            continue
        try:
            with locked(Path(c['root']) / 'locks/tasks' / (key + '.lock'), False):
                output = Path(c['root']) / 'manifests/tasks' / key / 'output.json'
                restored_output = None
                if (t.get('stage') or t.get('failed_stage')) == 'gpu' and output.exists():
                    from products import validate_month
                    restored_output = read_json(output)
                    validate_month(c, key, restored_output)
                with q.edit() as d:
                    cur = d['tasks'][key]
                    if cur != t:
                        continue
                    stage = t.get('stage') or t.get('failed_stage', 'cpu')
                    cur['input_status' if stage == 'cpu' else 'prediction_status'] = 'done' if restored_output else 'queued'
                    if restored_output:
                        cur['output_sha256'] = restored_output['sha256']
                    cur.update(job=None, pid=None, lease=None, stage=None, transient=False,
                               recovered_at=now(), status=compatible_status(cur))
        except BlockingIOError:
            continue
