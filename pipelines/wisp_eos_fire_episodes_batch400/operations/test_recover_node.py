"""Safety boundaries and interrupted recovery, using temporary campaigns only."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import recover_node as ops


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        ops.atomic(self.root / 'code/release.json', {'frozen': True})
        self.release = ops.digest((self.root / 'code/release.json').read_bytes())
        state = {'phase': 'RUNNING', 'lanes': list(ops.TOKENS[:-1]) + [None] * 4,
                 'regions': {'T000001': {'status': 'ACTIVE', 'unrelated': True}}}
        for token in ops.TOKENS:
            state['regions'][token] = {'status': 'BLOCKED', 'error': ops.ERROR,
                                       'lane': 4, 'selected_days': 900}
            rr = self.root / 'regions' / token
            ops.atomic(rr / 'reports/stage_error.json', {
                'kind': 'cpu-startup', 'token': token, 'error': ops.ERROR,
                'transient': False, 'attempts': 17})
            ops.atomic(rr / 'state/stage_failures.json', {'0:cpu-startup': 17, '0:verify': 2})
            ops.atomic(rr / 'state/queue.json', {'phase': 'BUILD', 'active_wave': 0,
                'tasks': {'task': {'input_status': 'building', 'lease': 'running-worker',
                                   'reserved': 123, 'job': '789'}}})
            (rr / 'state/STOP').write_text('')
        ops.atomic(self.root / 'state/campaign.json', state)

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def apply(self, **kwargs):
        return ops.recover(self.root, apply=True, release_sha256=self.release,
                           slurm_rows=kwargs.get('rows', []))

    def test_dry_run_is_read_only_even_with_live_controller(self):
        before = self.files()
        result = ops.recover(self.root, slurm_rows=[{'id': '11', 'name': 'w4e-control', 'state': 'RUNNING'}])
        self.assertTrue(result['dry_run'])
        self.assertFalse(result['apply_ready'])
        self.assertEqual(before, self.files())

    def test_rejects_running_or_pending_controller_by_name_and_registry(self):
        for row in ({'id': '11', 'name': 'w4e-control', 'state': 'PENDING'},
                    {'id': '12', 'name': 'renamed-controller', 'state': 'RUNNING'}):
            ops.atomic(self.root / 'state/submissions/12.json', {'kind': 'control'})
            with self.assertRaisesRegex(ValueError, 'Controller still'):
                self.apply(rows=[row])
        self.assertTrue((self.root / 'regions' / ops.TOKENS[0] / 'state/STOP').exists())

    def test_applies_only_operational_block_and_archives_original_bytes(self):
        before = self.files()
        result = self.apply()
        self.assertEqual(result['phase'], 'COMPLETE')
        state = ops.read(self.root / 'state/campaign.json')
        self.assertEqual(state['regions']['T000001'], {'status': 'ACTIVE', 'unrelated': True})
        for token in ops.TOKENS:
            rr = self.root / 'regions' / token
            self.assertEqual((rr / 'state/queue.json').read_bytes(), before[f'regions/{token}/state/queue.json'])
            self.assertEqual(ops.read(rr / 'state/stage_failures.json'), {'0:verify': 2})
            self.assertFalse((rr / 'state/STOP').exists())
            self.assertFalse((rr / 'reports/stage_error.json').exists())
            self.assertEqual(state['regions'][token]['status'], 'READY' if token == ops.TOKENS[-1] else 'ACTIVE')
            self.assertNotIn('error', state['regions'][token])
            for name, relative in ops.FILES.items():
                self.assertEqual(ops.original(result['regions'][token]['files'][name]),
                                 before[f'regions/{token}/{relative}'])
        self.assertEqual((self.root / 'code/release.json').read_bytes(), before['code/release.json'])
        self.assertEqual(self.apply(), result)

    def test_resume_after_removal_before_campaign_update(self):
        actual = ops.atomic
        def crash(path, value):
            if Path(path) == self.root / 'state/campaign.json':
                raise OSError('simulated crash')
            return actual(path, value)
        with mock.patch.object(ops, 'atomic', side_effect=crash):
            with self.assertRaisesRegex(OSError, 'simulated crash'):
                self.apply()
        journal = self.root / 'reports/operations' / ('recover_' + ops.NODE + '.json')
        self.assertEqual(ops.read(journal)['phase'], 'PREPARED')
        self.assertFalse((self.root / 'regions' / ops.TOKENS[0] / 'state/STOP').exists())
        self.assertEqual(self.apply()['phase'], 'COMPLETE')

    def test_resume_refuses_new_failure_or_changed_release(self):
        with mock.patch.object(ops, 'finish', side_effect=OSError('after intent')):
            with self.assertRaises(OSError):
                self.apply()
        error = self.root / 'regions' / ops.TOKENS[0] / 'reports/stage_error.json'
        value = ops.read(error)
        value['error'] = 'unrelated disk failure'
        ops.atomic(error, value)
        with self.assertRaisesRegex(ValueError, 'Recovery target changed'):
            self.apply()
        ops.atomic(self.root / 'code/release.json', {'frozen': 'changed'})
        with self.assertRaisesRegex(ValueError, 'Frozen release SHA256'):
            self.apply()

    def test_rejects_wrong_error_phase_and_unpinned_apply(self):
        with self.assertRaisesRegex(ValueError, 'requires --release-sha256'):
            ops.recover(self.root, apply=True, slurm_rows=[])
        path = self.root / 'regions' / ops.TOKENS[0] / 'state/queue.json'
        queue = ops.read(path)
        queue['phase'] = 'PREDICT'
        ops.atomic(path, queue)
        with self.assertRaisesRegex(ValueError, 'exact node startup block'):
            self.apply()
        self.assertFalse((self.root / 'reports/operations').exists())


if __name__ == '__main__':
    unittest.main()
