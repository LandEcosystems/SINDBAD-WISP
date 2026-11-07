"""Actual-byte/CPU-consumer upgrades preserve work and resume after interruption."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ops = load('_actual_upgrade_test_common', 'common.py')
with patch.dict(sys.modules, common=ops):
    previous = load('_actual_upgrade_test_previous', 'backlog_upgrade.py')
with patch.dict(sys.modules, common=ops, backlog_upgrade=previous):
    target = load('_actual_upgrade_test_target', 'actual_upgrade.py')


class ActualUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'campaign'
        self.source = Path(self.tmp.name) / 'source'
        self.source.mkdir()
        (self.source / 'controller.py').write_text('# New actual-byte controller\n')
        (self.source / 'disk_budget.py').write_text('# Shared actual-byte writer gate\n')
        (self.root / 'code/batch400').mkdir(parents=True)
        (self.root / 'code/batch400/controller.py').write_text('# Previous deployed controller\n')
        cfg = dict(root=str(self.root), scratch=str(self.root / 'scratch'),
                   max_lanes=2, max_build_regions=1, region_budget_bytes=256 * ops.GIB)
        ops.atomic(self.root / 'config/campaign.json', cfg)
        self.tokens = ['T000001', 'T000002', 'T000003']
        ops.atomic(self.root / 'manifests/named_production_regions.json', {'regions': self.tokens})
        release = dict(files={'batch400/controller.py': ops.sha(self.root / 'code/batch400/controller.py')},
                       campaign_config_sha256=ops.sha(self.root / 'config/campaign.json'),
                       catalog_sha256=ops.sha(self.root / 'manifests/named_production_regions.json'))
        ops.atomic(self.root / 'code/release.json', release)
        self.old_release = ops.sha(self.root / 'code/release.json')
        self.queues = {}
        for index, token in enumerate(self.tokens):
            rr = self.root / 'regions' / token
            queue = dict(phase='PREDICT' if index == 0 else 'BUILD', tasks={
                token + '_task': dict(dates=['2020-01-01'], input_status='ready' if index == 0 else 'queued',
                                     prediction_status='queued', lease=None)})
            self.queues[token] = queue
            ops.atomic(rr / 'state/queue.json', queue)
            ops.atomic(rr / 'config/deployment.json', {'checkpoint': 'frozen', 'region': token})
            for manifest in ['selection', 'waves', 'regions', 'parity_dates']:
                ops.atomic(rr / 'manifests' / (manifest + '.json'), {'frozen': manifest, 'region': token})
            ops.atomic(rr / 'manifests/identity.json',
                       {'campaign_release_sha256': self.old_release, 'deployment_fingerprint': 'unchanged-science'})
        self.state = dict(lanes=self.tokens[:2], lane_limit=2, producer_cap=21, phase='RUNNING',
                          regions={token: dict(status='ACTIVE' if i < 2 else 'READY', selected_days=1)
                                   for i, token in enumerate(self.tokens)})
        ops.atomic(self.root / 'state/campaign.json', self.state)
        for job, kind in [('123', 'gpu'), ('456', 'cpu')]:
            ops.atomic(self.root / 'state/submissions' / (job + '.json'),
                       dict(kind=kind, name='w4e-' + kind, job=job, release_sha256=self.old_release))
        self.raw_input = self.root / 'scratch/regions/T000001/inputs/task/chunk'
        self.prediction = self.root / 'regions/T000001/predictions/already_done.nc'
        for path, contents in [(self.raw_input, b'compressed-input'), (self.prediction, b'formal-prediction')]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        self.preserved = previous.preserved(self.root)
        self.archive = self.root / 'operations/actual5tb_cpu_gpu_20261108/release_archive'
        self.journal = self.archive.parent / 'upgrade.json'

    def scheduler(self, args, **kwargs):
        self.assertEqual(args[0], 'squeue')
        return types.SimpleNamespace(stdout=(
            '123|w4e-gpu|PENDING|JobHeldUser\n'
            '456|w4e-cpu|PENDING|JobHeldUser\n'
            '789|unrelated-user-job|RUNNING|node01\n'))

    def check_preserved(self):
        self.assertEqual(previous.preserved(self.root), self.preserved)
        self.assertEqual(self.raw_input.read_bytes(), b'compressed-input')
        self.assertEqual(self.prediction.read_bytes(), b'formal-prediction')
        after = ops.read(self.root / 'state/campaign.json')
        self.assertEqual(after['regions'], self.state['regions'])
        self.assertEqual(after['lanes'], self.tokens[:2] + [None])
        self.assertEqual(after['producer_cap'], 21)
        cfg = ops.read(self.root / 'config/campaign.json')
        self.assertEqual(cfg['backlog_budget_bytes'], 5_000_000_000_000)
        self.assertEqual(cfg['input_budget_mode'], 'actual')
        self.assertEqual(cfg['max_lanes'], 3)
        self.assertEqual(cfg['max_total_producers'], 210)
        self.assertEqual(cfg['max_cpu_consumers'], 8)
        self.assertEqual(cfg['inference_backends'], ['cuda', 'cpu'])
        self.assertEqual(cfg['cpu_prediction_precision'], 'float32')
        new_release = ops.sha(self.root / 'code/release.json')
        for token in self.tokens:
            identity = ops.read(self.root / 'regions' / token / 'manifests/identity.json')
            self.assertEqual(identity['campaign_release_sha256'], new_release)
            self.assertEqual(identity['deployment_fingerprint'], 'unchanged-science')
        for job in ['123', '456']:
            record = ops.read(self.root / 'state/submissions' / (job + '.json'))
            self.assertEqual(record['release_sha256'], new_release)
            self.assertTrue(record['operational_rebind']['remained_held'])
        self.assertEqual(ops.read(self.journal)['phase'], 'COMPLETE')
        self.assertTrue((self.archive / 'code').is_dir())
        ops.verify_release(self.root)

    def test_complete_upgrade_preserves_all_work_and_replays(self):
        with patch.object(target.subprocess, 'run', side_effect=self.scheduler):
            target.upgrade(self.source, self.root, 'newcommit123')
            target.upgrade(self.source, self.root, 'newcommit123')
        self.check_preserved()

    def test_interrupted_upgrade_resumes_after_code_switch(self):
        original_atomic = target.atomic
        interrupted = False

        def interrupt_once(path, value):
            nonlocal interrupted
            if Path(path) == self.root / 'state/campaign.json' and not interrupted:
                interrupted = True
                raise OSError('Simulated interruption after code/config switch')
            return original_atomic(path, value)

        with patch.object(target.subprocess, 'run', side_effect=self.scheduler), \
                patch.object(target, 'atomic', side_effect=interrupt_once):
            with self.assertRaisesRegex(OSError, 'Simulated interruption'):
                target.upgrade(self.source, self.root, 'newcommit123')
        self.assertEqual(ops.read(self.journal)['phase'], 'PREPARED')
        self.assertNotEqual(ops.sha(self.root / 'code/release.json'), self.old_release)
        with patch.object(target.subprocess, 'run', side_effect=self.scheduler):
            target.upgrade(self.source, self.root, 'newcommit123')
        self.check_preserved()

    def test_running_producer_is_refused_before_modification(self):
        response = types.SimpleNamespace(stdout='456|w4e-cpu|RUNNING|node01\n')
        with patch.object(target.subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(ValueError, 'finish or be held'):
                target.upgrade(self.source, self.root, 'newcommit123')
        self.assertFalse(self.archive.exists())
        self.assertEqual(ops.sha(self.root / 'code/release.json'), self.old_release)

    def test_unheld_pending_gpu_is_refused(self):
        response = types.SimpleNamespace(stdout='123|w4e-gpu|PENDING|Priority\n')
        with patch.object(target.subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(ValueError, 'finish or be held'):
                target.upgrade(self.source, self.root, 'newcommit123')
        self.assertFalse(self.archive.exists())

    def test_controller_or_live_task_lease_is_refused(self):
        ops.atomic(self.root / 'state/submissions/999.json', dict(name='w4e-control', kind='control'))
        response = types.SimpleNamespace(stdout='999|w4e-control|PENDING|JobHeldUser\n')
        with patch.object(target.subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(ValueError, 'Stop controller'):
                target.quiescent(self.root)
        rr = self.root / 'regions/T000001/state/queue.json'
        queue = ops.read(rr)
        queue['tasks']['T000001_task']['lease'] = 'still-owned'
        ops.atomic(rr, queue)
        with patch.object(target.subprocess, 'run', side_effect=self.scheduler):
            with self.assertRaisesRegex(ValueError, 'Task owner still active'):
                target.upgrade(self.source, self.root, 'newcommit123')
        self.assertFalse(self.archive.exists())


if __name__ == '__main__':
    unittest.main()
