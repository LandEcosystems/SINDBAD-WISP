"""Manifest-pinned recovery tests use temporary synthetic campaigns only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import recover_startup_blocks as ops


class StartupRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tokens = ['T000123', 'T000456']
        self.nodes = ['node-r6-he09', 'node-r7-he01']
        ops.atomic(self.root / 'code/release.json', {'immutable': True})
        self.release = ops.digest((self.root / 'code/release.json').read_bytes())
        state = {'phase': 'RUNNING', 'lanes': [self.tokens[0], 'T000999'],
                 'regions': {'T000999': {'status': 'ACTIVE', 'unchanged': True}}}
        for token, node in zip(self.tokens, self.nodes):
            rr = self.root / 'regions' / token
            error = ops.ERROR_PREFIX + node
            state['regions'][token] = {'status': 'BLOCKED', 'error': error,
                                      'selected_days': 100, 'lane': 0}
            ops.atomic(rr / 'reports/stage_error.json', dict(kind='cpu-startup',
                       token=token, error=error, transient=False, attempts=9))
            ops.atomic(rr / 'state/stage_failures.json', {'0:cpu-startup': 9,
                                                       '0:verify': 2})
            ops.atomic(rr / 'state/queue.json', {'phase': 'BUILD', 'active_wave': 0,
                       'tasks': {'a': {'input_status': 'ready', 'lease': None,
                                       'dates': ['2020-01-01']}}})
            (rr / 'state/STOP').write_text('')
            (rr / 'state/input.bin').write_bytes(b'precious checkpoint')
        ops.atomic(self.root / 'state/campaign.json', state)
        self.manifest = ops.freeze_manifest(self.root, self.tokens, self.nodes,
                                            'test_recovery', self.release)

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def apply(self, rows=None):
        return ops.recover(self.root, self.manifest, apply=True,
                           release_sha256=self.release, slurm_rows=rows or [])

    def test_dry_run_does_not_write(self):
        before = self.files()
        result = ops.recover(self.root, self.manifest,
                             slurm_rows=[{'id': '1', 'name': 'w4e-control', 'state': 'RUNNING'}])
        self.assertFalse(result['apply_ready'])
        self.assertEqual(before, self.files())

    def test_exact_recovery_keeps_input_queue_and_other_counters(self):
        before = self.files()
        result = self.apply()
        self.assertEqual(result['phase'], 'COMPLETE')
        state = ops.read(self.root / 'state/campaign.json')
        self.assertEqual(state['regions']['T000123']['status'], 'ACTIVE')
        self.assertEqual(state['regions']['T000456']['status'], 'READY')
        self.assertNotIn('lane', state['regions']['T000456'])
        self.assertEqual(state['regions']['T000999'], {'status': 'ACTIVE', 'unchanged': True})
        for token in self.tokens:
            rr = self.root / 'regions' / token
            for suffix in ('state/input.bin', 'state/queue.json'):
                self.assertEqual((rr / suffix).read_bytes(), before['regions/' + token + '/' + suffix])
            self.assertEqual(ops.read(rr / 'state/stage_failures.json'), {'0:verify': 2})
            self.assertFalse((rr / 'state/STOP').exists())
            self.assertFalse((rr / 'reports/stage_error.json').exists())
            for name, relative in ops.FILES.items():
                self.assertEqual(ops.original(result['regions'][token]['files'][name]),
                                 before['regions/' + token + '/' + relative])
        self.assertEqual(self.apply(), result)

    def test_pins_error_counter_stop_queue_and_region(self):
        for relative in ('reports/stage_error.json', 'state/stage_failures.json',
                         'state/STOP', 'state/queue.json'):
            path = self.root / 'regions/T000123' / relative
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.assertRaisesRegex(ValueError, 'Manifest-pinned'):
                self.apply()
            path.write_bytes(original)
        state_path = self.root / 'state/campaign.json'
        state = ops.read(state_path)
        state['regions']['T000123']['selected_days'] = 101
        ops.atomic(state_path, state)
        with self.assertRaisesRegex(ValueError, 'region state changed'):
            self.apply()

    def test_refuses_controller_and_target_jobs(self):
        with self.assertRaisesRegex(ValueError, 'still active'):
            self.apply([{'id': '1', 'name': 'w4e-control', 'state': 'PENDING'}])
        ops.atomic(self.root / 'state/submissions/2.json', {'kind': 'cpu', 'token': 'T000123'})
        with self.assertRaisesRegex(ValueError, 'still active'):
            self.apply([{'id': '2', 'name': 'w4e-cpu', 'state': 'RUNNING'}])
        self.assertTrue((self.root / 'regions/T000123/state/STOP').exists())

    def test_crash_after_removals_resumes(self):
        actual = ops.atomic
        def interrupted(path, value):
            if Path(path) == self.root / 'state/campaign.json':
                raise OSError('interrupted')
            return actual(path, value)
        with mock.patch.object(ops, 'atomic', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.apply()
        self.assertFalse((self.root / 'regions/T000123/state/STOP').exists())
        self.assertEqual(ops.read(self.root / 'reports/operations/test_recovery.json')['phase'], 'PREPARED')
        self.assertEqual(self.apply()['phase'], 'COMPLETE')

    def test_resume_refuses_new_failure(self):
        with mock.patch.object(ops, 'finish', side_effect=OSError('after intent')):
            with self.assertRaises(OSError):
                self.apply()
        path = self.root / 'regions/T000123/reports/stage_error.json'
        value = ops.read(path)
        value['error'] = 'disk error'
        ops.atomic(path, value)
        with self.assertRaisesRegex(ValueError, 'Recovery target changed'):
            self.apply()

    def test_manifest_cannot_extend_targets_or_change_release(self):
        with self.assertRaisesRegex(ValueError, 'requires --release-sha256'):
            ops.recover(self.root, self.manifest, apply=True, slurm_rows=[])
        ops.atomic(self.root / 'code/release.json', {'immutable': 'changed'})
        with self.assertRaisesRegex(ValueError, 'Frozen release SHA256'):
            self.apply()

    def test_invalid_failure_or_live_lease_cannot_be_frozen(self):
        path = self.root / 'regions/T000123/state/queue.json'
        queue = ops.read(path)
        queue['tasks']['a']['lease'] = 'live'
        ops.atomic(path, queue)
        with self.assertRaisesRegex(ValueError, 'leased tasks'):
            ops.freeze_manifest(self.root, self.tokens, self.nodes, 'test', self.release)
        queue['tasks']['a']['lease'] = None
        queue['phase'] = 'PREDICT'
        ops.atomic(path, queue)
        with self.assertRaisesRegex(ValueError, 'exact CPU qualification block'):
            ops.freeze_manifest(self.root, self.tokens, self.nodes, 'test', self.release)


if __name__ == '__main__':
    unittest.main()
