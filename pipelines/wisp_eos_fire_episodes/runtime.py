"""Event-only operational adapter; the frozen numerical implementation is read-only.

One lease owns one short contiguous interval. Inputs remain reserved after GPU
completion and can only be removed by the separate verified cleanup stage.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time
import uuid

from wisp_stream import common
from wisp_batch.common import SHAPES, atomic_json, file_hash

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CONFIG = Path('/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/shift_v3_60km/wisp_eos_q10_v1/config/deployment.json')
_original_verify = common.verify_code
_claims = {}
_held = set()
STOP = False
STARTED = time.monotonic()
BUDGET_BYTES = 128 * 2**30
OPERATIONAL_FIELDS = {'root', 'scratch', 'scratch_budget_bytes', 'shared_cache_reserve_bytes',
                      'scratch_min_free_bytes', 'config_path'}
TERMINAL_STATES = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'OUT_OF_MEMORY',
                   'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE'}


class StopAtCheckpoint(BaseException):
    pass


def now():
    return common.now()


def read_json(path):
    return json.loads(Path(path).read_text())


@contextlib.contextmanager
def locked(path, blocking=True):
    """Stable-inode POSIX locks, shared by all stages of this isolated workflow."""
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


def config():
    return common.config(ROOT / 'config/deployment.json')


def check_operational_config(c, original):
    """Reject changing any numerical/source setting through this adapter."""
    if {k: v for k, v in c.items() if k not in OPERATIONAL_FIELDS} != {
        k: v for k, v in original.items() if k not in OPERATIONAL_FIELDS
    }:
        raise ValueError('Frozen scientific/source configuration differs')
    if Path(c['root']).resolve() != ROOT.resolve():
        raise ValueError('Event root must match the deployed code location')
    scratch, old = Path(c['scratch']).resolve(), Path(original['scratch']).resolve()
    if scratch == old or scratch in old.parents or old in scratch.parents:
        raise ValueError('Event scratch must be independent of the full-decade scratch')
    if c['scratch_budget_bytes'] != BUDGET_BYTES:
        raise ValueError('The event experiment cache budget must be 128 GiB')
    if not 0 <= c['shared_cache_reserve_bytes'] < BUDGET_BYTES:
        raise ValueError('Invalid shared-cache reservation')
    if c['scratch_min_free_bytes'] < 0:
        raise ValueError('Invalid filesystem free-space guard')


def verify_execution(c):
    original = common.config(CONFIG)
    deployment = _original_verify(original)
    check_operational_config(c, original)
    if read_json(ROOT / 'manifests/deployment.json') != deployment:
        raise ValueError('Frozen scientific deployment identity differs')
    release = read_json(HERE / 'release.json')
    if not release.get('files'):
        raise ValueError('Missing immutable event release files')
    for name, digest in release['files'].items():
        path = (HERE / name).resolve()
        if HERE.resolve() not in path.parents or file_hash(path) != digest:
            raise ValueError('Operational release changed: ' + name)
    for key, path in [('config_sha256', ROOT / 'config/deployment.json'),
                      ('selection_sha256', ROOT / 'manifests/selection.json')]:
        if key in release and file_hash(path) != release[key]:
            raise ValueError('Operational identity changed: ' + str(path))
    return deployment


def process_id():
    return (os.environ.get('SLURM_JOB_ID', 'local'), os.getpid(), socket.gethostname())


def compatible_status(t):
    if t.get('prediction_status') == 'done' and t.get('input_status') in {'ready', 'cleaned'}:
        return 'done'
    if t.get('prediction_status') in {'predicting', 'blocked'}:
        return t['prediction_status']
    return t.get('input_status', 'queued')


def validate_task(key, task):
    if not re.fullmatch(r'T\d{6}_evt_[A-Za-z0-9]+_[A-Za-z0-9_-]+', key):
        raise ValueError('Invalid episode task key: ' + key)
    if key.split('_')[0] != task.get('region') or task.get('kind') != 'episode':
        raise ValueError('Episode task identity mismatch: ' + key)
    dates = [dt.datetime.fromisoformat(x) for x in task.get('dates', [])]
    if not 1 <= len(dates) <= 7 or len(set(dates)) != len(dates):
        raise ValueError('An episode task must contain one to seven unique days')
    if any(x.time() != dt.time() or x.tzinfo is not None for x in dates):
        raise ValueError('Prediction dates must be midnight UTC, stored without an offset')
    if any(b - a != dt.timedelta(days=1) for a, b in zip(dates, dates[1:])):
        raise ValueError('An episode task must be consecutive and chronologically ordered')
    if dates[0] < dt.datetime(2014, 1, 12) or dates[-1] > dt.datetime(2023, 12, 31):
        raise ValueError('Prediction date outside the approved research period')


def task_reservation(task):
    # Seven float32 arrays plus 25% allowance and a small per-store allowance.
    raw_day_bytes = 4 * sum(math.prod(shape) for shape in SHAPES.values())
    return math.ceil(len(task['dates']) * raw_day_bytes * 1.25) + 32 * 2**20


def scratch_size(path):
    """Logical bytes, counting the private tree and never following symlinks."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [x for x in dirs if not (Path(root) / x).is_symlink()]
        for name in files:
            item = Path(root) / name
            if item.is_symlink():
                continue
            try:
                total += item.stat().st_size
            except FileNotFoundError:
                # Atomic writers may replace their own temporary files during the scan.
                continue
    return total


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
                raise ValueError('Invalid event queue')
            yield d
            atomic_json(self.path, d)

    def snapshot(self):
        with locked(self.root / 'locks/queue.lock'):
            return read_json(self.path)

    def claim(self, stage):
        if stage not in {'cpu', 'gpu'}:
            raise ValueError('Unknown queue stage')
        if STOP or (self.root / 'state/STOP').exists():
            return None
        with self.edit() as d:
            if d['phase'] != {'cpu': 'BUILD', 'gpu': 'PREDICT'}[stage]:
                return None
            reserved = sum(t.get('reserved', 0) for t in d['tasks'].values())
            fixed = d.get('quarantine_bytes', 0) + self.c['shared_cache_reserve_bytes']
            if stage == 'cpu' and time.time() - d.get('scratch_measured_at', 0) >= 60:
                measured = scratch_size(self.c['scratch'])
                d.update(scratch_measured_bytes=measured, scratch_measured_at=time.time(),
                         scratch_peak_measured_bytes=max(measured, d.get('scratch_peak_measured_bytes', 0)))
            for key, t in sorted(d['tasks'].items(), key=lambda kv: (kv[1].get('priority', 10), kv[0])):
                if t.get('kind') != 'episode' or t.get('lease'):
                    continue
                eligible = t.get('input_status') == 'queued' if stage == 'cpu' else (
                    t.get('input_status') == 'ready' and t.get('prediction_status') == 'queued')
                if not eligible:
                    continue
                validate_task(key, t)
                increment = max(0, task_reservation(t) - t.get('reserved', 0)) if stage == 'cpu' else 0
                if stage == 'cpu':
                    if max(reserved + fixed, d.get('scratch_measured_bytes', 0)) + increment > self.c['scratch_budget_bytes']:
                        d['admission_pause'] = {'reason': '128 GiB cache budget', 'at': now()}
                        return None
                    if shutil.disk_usage(self.c['scratch']).free < increment + self.c['scratch_min_free_bytes']:
                        d['admission_pause'] = {'reason': 'filesystem free-space guard', 'at': now()}
                        return None
                lease = uuid.uuid4().hex
                job, pid, host = process_id()
                t.update(job=job, pid=pid, host=host, lease=lease, stage=stage,
                         claim_generation=t.get('claim_generation', 0) + 1, updated=now())
                if stage == 'cpu':
                    t.update(input_status='building', status='building', reserved=t.get('reserved', 0) + increment)
                else:
                    t.update(prediction_status='predicting', status='predicting')
                d.pop('admission_pause', None)
                _claims[(str(self.root), key)] = lease
                return key, dict(t)
        return None

    def update(self, key, **kw):
        with self.edit() as d:
            t = d['tasks'][key]
            expected = _claims.get((str(self.root), key))
            owner = (str(t.get('job')), t.get('pid'), t.get('host'))
            if not expected or t.get('lease') != expected or owner != process_id():
                raise RuntimeError('Stale worker update refused: ' + key)
            status = kw.pop('status', None)
            kw.pop('reserved', None)  # GPU completion never authorizes deletion or frees space.
            for field in ('lease', 'stage', 'host', 'job', 'pid'):
                kw.pop(field, None)
            if status == 'ready' and t['stage'] == 'cpu':
                t['input_status'] = 'ready'
            elif status == 'done' and t['stage'] == 'gpu':
                t['prediction_status'] = 'done'
            elif status in {'blocked', 'queued'}:
                t['input_status' if t['stage'] == 'cpu' else 'prediction_status'] = status
            elif status is not None:
                raise ValueError('Invalid event stage transition')
            t.update(kw, updated=now())
            t['status'] = compatible_status(t)
            if status is not None:
                t.update(job=None, pid=None, host=None, lease=None, stage=None)
                _claims.pop((str(self.root), key), None)


def request_cleanup(c, key, receipt):
    """Frozen GPU callback: validate a chunk only; never delete its inputs."""
    from products import validate_chunk
    report = validate_chunk(c, key, receipt)
    atomic_json(Path(c['root']) / 'manifests/tasks' / key / 'prediction_verified.json', report)


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
              'reference': 'Independent frozen IndexedReader.read_window + normalization + assembler, no rolling reuse',
              'completed': now()}
    atomic_json(report_path, report)
    if not report['passed']:
        raise ValueError('Event direct-window reference mismatch: ' + key)


def heartbeat(c, kind, **kw):
    root = Path(c['root'])
    tag = os.environ.get('SLURM_JOB_ID', 'local') + '_' + str(os.getpid())
    path = root / 'state/workers' / (kind + '_' + tag + '.json')
    old = read_json(path) if path.exists() else {}
    fresh = old.get('fresh_days', [])
    parity_seconds = 0.0
    if kind == 'cpu' and kw.get('date'):
        record = {'task': kw['task'], 'date': kw['date']}
        if record not in fresh:
            fresh.append(record)
        parity = root / 'reports/pilot_parity' / (tag + '.json')
        if not (root / 'reports/pilot_passed.json').exists() and not parity.exists():
            started = time.monotonic()
            verify_pilot_day(c, kw['task'], kw['date'], parity)
            parity_seconds += time.monotonic() - started
        selection = root / 'manifests/parity_dates.json'
        planned = read_json(selection).get(kw['task'].split('_')[0], []) if selection.exists() else []
        if kw['date'] in planned:
            boundary = root / 'reports/input_parity' / (kw['task'] + '_' + kw['date'][:10] + '.json')
            if not boundary.exists():
                if parity.exists() and read_json(parity).get('date') == kw['date'] and read_json(parity).get('key') == kw['task']:
                    atomic_json(boundary, read_json(parity))
                else:
                    started = time.monotonic()
                    verify_pilot_day(c, kw['task'], kw['date'], boundary)
                    parity_seconds += time.monotonic() - started
    record = {'kind': kind, 'job': os.environ.get('SLURM_JOB_ID'), 'pid': os.getpid(),
              'time': time.time(), 'utc': now(), 'parity_seconds': parity_seconds, **kw}
    # Each producer owns its journal; preserve every completed date/batch timing.
    # A killed append may leave one incomplete final line, which readers may ignore.
    journal = root / 'logs/worker_events' / (kind + '_' + tag + '.jsonl')
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open('a') as f:
        f.write(json.dumps(record, sort_keys=True, allow_nan=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
    atomic_json(path, dict(record, fresh_days=fresh))
    if STOP or (root / 'state/STOP').exists() or time.monotonic() - STARTED > 23 * 3600:
        raise StopAtCheckpoint()


def install(c):
    from wisp_stream import pipeline, scheduler, sources
    from numcodecs import blosc
    blosc.set_nthreads(1)
    for module in [common, pipeline, scheduler, sources]:
        module.locked = locked
        if hasattr(module, 'Queue'):
            module.Queue = Queue
        if hasattr(module, 'verify_code'):
            module.verify_code = verify_execution
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
            transient = isinstance(exc, OSError) and exc.errno in {
                errno.EIO, errno.ENXIO, errno.ESTALE, errno.ETIMEDOUT, errno.ECONNRESET, errno.ENETUNREACH}
            q.update(key, status='blocked', failed_stage='cpu', error=str(exc), transient=transient,
                     attempts=task.get('attempts', 0) + 1, retry_after=time.time() + 120)
            atomic_json(Path(c['root']) / 'reports/errors' / (key + '_' + str(time.time_ns()) + '.json'),
                        {'at': now(), 'key': key, 'error': str(exc), 'traceback': traceback.format_exc()})
            raise


def recover(c, active_ids):
    """Requeue only scheduler-confirmed dead owners or bounded transient failures."""
    q = Queue(c)
    snapshot = q.snapshot()
    active_ids = {str(x) for x in active_ids}
    missing = {str(t['job']) for t in snapshot['tasks'].values()
               if t.get('lease') and t.get('job') and str(t['job']) not in active_ids}
    terminal = {}
    if missing:
        result = subprocess.run(['sacct', '-X', '-n', '-P', '-j', ','.join(sorted(missing)), '-o', 'JobIDRaw,State'],
                                text=True, capture_output=True, check=True, timeout=30)
        for row in result.stdout.splitlines():
            fields = row.split('|')
            if len(fields) > 1 and fields[1].split() and fields[1].split()[0] in TERMINAL_STATES:
                terminal[fields[0]] = fields[1]
    for key, task in snapshot['tasks'].items():
        expired = task.get('lease') and str(task.get('job')) in terminal
        retry = (task.get('transient') and task.get('attempts', 0) < 3
                 and time.time() >= task.get('retry_after', 0)
                 and 'blocked' in (task.get('input_status'), task.get('prediction_status')))
        if not (expired or retry):
            continue
        try:
            with locked(Path(c['root']) / 'locks/tasks' / (key + '.lock'), False):
                output = Path(c['root']) / 'manifests/tasks' / key / 'output.json'
                restored = None
                if (task.get('stage') or task.get('failed_stage')) == 'gpu' and output.exists():
                    from products import validate_chunk
                    receipt = read_json(output)
                    try:
                        validate_chunk(c, key, receipt)
                        restored = receipt
                    except (ValueError, FileNotFoundError, KeyError):
                        # A partial output must be resumed/rewritten, never called complete.
                        restored = None
                with q.edit() as d:
                    current = d['tasks'][key]
                    if current != task:
                        continue
                    stage = task.get('stage') or task.get('failed_stage', 'cpu')
                    field = 'input_status' if stage == 'cpu' else 'prediction_status'
                    current[field] = 'done' if restored else 'queued'
                    if restored:
                        current['output_sha256'] = restored['sha256']
                    current.update(job=None, pid=None, host=None, lease=None, stage=None, transient=False,
                                   recovered_at=now())
                    current['status'] = compatible_status(current)
        except BlockingIOError:
            continue
