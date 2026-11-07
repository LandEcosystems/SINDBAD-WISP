"""Actual compressed input accounting with a global, durable writer gate.

Only ``scratch/regions/Txxxxxx/inputs/*`` is counted. Raw sources, model output,
logs and shared ancillary caches are not part of this input-only budget. Both
finished and partial input stores count. A short-lived upper bound for each
active writer covers bytes not yet present on disk; finished stores consume
their measured bytes, never their old uncompressed estimate.

All producers must acquire before writing and release after they stop writing.
A crashed owner is NOT automatically expired: the caller must prove that the
writer stopped before releasing its lease. Prediction completion does not free
space. Call refresh_path after verified physical deletion, or refresh globally.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

DEFAULT_LIMIT_BYTES = 5_000_000_000_000  # Decimal TB, as requested.


class BudgetScanError(RuntimeError):
    """Accounting failed; no additional writer may be admitted."""


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('w') as handle:
        json.dump(value, handle, sort_keys=True, allow_nan=False)
        handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def measured_bytes(path, missing_ok=False):
    """Conservative compressed bytes: max(file length, allocated blocks).

    Symlinks and special files fail closed. Files disappearing during a scan
    also fail closed, rather than silently undercounting a changing directory.
    A missing *whole store* is valid only for a new task or verified cleanup.
    """
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return 0
        raise
    if stat.S_ISLNK(info.st_mode):
        raise BudgetScanError('Input accounting refuses symlink: ' + str(path))
    if stat.S_ISREG(info.st_mode):
        return max(info.st_size, getattr(info, 'st_blocks', 0) * 512)
    if not stat.S_ISDIR(info.st_mode):
        raise BudgetScanError('Input accounting refuses special file: ' + str(path))
    total = getattr(info, 'st_blocks', 0) * 512
    with os.scandir(path) as entries:
        for entry in entries:
            total += measured_bytes(entry.path)
    return total


class ActualInputBudget:
    def __init__(self, root, scratch, limit_bytes=DEFAULT_LIMIT_BYTES,
                 scan_interval_seconds=300, min_free_bytes=0):
        self.root = Path(root).resolve()
        self.scratch = Path(scratch).resolve()
        self.limit = int(limit_bytes)
        self.scan_interval = float(scan_interval_seconds)
        self.min_free = int(min_free_bytes)
        if self.limit <= 0 or self.scan_interval < 0 or self.min_free < 0:
            raise ValueError('Invalid actual input budget settings')
        self.path = self.root / 'state/actual_input_budget.json'
        self.lock_path = self.root / 'locks/actual_input_budget.lock'

    @contextlib.contextmanager
    def _edit(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                state = json.loads(self.path.read_text()) if self.path.exists() else {
                    'schema': 1, 'scratch': str(self.scratch), 'stores': {},
                    'writers': {}, 'measured_at': 0, 'actual_input_bytes': 0,
                }
                if state.get('schema') != 1 or state.get('scratch') != str(self.scratch):
                    raise ValueError('Disk ledger belongs to another scratch/schema')
                state['limit_bytes'] = self.limit
                yield state
                _atomic(self.path, state)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _store(self, path):
        # Resolve only after checking each ancestor for symlinks. Otherwise a
        # symlink into a second campaign could be accepted after normalization.
        candidate = Path(os.path.abspath(os.fspath(path)))
        try:
            relative = candidate.relative_to(self.scratch)
        except ValueError as exc:
            raise ValueError('Input store is outside campaign scratch') from exc
        parts = relative.parts
        if (len(parts) != 4 or parts[0] != 'regions' or
                not re.fullmatch(r'T\d{6}', parts[1]) or parts[2] != 'inputs'):
            raise ValueError('Expected scratch/regions/Txxxxxx/inputs/task')
        current = self.scratch
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise BudgetScanError('Input path has a symlink: ' + str(current))
        return candidate, str(relative)

    def _scan(self, active_baselines=None):
        # Active stores have a proven byte upper bound, so avoid traversing
        # files being atomically replaced by their writer. The measured field
        # stays at its last baseline until settlement; charged bytes includes
        # all remaining growth and is the admission invariant.
        stores = dict(active_baselines or {})
        region_root = self.scratch / 'regions'
        if not self.scratch.is_dir():
            raise BudgetScanError('Scratch is unavailable: ' + str(self.scratch))
        if not region_root.exists():
            return stores
        if region_root.is_symlink():
            raise BudgetScanError('Region directory is a symlink')
        with os.scandir(region_root) as regions:
            for region in regions:
                if not re.fullmatch(r'T\d{6}', region.name):
                    continue
                if region.is_symlink():
                    raise BudgetScanError('Region is a symlink: ' + region.path)
                inputs = Path(region.path) / 'inputs'
                if not inputs.exists():
                    continue
                if inputs.is_symlink():
                    raise BudgetScanError('Inputs directory is a symlink: ' + str(inputs))
                with os.scandir(inputs) as entries:
                    for entry in entries:
                        path, relative = self._store(entry.path)
                        if relative not in stores:
                            stores[relative] = measured_bytes(path)
        return stores

    def _refresh(self, state, force=False):
        if not force and time.time() - state['measured_at'] < self.scan_interval:
            return
        try:
            active_baselines = {w['store']: state['stores'].get(w['store'], 0)
                                for w in state['writers'].values()}
            stores = self._scan(active_baselines)
        except (OSError, BudgetScanError) as exc:
            # Persist the failure without replacing the last successful totals.
            state['scan_error'] = {'at': time.time(), 'message': str(exc)}
            _atomic(self.path, state)
            raise BudgetScanError('Actual input scan failed; admission paused: ' + str(exc)) from exc
        state.update(stores=stores, actual_input_bytes=sum(stores.values()), measured_at=time.time())
        state.pop('scan_error', None)

    @staticmethod
    def _view(state):
        remaining = sum(max(0, w['maximum_store_bytes'] - state['stores'].get(w['store'], 0))
                        for w in state['writers'].values())
        projected = state['actual_input_bytes'] + remaining
        return dict(actual_input_bytes=state['actual_input_bytes'],
                    inflight_reserved_bytes=remaining, projected_bytes=projected, charged_bytes=projected,
                    limit_bytes=state['limit_bytes'],
                    headroom_bytes=max(0, state['limit_bytes'] - projected),
                    active_writers=len(state['writers']), measured_at=state['measured_at'],
                    active_writer_growth_not_yet_measured=bool(state['writers']),
                    scan_error=state.get('scan_error'), pause=state.get('pause'),
                    bound_violation=state.get('bound_violation'))

    def snapshot(self, refresh=False):
        with self._edit() as state:
            self._refresh(state, force=refresh or bool(state.get('scan_error')))
            return self._view(state)

    def acquire(self, key, path, maximum_store_bytes, owner=None):
        """Reserve a complete task store's bounded growth; return a lease or None.

        ``maximum_store_bytes`` is an audited upper bound for this short task's
        entire store, including existing partial bytes and compression overhead.
        Owner metadata is diagnostic; it never authorizes expiry by itself.
        """
        key = str(key)
        path, relative = self._store(path)
        maximum = int(maximum_store_bytes)
        if maximum <= 0:
            raise ValueError('Writer requires a positive byte bound')
        with self._edit() as state:
            self._refresh(state, force=bool(state.get('scan_error')))
            if state.get('bound_violation'):
                raise BudgetScanError('Previous writer exceeded its bound; admission paused')
            if key in state['writers'] or any(w['store'] == relative for w in state['writers'].values()):
                raise ValueError('Input store already has a budget writer')
            try:
                current = measured_bytes(path, missing_ok=True)
                free = shutil.disk_usage(self.scratch).free
            except (OSError, BudgetScanError) as exc:
                state['scan_error'] = {'at': time.time(), 'message': str(exc)}
                _atomic(self.path, state)
                raise BudgetScanError('Cannot account for input store') from exc
            state['actual_input_bytes'] += current - state['stores'].get(relative, 0)
            state['stores'][relative] = current
            if current > maximum:
                state['bound_violation'] = dict(key=key, at=time.time(), observed=current,
                                               bound=maximum, existing_partial=True)
                _atomic(self.path, state)
                raise BudgetScanError('Existing input exceeds the writer byte bound; admission paused')
            growth = maximum - current
            view = self._view(state)
            if view['projected_bytes'] + growth > self.limit:
                state['pause'] = {'reason': 'actual input budget plus bounded active writes', 'at': time.time()}
                return None
            if free < view['inflight_reserved_bytes'] + growth + self.min_free:
                state['pause'] = {'reason': 'filesystem free-space guard', 'at': time.time()}
                return None
            lease = uuid.uuid4().hex
            state['writers'][str(key)] = dict(store=relative, maximum_store_bytes=maximum,
                                            lease=lease, owner=owner, acquired_at=time.time())
            state.pop('pause', None)
            return lease

    def release(self, key, lease):
        """Measure a stopped writer's files, including failure/checkpoint partials."""
        key = str(key)
        if not re.fullmatch(r'[0-9a-f]{32}', str(lease)):
            raise ValueError('Invalid budget lease')
        settled_path = self.root / 'state/actual_input_budget_settled' / (lease + '.json')
        exceeded = False
        with self._edit() as state:
            writer = state['writers'].get(key)
            if not writer or writer['lease'] != lease:
                # The queue and byte ledger are separate journals. A crash
                # after byte settlement but before queue commit is safe to
                # replay only with the exact recorded task and lease.
                if settled_path.exists():
                    settled = json.loads(settled_path.read_text())
                    if settled.get('key') == key and settled.get('lease') == lease:
                        return self._view(state)
                raise ValueError('Stale or absent budget writer')
            path, relative = self._store(self.scratch / writer['store'])
            try:
                current = measured_bytes(path, missing_ok=True)
            except (OSError, BudgetScanError) as exc:
                state['scan_error'] = {'at': time.time(), 'message': str(exc)}
                _atomic(self.path, state)
                raise BudgetScanError('Cannot settle input writer; reservation retained') from exc
            state['actual_input_bytes'] += current - state['stores'].get(relative, 0)
            state['stores'][relative] = current
            exceeded = current > writer['maximum_store_bytes']
            if exceeded:
                state['bound_violation'] = dict(key=key, at=time.time(), observed=current,
                                               bound=writer['maximum_store_bytes'])
            _atomic(settled_path, dict(key=key, lease=lease, at=time.time(), bytes=current,
                                      store=relative, bound_exceeded=exceeded))
            del state['writers'][key]
            result = self._view(state)
        if exceeded:
            raise BudgetScanError('Writer exceeded its audited maximum; inspect input bytes')
        return result

    def reconcile_stopped_jobs(self, confirmed_dead_job_ids):
        """Settle orphan writers only after scheduler-confirmed process death.

        Caller supplies terminal job IDs, not IDs absent from a queue snapshot.
        A nonblocking POSIX task lock provides an independent no-live-writer
        check. This also recovers a claim committed to this ledger just before
        its worker crashed without saving the region queue claim.
        """
        confirmed = {str(job) for job in confirmed_dead_job_ids}
        with self._edit() as state:
            candidates = [(key, dict(writer)) for key, writer in state['writers'].items()
                          if isinstance(writer.get('owner'), dict)
                          and str(writer['owner'].get('job')) in confirmed]
        recovered = []
        for key, writer in candidates:
            relative = Path(writer['store'])
            token, task_name = relative.parts[1], relative.parts[3]
            task_lock = self.root / 'regions' / token / 'locks/tasks' / (task_name + '.lock')
            task_lock.parent.mkdir(parents=True, exist_ok=True)
            with task_lock.open('a+') as handle:
                try:
                    fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    self.release(key, writer['lease'])
                    recovered.append(key)
                finally:
                    fcntl.lockf(handle, fcntl.LOCK_UN)
        return recovered

    def refresh_path(self, path):
        """Update usage after cleanup; prediction without deletion releases nothing."""
        path, relative = self._store(path)
        with self._edit() as state:
            if any(w['store'] == relative for w in state['writers'].values()):
                raise ValueError('Cannot settle cleanup for an active writer')
            try:
                current = measured_bytes(path, missing_ok=True)
            except (OSError, BudgetScanError) as exc:
                state['scan_error'] = {'at': time.time(), 'message': str(exc)}
                _atomic(self.path, state)
                raise BudgetScanError('Cannot measure input cleanup') from exc
            state['actual_input_bytes'] += current - state['stores'].get(relative, 0)
            state['stores'][relative] = current
            return self._view(state)
