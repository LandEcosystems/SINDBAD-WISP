"""Receipt recovery must retain real evidence and never overwrite mismatches."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import recover_legacy_gpu_gate as ops


class GateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name) / 'legacy'
        self.lane = self.parent / 'lanes/lane_b'
        self.audit = Path(self.tmp.name) / 'audit/recovery.json'
        self.manifest = dict(fingerprint=ops.SCIENTIFIC_ID, checkpoint_sha256='checkpoint', model_commit='code', packages={'torch': 'frozen'})
        self.receipt = dict(passed=True, deployment=ops.SCIENTIFIC_ID, hardware=ops.HARDWARE,
                            members=[{'date': str(i)} for i in range(8)],
                            comparisons_to_b1=[{}] * 8, b1_repeat=[{}] * 8, completed='original-time')
        self.put(self.parent / 'manifests/deployment.json', self.manifest)
        self.put(self.lane / 'manifests/deployment.json', self.manifest)
        self.source = self.parent / 'reports/mixed_batch_A100.json'
        self.target = self.lane / 'reports/mixed_batch_A100.json'
        self.put(self.source, self.receipt)
        self.sha = ops.digest(self.source.read_bytes())
        self.put(self.lane / 'reports/gpu_environment_1.json', {
            'hardware': ops.HARDWARE, 'model': {'checkpoint_sha256': 'checkpoint', 'model_commit': 'code'},
            'environment': {'packages': {'torch': 'frozen'}}})

    def put(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def apply(self):
        return ops.recover(self.parent, self.lane, self.audit, self.sha, apply=True)

    def test_dry_run_does_not_create_target_or_audit(self):
        result = ops.recover(self.parent, self.lane, self.audit, self.sha)
        self.assertTrue(result['dry_run'])
        self.assertFalse(self.target.exists())
        self.assertFalse(self.audit.parent.exists())

    def test_same_bytes_published_idempotently_with_provenance(self):
        result = self.apply()
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(result['phase'], 'COMPLETE')
        self.assertEqual(result['original_qualified_at'], 'original-time')
        self.assertEqual(self.apply(), result)

    def test_refuses_mismatched_existing_receipt(self):
        self.put(self.target, {'passed': True, 'different': True})
        original = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            self.apply()
        self.assertEqual(self.target.read_bytes(), original)
        self.assertFalse(self.audit.exists())

    def test_manifest_hardware_and_incomplete_gate_are_rejected(self):
        self.put(self.lane / 'manifests/deployment.json', dict(self.manifest, model_commit='other'))
        with self.assertRaisesRegex(ValueError, 'manifests differ'):
            self.apply()
        self.put(self.lane / 'manifests/deployment.json', self.manifest)
        for update in ({'passed': False}, {'hardware': 'other'}, {'members': []}, {'deployment': 'other'}):
            self.put(self.source, dict(self.receipt, **update))
            self.sha = ops.digest(self.source.read_bytes())
            with self.assertRaisesRegex(ValueError, 'complete matching'):
                self.apply()

    def test_mismatch_in_actual_gpu_environment_is_rejected(self):
        self.put(self.lane / 'reports/gpu_environment_1.json', {'hardware': 'A40'})
        with self.assertRaisesRegex(ValueError, 'Actual lane GPU'):
            self.apply()

    def test_resume_after_publication_before_completion_audit(self):
        actual = ops.atomic_bytes
        def interrupted(path, data, exclusive=False):
            if Path(path) == self.audit and json.loads(data)['phase'] == 'COMPLETE':
                raise OSError('simulated process interruption')
            return actual(path, data, exclusive)
        with mock.patch.object(ops, 'atomic_bytes', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(ops.read(self.audit)['phase'], 'PREPARED')
        self.assertEqual(self.apply()['phase'], 'COMPLETE')


if __name__ == '__main__':
    unittest.main()
