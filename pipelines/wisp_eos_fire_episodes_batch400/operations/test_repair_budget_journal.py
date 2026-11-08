import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from disk_budget import ActualInputBudget
from repair_budget_journal import restore_missing_writer


class MissingWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / 'scratch').mkdir()
        self.budget = ActualInputBudget(root / 'campaign', root / 'scratch', 100000)
        self.path = root / 'scratch/regions/T000001/inputs/key'
        self.task = dict(stage='cpu', lease='q', job='123', reserved=60000,
                         input_budget_lease='a' * 32, region='T000001')

    def test_dead_owner_repaired_settled_and_replay_safe(self):
        self.assertTrue(restore_missing_writer(self.budget, 'key', self.task, self.path, {'123'}))
        self.path.mkdir(parents=True)
        (self.path / 'data').write_bytes(b'x' * 1000)
        result = self.budget.release('key', 'a' * 32)
        self.assertGreater(result['actual_input_bytes'], 0)
        self.assertEqual(result['active_writers'], 0)
        self.assertFalse(restore_missing_writer(self.budget, 'key', self.task, self.path, {'123'}))

    def test_no_scheduler_proof_refused(self):
        with self.assertRaisesRegex(ValueError, 'terminated'):
            restore_missing_writer(self.budget, 'key', self.task, self.path, set())

    def test_different_live_lease_not_overwritten(self):
        live = self.budget.acquire('key', self.path, 60000, owner={'job': '456'})
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            restore_missing_writer(self.budget, 'key', self.task, self.path, {'123'})
        self.assertEqual(json.loads(self.budget.path.read_text())['writers']['key']['lease'], live)
