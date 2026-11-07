"""Wave-aware operations around the read-only, frozen EOS numerical pipeline.

Only scheduling, verification and journalling change here.  The scientific
configuration, source reader, rolling window, normalization, assembler, model,
batch implementation and output writer remain the audited implementations.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
PILOT = HERE.parent / 'wisp_eos_fire_episodes'
if str(PILOT) not in sys.path:
    sys.path.insert(0, str(PILOT))
import runtime as rt
from wisp_stream import common
from wisp_batch.common import SHAPES, atomic_json, file_hash

if Path(rt.__file__).resolve().parent != PILOT.resolve():
    raise ImportError('Unexpected event runtime; refusing to use another deployment')

locked = rt.locked
read_json = rt.read_json
now = rt.now
BUDGET_BYTES = 256 * 2**30
MAX_WAVE_BYTES = 240 * 2**30
FIXED_CACHE_BYTES = 8 * 2**30
MAX_FAILURE_ATTEMPTS = 3
TRANSIENT_ERRNOS = {errno.EIO, errno.ENXIO, errno.ESTALE, errno.ETIMEDOUT,
                    errno.ECONNRESET, errno.ENETUNREACH}
_CAMPAIGN = None
_ORIGINAL_QUEUE = rt.Queue
_ORIGINAL_VERIFY = rt._original_verify


class WaveFinished(BaseException):
    """Return an idle frozen GPU worker to its shared campaign dispatcher."""


@contextlib.contextmanager
def prediction_reader(c):
    """Multiple CPU/GPU readers coexist; cleanup requires exclusive ownership."""
    path=Path(c['root'])/'locks/prediction_readers.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_SH)
        try:yield
        finally:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def _inside(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    return path != parent and parent in path.parents


def _campaign(c, campaign_root=None):
    root = Path(c['root']).resolve()
    campaign = Path(campaign_root or _CAMPAIGN or root.parent.parent).resolve()
    if root.parent != campaign / 'regions' or not re.fullmatch(r'T\d{6}', root.name):
        raise ValueError('Region root must be campaign/regions/Txxxxxx')
    return campaign


def check_operational_config(c, original, campaign_root):
    """Only explicitly operational fields may differ from the frozen run."""
    if {k: v for k, v in c.items() if k not in rt.OPERATIONAL_FIELDS} != {
        k: v for k, v in original.items() if k not in rt.OPERATIONAL_FIELDS
    }:
        raise ValueError('Frozen scientific/source configuration differs')
    campaign = _campaign(c, campaign_root)
    scratch, old = Path(c['scratch']).resolve(), Path(original['scratch']).resolve()
    if scratch == old or scratch in old.parents or old in scratch.parents:
        raise ValueError('Campaign scratch must be independent of the full-decade scratch')
    if scratch.parent.name != 'regions' or scratch.name != Path(c['root']).name:
        raise ValueError('Scratch must be campaign_scratch/regions/Txxxxxx')
    if scratch.parent.parent.name != campaign.name:
        raise ValueError('Scratch campaign identity differs')
    if c['scratch_budget_bytes'] != BUDGET_BYTES:
        raise ValueError('Each region cache budget must be 256 GiB')
    if not 0 <= c['shared_cache_reserve_bytes'] <= BUDGET_BYTES - MAX_WAVE_BYTES:
        raise ValueError('Shared-cache reservation exceeds the 16 GiB margin')
    if c['scratch_min_free_bytes'] < 0:
        raise ValueError('Invalid filesystem free-space guard')


def verify_release(campaign_root):
    """Validate the complete deployed operational code and pilot dependencies."""
    code = Path(campaign_root).resolve() / 'code'
    release_path = code / 'release.json'
    release = read_json(release_path)
    if not release.get('files'):
        raise ValueError('Missing immutable campaign release')
    required = {'batch400/adapter.py', 'wisp_eos_fire_episodes/runtime.py',
                'wisp_eos_fire_episodes/products.py'}
    if not required.issubset(release['files']):
        raise ValueError('Operational release omits adapter or pilot dependencies')
    for name, digest in release['files'].items():
        path = (code / name).resolve()
        if not _inside(path, code) or file_hash(path) != digest:
            raise ValueError('Operational release changed: ' + name)
    for field, path in [('campaign_config_sha256', code.parent / 'config/campaign.json'),
                        ('catalog_sha256', code.parent / 'manifests/named_production_regions.json')]:
        if field in release and file_hash(path) != release[field]:
            raise ValueError('Campaign configuration or catalog changed: ' + field)
    if HERE.resolve() != (code / 'batch400').resolve():
        raise ValueError('Worker is not running the immutable deployed adapter')
    return release


def _frozen_waves(root):
    value = read_json(Path(root) / 'manifests/waves.json')
    return value['waves'] if isinstance(value, dict) else value


def verify_execution(c):
    campaign = _campaign(c)
    original = common.config(rt.CONFIG)
    deployment = _ORIGINAL_VERIFY(original)
    check_operational_config(c, original, campaign)
    verify_release(campaign)
    root = Path(c['root'])
    identity = read_json(root / 'manifests/identity.json')
    if read_json(root / 'manifests/deployment.json') != deployment:
        raise ValueError('Frozen scientific deployment identity differs')
    if identity.get('deployment_fingerprint') != deployment['fingerprint']:
        raise ValueError('Region deployment fingerprint differs')
    if ('campaign_release_sha256' in identity
            and identity['campaign_release_sha256'] != file_hash(campaign / 'code/release.json')):
        raise ValueError('Region belongs to another operational release')
    files = {
        'config_sha256': 'config/deployment.json',
        'selection_sha256': 'manifests/selection.json',
        'regions_sha256': 'manifests/regions.json',
        'parity_dates_sha256': 'manifests/parity_dates.json',
        'waves_sha256': 'manifests/waves.json',
    }
    for field, relative in files.items():
        if identity.get(field) != file_hash(root / relative):
            raise ValueError('Frozen region identity changed: ' + relative)
    if 'pre5_migration_sha256' in identity:
        if file_hash(root / 'manifests/pre5_migration.json') != identity['pre5_migration_sha256']:
            raise ValueError('Frozen date-subset migration identity changed')
    # The in-memory configuration is checked too: hashing a file alone must not
    # allow a caller to supply different operational paths after loading it.
    on_disk = read_json(root / 'config/deployment.json')
    if {k: v for k, v in c.items() if k != 'config_path'} != {
        k: v for k, v in on_disk.items() if k != 'config_path'
    }:
        raise ValueError('In-memory region configuration differs from its receipt')
    regions = read_json(root / 'manifests/regions.json')
    selection = read_json(root / 'manifests/selection.json')
    if set(regions) != {root.name} or set(selection['regions']) != {root.name}:
        raise ValueError('A lane must own exactly one frozen region')
    if ('coverage_qa_sha256' in identity
            and identity['coverage_qa_sha256'] != file_hash(regions[root.name]['coverage_qa'])):
        raise ValueError('Frozen coverage QA changed')
    validate_waves(_frozen_waves(root), selection['tasks'])
    state_path = root / 'state/queue.json'
    if state_path.exists():
        state = Queue(c).snapshot()
        if set(state['tasks']) != set(selection['tasks']) or state['waves'] != _frozen_waves(root):
            raise ValueError('Queue membership differs from frozen selection')
        for key, task in selection['tasks'].items():
            if any(state['tasks'][key].get(field) != task.get(field) for field in ('region', 'kind', 'dates')):
                raise ValueError('Queue task identity differs from frozen selection: ' + key)
    return deployment


def validate_waves(waves, tasks):
    all_keys = []
    ids = []
    for wave in waves:
        keys = wave['keys']
        if not keys or len(keys) != len(set(keys)):
            raise ValueError('Wave has empty or repeated task keys')
        if any(key not in tasks for key in keys):
            raise ValueError('Wave references an unknown task')
        if not isinstance(wave['wave_id'], int):
            raise ValueError('Wave ID must be an integer')
        days = sum(len(tasks[key]['dates']) for key in keys)
        reservation = FIXED_CACHE_BYTES + sum(rt.task_reservation(tasks[key]) for key in keys)
        if days != wave['days'] or not 1 <= days <= 1000:
            raise ValueError('Wave day count exceeds the frozen limit or differs')
        if reservation != wave['reserved_bytes'] or reservation > MAX_WAVE_BYTES:
            raise ValueError('Wave cache reservation differs or exceeds 240 GiB')
        ids.append(wave['wave_id'])
        all_keys.extend(keys)
    if len(ids) != len(set(ids)) or len(all_keys) != len(set(all_keys)) or set(all_keys) != set(tasks):
        raise ValueError('Waves must partition all selected tasks exactly once')


def current_wave(state):
    matches = [w for w in state['waves'] if w['wave_id'] == state['active_wave']]
    if len(matches) != 1:
        raise ValueError('No unique active wave')
    wave = matches[0]
    if len(wave['keys']) != len(set(wave['keys'])) or not set(wave['keys']).issubset(state['tasks']):
        raise ValueError('Invalid active-wave task membership')
    return wave

def actual_budget(c):
    campaign=_campaign(c)
    path=campaign/'config/campaign.json'
    if not path.exists():return None
    cfg=read_json(path)
    if cfg.get('input_budget_mode')!='actual':return None
    from disk_budget import ActualInputBudget
    return ActualInputBudget(campaign,cfg['scratch'],limit_bytes=cfg['backlog_budget_bytes'],
        scan_interval_seconds=cfg.get('input_budget_scan_seconds',3600),
        min_free_bytes=cfg.get('backlog_min_free_bytes',0))


class Queue(_ORIGINAL_QUEUE):
    """Keep every task auditable, while admitting only the current wave."""

    def claim(self, stage):
        if stage not in {'cpu', 'gpu'}:
            raise ValueError('Unknown queue stage')
        if rt.STOP or (self.root / 'state/STOP').exists():
            return None
        budget=actual_budget(self.c) if stage=='cpu' else None
        with self.edit() as state:
            if state['phase'] != {'cpu': 'BUILD', 'gpu': 'PREDICT'}[stage]:
                return None
            if (stage == 'cpu' and 'allowed_cpu_jobs' in state
                    and os.environ.get('SLURM_JOB_ID', 'local') not in state['allowed_cpu_jobs']):
                return None
            wave = current_wave(state)
            if state['waves'] != _frozen_waves(self.root):
                raise ValueError('Queue wave plan differs from its immutable manifest')
            selected = [(key, state['tasks'][key]) for key in wave['keys']]
            if stage == 'gpu' and all(t.get('prediction_status') == 'done' and not t.get('lease')
                                      for _, t in selected):
                raise WaveFinished()
            reserved = sum(t.get('reserved', 0) for t in state['tasks'].values())
            fixed = state.get('quarantine_bytes', 0) + self.c['shared_cache_reserve_bytes']
            if stage == 'cpu' and budget is None and time.time() - state.get('scratch_measured_at', 0) >= 60:
                measured = rt.scratch_size(self.c['scratch'])
                state.update(scratch_measured_bytes=measured, scratch_measured_at=time.time(),
                             scratch_peak_measured_bytes=max(measured, state.get('scratch_peak_measured_bytes', 0)))
            for key, task in sorted(selected, key=lambda kv: (kv[1].get('priority', 10), kv[0])):
                if task.get('lease'):
                    continue
                eligible = task.get('input_status') == 'queued' if stage == 'cpu' else (
                    task.get('input_status') == 'ready' and task.get('prediction_status') == 'queued')
                if not eligible:
                    continue
                if stage == 'gpu':
                    backend=os.environ.get('WISP_INFERENCE_BACKEND','cuda')
                    if backend not in {'cpu','cuda'}:raise ValueError('Unknown inference backend')
                    pinned=task.get('prediction_backend')
                    # Old partial predictions were produced only by A100.
                    if pinned is None:
                        metadata=common.task_paths(self.c,key)[1]
                        if ((metadata/'prediction_days.json').exists() or
                                any((self.root/'monthly').glob('*/'+key+'.partial.nc'))):
                            pinned='cuda'
                    if pinned is not None and pinned!=backend:continue
                rt.validate_task(key, task)
                increment = max(0, rt.task_reservation(task) - task.get('reserved', 0)) if stage == 'cpu' else 0
                if stage == 'cpu':
                    if budget is None and max(reserved + fixed, state.get('scratch_measured_bytes', 0)) + increment > self.c['scratch_budget_bytes']:
                        state['admission_pause'] = {'reason': '256 GiB cache budget', 'at': now()}
                        return None
                    if shutil.disk_usage(self.c['scratch']).free < increment + self.c['scratch_min_free_bytes']:
                        state['admission_pause'] = {'reason': 'filesystem free-space guard', 'at': now()}
                        return None
                lease = uuid.uuid4().hex
                job, pid, host = rt.process_id()
                if stage=='cpu' and budget is not None:
                    from disk_budget import BudgetScanError
                    try:
                        reservation=budget.acquire(key,common.task_paths(self.c,key)[0],rt.task_reservation(task),
                            owner=dict(job=job,pid=pid,host=host,region=self.root.name,task=key))
                    except BudgetScanError as exc:
                        state['admission_pause']=dict(reason=str(exc),at=now())
                        return None
                    if reservation is None:
                        state['admission_pause']=dict(reason='actual global input budget',at=now())
                        return None
                    task['input_budget_lease']=reservation
                task.update(job=job, pid=pid, host=host, lease=lease, stage=stage,
                            claim_generation=task.get('claim_generation', 0) + 1, updated=now())
                if stage == 'cpu':
                    task.update(input_status='building', status='building', reserved=task.get('reserved', 0) + increment)
                else:
                    task.update(prediction_status='predicting', status='predicting',
                                prediction_backend=backend)
                state.pop('admission_pause', None)
                rt._claims[(str(self.root), key)] = lease
                return key, dict(task)
        return None

    def update(self,key,**kw):
        """Settle actual input bytes before relinquishing the writer lease."""
        with self.edit() as state:
            task=state['tasks'][key]
            expected=rt._claims.get((str(self.root),key))
            owner=(str(task.get('job')),task.get('pid'),task.get('host'))
            if not expected or task.get('lease')!=expected or owner!=rt.process_id():
                raise RuntimeError('Stale worker update refused: '+key)
            status=kw.pop('status',None);kw.pop('reserved',None)
            for field in ('lease','stage','host','job','pid'):kw.pop(field,None)
            if status is not None and task['stage']=='cpu' and task.get('input_budget_lease'):
                actual_budget(self.c).release(key,task['input_budget_lease'])
                task.pop('input_budget_lease',None)
            if status=='ready' and task['stage']=='cpu':task['input_status']='ready'
            elif status=='done' and task['stage']=='gpu':task['prediction_status']='done'
            elif status in {'blocked','queued'}:
                task['input_status' if task['stage']=='cpu' else 'prediction_status']=status
            elif status is not None:raise ValueError('Invalid event stage transition')
            task.update(kw,updated=now());task['status']=rt.compatible_status(task)
            if status is not None:
                task.update(job=None,pid=None,host=None,lease=None,stage=None)
                rt._claims.pop((str(self.root),key),None)


def heartbeat(c, kind, **kw):
    """Retain per-date journals; independently reassemble only frozen QA dates."""
    root = Path(c['root'])
    tag = os.environ.get('SLURM_JOB_ID', 'local') + '_' + str(os.getpid())
    path = root / 'state/workers' / (kind + '_' + tag + '.json')
    previous = read_json(path) if path.exists() else {}
    fresh = previous.get('fresh_days', [])
    parity_seconds = 0.0
    if kind == 'cpu' and kw.get('date'):
        item = {'task': kw['task'], 'date': kw['date']}
        if item not in fresh:
            fresh.append(item)
        planned = read_json(root / 'manifests/parity_dates.json').get(root.name, [])
        if kw['date'] in planned:
            boundary = root / 'reports/input_parity' / (kw['task'] + '_' + kw['date'][:10] + '.json')
            if not boundary.exists():
                started = time.monotonic()
                rt.verify_pilot_day(c, kw['task'], kw['date'], boundary)
                parity_seconds = time.monotonic() - started
    record = {'kind': kind, 'job': os.environ.get('SLURM_JOB_ID'), 'pid': os.getpid(),
              'time': time.time(), 'utc': now(), 'parity_seconds': parity_seconds, **kw}
    journal = root / 'logs/worker_events' / (kind + '_' + tag + '.jsonl')
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open('a') as handle:
        handle.write(json.dumps(record, sort_keys=True, allow_nan=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    atomic_json(path, dict(record, fresh_days=fresh))
    if rt.STOP or (root / 'state/STOP').exists() or time.monotonic() - rt.STARTED > 23 * 3600:
        raise rt.StopAtCheckpoint()


def install_signal_handlers():
    """Keep controller and worker stop flags in sync, without growing a chain."""
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)
        if getattr(previous, '_batch400_stop_handler', False) is True:
            continue
        def stop_handler(received, frame, prior=previous):
            rt.STOP = True
            if callable(prior) and prior is not signal.default_int_handler:
                prior(received, frame)
        stop_handler._batch400_stop_handler = True
        signal.signal(sig, stop_handler)


def activate(c, campaign_root):
    """Activate region-scoped operations without touching any frozen files."""
    global _CAMPAIGN
    _CAMPAIGN = Path(campaign_root).resolve()
    deployment = verify_execution(c)
    from wisp_stream import pipeline, scheduler, sources
    from numcodecs import blosc
    blosc.set_nthreads(1)
    for module in (common, pipeline, scheduler, sources):
        module.locked = locked
        if hasattr(module, 'Queue'):
            module.Queue = Queue
        if hasattr(module, 'verify_code'):
            module.verify_code = verify_execution
    rt.Queue = Queue
    rt.heartbeat = heartbeat
    rt.verify_execution = verify_execution
    import products
    import batch_products
    batch_products.activate_products(products)
    pipeline.safe_cleanup = rt.request_cleanup
    pipeline.worker_heartbeat = heartbeat
    common.worker_heartbeat = heartbeat
    install_signal_handlers()
    return deployment


def cpu_one(c):
    return rt.cpu_one(c)


def _stage_attempts(task, stage):
    attempts = int(task.get('failure_attempts_by_stage', {}).get(stage, 0))
    legacy = int(task.get('attempts', 0)) if task.get('failed_stage', stage) == stage else 0
    return max(attempts, legacy)


def recover(c, active_ids):
    """Bound scheduler-owner failures as well as ordinary transient failures.

    A complete, validated GPU receipt is restored without consuming a retry.
    Otherwise a scheduler-confirmed dead owner consumes one failed attempt of
    its stage. The third failure is terminal; ordinary day checkpoints release
    their leases explicitly and do not count as failures.
    """
    queue = Queue(c)
    snapshot = queue.snapshot()
    active_ids = {str(item) for item in active_ids}
    missing = {str(task['job']) for task in snapshot['tasks'].values()
               if task.get('lease') and task.get('job') and str(task['job']) not in active_ids}
    terminal = {}
    if missing:
        result = rt.subprocess.run(
            ['sacct', '-X', '-n', '-P', '-j', ','.join(sorted(missing)), '-o', 'JobIDRaw,State'],
            text=True, capture_output=True, check=True, timeout=30)
        for row in result.stdout.splitlines():
            fields = row.split('|')
            if len(fields) > 1 and fields[1].split() and fields[1].split()[0] in rt.TERMINAL_STATES:
                terminal[fields[0]] = fields[1]
    for key, task in snapshot['tasks'].items():
        expired = bool(task.get('lease') and str(task.get('job')) in terminal)
        stage = task.get('stage') or task.get('failed_stage', 'cpu')
        if stage not in {'cpu', 'gpu'}:
            raise ValueError('Unknown task recovery stage: ' + key)
        retry = (task.get('transient') and _stage_attempts(task, stage) < MAX_FAILURE_ATTEMPTS
                 and time.time() >= task.get('retry_after', 0)
                 and 'blocked' in (task.get('input_status'), task.get('prediction_status')))
        if not (expired or retry):
            continue
        try:
            with locked(Path(c['root']) / 'locks/tasks' / (key + '.lock'), False):
                receipt_path = Path(c['root']) / 'manifests/tasks' / key / 'output.json'
                restored = None
                if stage == 'gpu' and receipt_path.exists():
                    from products import validate_chunk
                    receipt = read_json(receipt_path)
                    try:
                        validate_chunk(c, key, receipt, task)
                        restored = receipt
                    except (ValueError, FileNotFoundError, KeyError):
                        # A partial/corrupt file remains incomplete; never infer
                        # success solely from an output filename or receipt.
                        restored = None
                with queue.edit() as state:
                    current = state['tasks'][key]
                    if current != task:
                        continue
                    if stage=='cpu' and current.get('input_budget_lease'):
                        actual_budget(c).release(key,current['input_budget_lease'])
                        current.pop('input_budget_lease',None)
                    field = 'input_status' if stage == 'cpu' else 'prediction_status'
                    if restored:
                        current[field] = 'done'
                        current['output_sha256'] = restored['sha256']
                        current['restored_from_verified_output_at'] = now()
                    else:
                        attempts = _stage_attempts(task, stage)
                        if expired:
                            attempts += 1
                            failures = dict(current.get('failure_attempts_by_stage', {}))
                            failures[stage] = attempts
                            deaths = dict(current.get('owner_failure_attempts_by_stage', {}))
                            deaths[stage] = deaths.get(stage, 0) + 1
                            current.update(attempts=attempts, failure_attempts_by_stage=failures,
                                           owner_failure_attempts_by_stage=deaths, failed_stage=stage,
                                           last_scheduler_failure=terminal[str(task['job'])],
                                           error='Task owner ended in scheduler state ' + terminal[str(task['job'])])
                        current[field] = 'blocked' if attempts >= MAX_FAILURE_ATTEMPTS else 'queued'
                        if attempts >= MAX_FAILURE_ATTEMPTS:
                            current['retry_limit_reached'] = True
                    current.update(job=None, pid=None, host=None, lease=None, stage=None,
                                   transient=False, recovered_at=now())
                    current['status'] = rt.compatible_status(current)
                    rt._claims.pop((str(queue.root), key), None)
        except BlockingIOError:
            # A live reader/writer still owns the file; scheduler evidence alone
            # never authorizes concurrent repair.
            continue


def record_gpu_failure(c, exc, before):
    """Add bounded, typed failure metadata omitted by the frozen GPU worker."""
    queue = Queue(c)
    wave_keys = set(current_wave(before)['keys'])
    transient = isinstance(exc, OSError) and exc.errno in TRANSIENT_ERRNOS
    affected = []
    with queue.edit() as state:
        for key in wave_keys:
            previous = before['tasks'][key]
            task = state['tasks'][key]
            if (task.get('prediction_status') != 'blocked' or task.get('failed_stage') != 'gpu'
                    or task.get('lease') or task.get('error') != str(exc)):
                continue
            if (previous.get('prediction_status') == 'blocked'
                    and previous.get('updated') == task.get('updated')):
                continue
            attempts = _stage_attempts(previous, 'gpu') + 1
            failures = dict(task.get('failure_attempts_by_stage', {}))
            failures['gpu'] = attempts
            task.update(attempts=attempts, failure_attempts_by_stage=failures,
                        transient=transient and attempts < MAX_FAILURE_ATTEMPTS,
                        retry_after=time.time() + 120, failure_class=type(exc).__name__,
                        failure_errno=getattr(exc, 'errno', None), updated=now())
            if attempts >= MAX_FAILURE_ATTEMPTS:
                task['retry_limit_reached'] = True
            affected.append(key)
    atomic_json(Path(c['root']) / 'reports/errors' / ('gpu_classification_' + str(time.time_ns()) + '.json'),
                {'at': now(), 'keys': sorted(affected), 'error': str(exc), 'transient': transient,
                 'exception_class': type(exc).__name__, 'errno': getattr(exc, 'errno', None)})
    return affected


def verify_wave(c):
    """Read back all seven arrays and required independent parity evidence."""
    import numpy as np
    from wisp_batch.store import InputStore
    verify_execution(c)
    root = Path(c['root'])
    queue = Queue(c)
    state = queue.snapshot()
    if state['phase'] != 'INPUT_VERIFY':
        raise ValueError('Not in input verification phase')
    wave = current_wave(state)
    observed = []
    reports = []
    for key in wave['keys']:
        task = state['tasks'][key]
        if task.get('pre5_reused_prediction'):
            from pre5_migration import verify_imported_prediction
            verify_imported_prediction(c, key, task)
            observed.extend(task['dates'])
            reports.append(dict(key=key,days=len(task['dates']),reused_prediction=True))
            continue
        if task['input_status'] != 'ready' or task.get('lease'):
            raise ValueError('Wave input is not complete and unowned: ' + key)
        with locked(root / 'locks/tasks' / (key + '.lock'), False):
            store = InputStore(common.task_paths(c, key)[0])
            success = store.path / '_SUCCESS.json'
            if not success.exists() or store.fingerprint != task['input_fingerprint']:
                raise ValueError('Input fingerprint mismatch: ' + key)
            if list(store.dates) != task['dates']:
                raise ValueError('Input date manifest mismatch: ' + key)
            for i, day in enumerate(store.dates):
                arrays = store.read(i)  # Frozen reader verifies per-array receipt hashes.
                if set(arrays) != set(SHAPES):
                    raise ValueError('Missing or extra model input group')
                for name, array in arrays.items():
                    if tuple(array.shape) != tuple(SHAPES[name]) or array.dtype != np.dtype('float32'):
                        raise ValueError('Model input shape or dtype differs: ' + name)
                    if not np.isfinite(array).all():
                        raise ValueError('Nonfinite model input: ' + name)
                del arrays
                observed.append(day)
            # Numerical reader/assembler isolation is tested at every chunk start.
            days = read_json(root / 'manifests/tasks' / key / 'input_days.json')
            checks = days[task['dates'][0]]['metadata'].get('frozen_checks', {})
            for name in ('future_input_isolation', 'frozen_reference_parity'):
                if set(checks.get(name, {})) != set(SHAPES) or not all(checks[name].values()):
                    raise ValueError('Frozen first-day check missing or failed: ' + key + ' ' + name)
            reports.append({'key': key, 'days': len(store.dates), 'fingerprint': store.fingerprint,
                            'success_sha256': file_hash(success)})
    if len(observed) != len(set(observed)) or len(observed) != wave['days']:
        raise ValueError('Wave has duplicated or missing prediction dates')
    planned = read_json(root / 'manifests/parity_dates.json').get(root.name, [])
    checked = [read_json(path) for path in (root / 'reports/input_parity').glob('*.json')]
    for day in set(planned).intersection(observed):
        imported_key = next(k for k in wave['keys'] if day in state['tasks'][k]['dates'])
        if state['tasks'][imported_key].get('pre5_reused_prediction'):
            # Existing, independently checked predictions are copied byte-for-byte.
            # Their original input hashes and checks remain in the migration receipt.
            continue
        if not any(r.get('passed') and r.get('date') == day and r.get('key') in wave['keys']
                   and set(r.get('checks', {})) == set(SHAPES) and all(r['checks'].values()) for r in checked):
            # A checkpoint may have committed its input immediately before the
            # process died during the heartbeat/parity step. Reuse that input,
            # and repair only the missing validation evidence.
            key = next(key for key in wave['keys'] if day in state['tasks'][key]['dates'])
            path = root / 'reports/input_parity' / (key + '_' + day[:10] + '.json')
            with locked(root / 'locks/tasks' / (key + '.lock'), False):
                rt.verify_pilot_day(c, key, day, path)
            result = read_json(path)
            if not result.get('passed') or set(result.get('checks', {})) != set(SHAPES) or not all(result['checks'].values()):
                raise ValueError('Independent raw-window parity evidence failed: ' + day)
    report = {'passed': True, 'wave_id': wave['wave_id'], 'days': len(observed), 'chunks': reports,
              'independent_parity_dates': sorted(set(planned).intersection(observed)),
              'release_sha256': file_hash(_campaign(c) / 'code/release.json'), 'at': now()}
    atomic_json(root / 'reports/waves' / ('inputs_%04d.json' % wave['wave_id']), report)
    with queue.edit() as current:
        if current['phase'] != 'INPUT_VERIFY' or current['active_wave'] != wave['wave_id']:
            raise ValueError('Input phase or active wave changed during verification')
        if any(current['tasks'][key] != state['tasks'][key] for key in wave['keys']):
            raise ValueError('Input task changed during verification')
        current.update(phase='PREDICT', inputs_verified_at=now(), gpu_ready_epoch=time.time())
    return report


def gpu(c, model=None):
    """Run a verified wave using one model shared across sequential regions."""
    import numpy as np
    import torch
    from wisp_stream import pipeline as p
    from wisp_batch.store import InputStore
    verify_execution(c)
    root = Path(c['root'])
    state = Queue(c).snapshot()
    if state['phase'] != 'PREDICT':
        raise ValueError('Region is not ready for GPU inference')
    wave = current_wave(state)
    hardware = torch.cuda.get_device_name(0)
    if 'A100' not in hardware:
        raise ValueError('Batch400 requires the validated A100 hardware family')
    torch.manual_seed(42)
    np.random.seed(42)
    if model is None:
        model = p.BatchedEOS(c['checkpoint'], c['vendor_root'])
        model.model.requires_grad_(False)
    if (model.identity.get('checkpoint_sha256') != c['checkpoint_sha256']
            or model.identity.get('model_commit') != c['code_commit']
            or model.identity.get('precision') != 'bfloat16'
            or model.identity.get('history_mode') != 'observed_viirs'
            or model.model.training):
        raise ValueError('Shared GPU model identity or eval mode differs')
    gate = root / 'reports/mixed_batch_A100.json'
    if gate.exists():
        existing = read_json(gate)
        if not existing.get('passed') or existing['deployment'] != common.identity(c)['fingerprint']:
            raise ValueError('Numerical gate belongs to another deployment or failed')
    else:
        candidates = [(key, day) for key in wave['keys']
                      if state['tasks'][key]['prediction_status'] != 'done'
                      for day in state['tasks'][key]['dates']]
        if not candidates:
            return model
        count = min(8, len(candidates))
        chosen = [candidates[round(i * (len(candidates) - 1) / max(1, count - 1))] for i in range(count)]
        examples = []
        members = []
        for key, day in chosen:
            store = InputStore(common.task_paths(c, key)[0])
            examples.append((store, store.dates.index(day)))
            members.append({'token': root.name, 'day': day, 'key': key})
        # A short boundary episode may have fewer than eight dates. Padding is
        # explicitly recorded, and matches the frozen production batch writer.
        while len(examples) < 8:
            examples.append(examples[-1])
        p.mixed_check(c, model, examples, hardware)
        atomic_json(root / 'reports/batch_gate_members.json', {
            'regions': 1, 'distinct_dates': count, 'padded_members': 8 - count,
            'members': members, 'meaning': 'Dates within one cube; padding is not extra observations', 'at': now()})
    original = p.BatchedEOS
    p.BatchedEOS = lambda *args, **kwargs: model
    try:
        p.gpu_worker(c)
    except WaveFinished:
        pass
    except Exception as exc:
        record_gpu_failure(c, exc, state)
        raise
    finally:
        p.BatchedEOS = original
    return model
