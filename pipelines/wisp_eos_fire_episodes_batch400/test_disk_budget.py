"""Actual bytes, partial writes, concurrency, cleanup and fail-closed tests."""
import importlib.util
import fcntl
import json
import multiprocessing
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('batch400_actual_disk_budget', Path(__file__).with_name('disk_budget.py'))
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)


def competing_writer(root, scratch, number, outputs):
    budget = db.ActualInputBudget(root, scratch, limit_bytes=100_000)
    path = Path(scratch) / 'regions/T000001/inputs' / str(number)
    outputs.put(bool(budget.acquire(str(number), path, 60_000)))


def hold_task_lock(path, ready, stop):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        fcntl.lockf(handle, fcntl.LOCK_EX)
        ready.set()
        stop.wait(10)


class ActualInputBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'campaign'
        self.scratch = Path(self.tmp.name) / 'scratch'
        self.scratch.mkdir()
        self.budget = db.ActualInputBudget(self.root, self.scratch, limit_bytes=100_000)

    def path(self, task='a', region='T000001'):
        return self.scratch / 'regions' / region / 'inputs' / task

    def write(self, path, payload=b'x' * 1000):
        path.mkdir(parents=True, exist_ok=True)
        (path / 'chunk').write_bytes(payload)
        return db.measured_bytes(path)

    def test_decimal_five_tb_default(self):
        self.assertEqual(db.DEFAULT_LIMIT_BYTES, 5_000_000_000_000)

    def test_only_actual_input_stores_count_including_partials(self):
        first = self.write(self.path('complete'))
        second = self.write(self.path('partial'))
        self.write(self.scratch / 'regions/T000001/raw')
        self.write(self.scratch / 'outputs')
        self.write(self.scratch / 'operational_cache')
        view = self.budget.snapshot()
        self.assertEqual(view['actual_input_bytes'], first + second)
        self.assertEqual(view['inflight_reserved_bytes'], 0)

    def test_completed_compressed_store_frees_unwritten_reservation(self):
        lease = self.budget.acquire('a', self.path(), 70_000, owner={'job': '123'})
        self.assertTrue(lease)
        self.assertEqual(self.budget.snapshot()['projected_bytes'], 70_000)
        self.assertIsNone(self.budget.acquire('b', self.path('b'), 40_000))
        actual = self.write(self.path())
        self.budget.release('a', lease)
        view = self.budget.snapshot()
        self.assertEqual(view['actual_input_bytes'], actual)
        self.assertEqual(view['inflight_reserved_bytes'], 0)
        self.assertTrue(self.budget.acquire('b', self.path('b'), 40_000))

    def test_global_rescan_does_not_double_count_live_growth(self):
        lease = self.budget.acquire('a', self.path(), 70_000)
        actual = self.write(self.path())
        with patch.object(db, 'measured_bytes', side_effect=AssertionError('must not scan active store')):
            view = self.budget.snapshot(refresh=True)
        self.assertEqual(view['actual_input_bytes'], 0)
        self.assertEqual(view['inflight_reserved_bytes'], 70_000)
        self.assertEqual(view['projected_bytes'], 70_000)
        self.budget.release('a', lease)
        self.assertEqual(self.budget.snapshot()['actual_input_bytes'], actual)

    def test_partial_retry_accounts_only_remaining_growth(self):
        actual = self.write(self.path())
        self.budget.snapshot()
        lease = self.budget.acquire('a', self.path(), 60_000)
        view = self.budget.snapshot()
        self.assertEqual(view['projected_bytes'], 60_000)
        self.assertEqual(view['inflight_reserved_bytes'], 60_000 - actual)
        self.budget.release('a', lease)

    def test_prediction_does_not_free_bytes_but_verified_deletion_does(self):
        self.write(self.path())
        old = self.budget.snapshot()['actual_input_bytes']
        self.assertEqual(self.budget.refresh_path(self.path())['actual_input_bytes'], old)
        shutil.rmtree(self.path())
        self.assertEqual(self.budget.refresh_path(self.path())['actual_input_bytes'], 0)

    def test_stale_or_duplicate_writer_cannot_release_or_write(self):
        lease = self.budget.acquire('a', self.path(), 60_000)
        with self.assertRaises(ValueError):
            self.budget.acquire('a', self.path(), 60_000)
        with self.assertRaises(ValueError):
            self.budget.acquire('other', self.path(), 60_000)
        with self.assertRaises(ValueError):
            self.budget.release('a', 'stale-lease')
        with self.assertRaises(ValueError):
            self.budget.refresh_path(self.path())
        self.assertEqual(self.budget.snapshot()['active_writers'], 1)
        self.budget.release('a', lease)

    def test_scan_failure_keeps_previous_total_and_denies_admission(self):
        actual = self.write(self.path())
        self.budget.snapshot()
        with patch.object(self.budget, '_scan', side_effect=PermissionError('denied')):
            with self.assertRaises(db.BudgetScanError):
                self.budget.snapshot(refresh=True)
            with self.assertRaises(db.BudgetScanError):
                self.budget.acquire('b', self.path('b'), 60_000)
        state = json.loads(self.budget.path.read_text())
        self.assertEqual(state['actual_input_bytes'], actual)
        self.assertEqual(state['writers'], {})
        self.assertIn('scan_error', state)
        self.assertTrue(self.budget.acquire('b', self.path('b'), 60_000))

    def test_symlink_and_external_store_rejected(self):
        external = self.root / 'formal_outputs'
        self.write(external)
        self.path().parent.mkdir(parents=True)
        self.path().symlink_to(external, target_is_directory=True)
        with self.assertRaises(db.BudgetScanError):
            self.budget.snapshot()
        with self.assertRaises(db.BudgetScanError):
            self.budget.acquire('a', self.path(), 10_000)
        with self.assertRaises(ValueError):
            self.budget.acquire('b', external, 10_000)

    def test_full_scan_is_cached_not_per_task_or_per_day(self):
        with patch.object(self.budget, '_scan', wraps=self.budget._scan) as scan:
            self.budget.snapshot()
            a = self.budget.acquire('a', self.path(), 30_000)
            self.budget.release('a', a)
            b = self.budget.acquire('b', self.path('b'), 30_000)
            self.budget.snapshot()
            self.budget.release('b', b)
            self.assertEqual(scan.call_count, 1)

    def test_filesystem_guard_independent_of_input_budget(self):
        with patch.object(db.shutil, 'disk_usage', return_value=type('Usage', (), {'free': 1})()):
            self.assertIsNone(self.budget.acquire('a', self.path(), 60_000))
        self.assertEqual(self.budget.snapshot()['active_writers'], 0)

    def test_parallel_claims_cannot_overbook_budget(self):
        ctx = multiprocessing.get_context('fork')
        outputs = ctx.Queue()
        workers = [ctx.Process(target=competing_writer,
                               args=(str(self.root), str(self.scratch), i, outputs)) for i in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sum(outputs.get(timeout=3) for _ in workers), 1)
        self.assertEqual(self.budget.snapshot()['projected_bytes'], 60_000)

    def test_oversized_writer_stops_future_admission(self):
        lease = self.budget.acquire('a', self.path(), 100)
        self.write(self.path())
        with self.assertRaises(db.BudgetScanError):
            self.budget.release('a', lease)
        with self.assertRaises(db.BudgetScanError):
            self.budget.acquire('b', self.path('b'), 60_000)

    def test_release_is_replayable_only_for_matching_settlement(self):
        lease = self.budget.acquire('a', self.path(), 60_000)
        actual = self.write(self.path())
        self.budget.release('a', lease)
        self.assertEqual(self.budget.release('a', lease)['actual_input_bytes'], actual)
        with self.assertRaises(ValueError):
            self.budget.release('other', lease)
        new_lease = self.budget.acquire('a', self.path(), 60_000)
        self.budget.release('a', lease)  # A stale replay cannot release new owner.
        self.assertEqual(self.budget.snapshot()['active_writers'], 1)
        self.budget.release('a', new_lease)

    def test_only_confirmed_dead_owners_are_reconciled(self):
        lease = self.budget.acquire('a', self.path(), 60_000, owner={'job': '123'})
        self.assertEqual(self.budget.reconcile_stopped_jobs(['456']), [])
        actual = self.write(self.path())
        self.assertEqual(self.budget.reconcile_stopped_jobs(['123']), ['a'])
        self.assertEqual(self.budget.snapshot()['actual_input_bytes'], actual)
        self.assertEqual(self.budget.snapshot()['active_writers'], 0)
        self.budget.release('a', lease)  # Queue recovery can replay the settlement.

    def test_task_file_lock_blocks_orphan_recovery(self):
        self.budget.acquire('a', self.path(), 60_000, owner={'job': '123'})
        ctx = multiprocessing.get_context('fork')
        ready, stop = ctx.Event(), ctx.Event()
        lock = self.root / 'regions/T000001/locks/tasks/a.lock'
        worker = ctx.Process(target=hold_task_lock, args=(str(lock), ready, stop))
        worker.start()
        try:
            self.assertTrue(ready.wait(3))
            self.assertEqual(self.budget.reconcile_stopped_jobs(['123']), [])
            self.assertEqual(self.budget.snapshot()['active_writers'], 1)
        finally:
            stop.set(); worker.join(10)
        self.assertEqual(worker.exitcode, 0)
        self.assertEqual(self.budget.reconcile_stopped_jobs(['123']), ['a'])


if __name__ == '__main__':
    unittest.main()
