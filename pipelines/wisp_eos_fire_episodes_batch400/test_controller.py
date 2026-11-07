"""Controller policy tests with fake scheduler and file-backed region queues."""
import contextlib
import errno
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


ops = module('_batch400_controller_test_common', 'common.py')
with patch.dict(sys.modules, common=ops):
    ctl = module('_batch400_controller_test_target', 'controller.py')


class Queue:
    def __init__(self, c):
        self.path = Path(c['root']) / 'state/queue.json'

    def snapshot(self):
        return ops.read(self.path)

    @contextlib.contextmanager
    def edit(self):
        state = ops.read(self.path)
        yield state
        ops.atomic(self.path, state)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'campaign'
        self.token = 'T000177'
        self.rr = self.root / 'regions' / self.token
        self.c = {'root': str(self.rr)}
        self.ad = types.SimpleNamespace(Queue=Queue, verify_wave=Mock())
        ops.atomic(self.root / 'config/campaign.json', {
            'max_cpu_jobs': 320, 'partitions': 'work,small,gpu'})
        ops.atomic(self.root / 'code/release.json', {'version': 'test'})

    def state(self, phase='WAVE_VERIFY', active=0):
        state = dict(phase=phase, active_wave=active,
                     waves=[{'wave_id': 0, 'keys': ['a']}, {'wave_id': 1, 'keys': ['b']}],
                     tasks={'a': {'input_status': 'cleaned'}, 'b': {'input_status': 'cleaned'}})
        ops.atomic(self.rr / 'state/queue.json', state)
        return state

    def test_total_and_lane_caps_prevent_scheduler_submission(self):
        cases = [([dict(kind='cpu', lane=i % 10) for i in range(320)], 0),
                 ([dict(kind='cpu', lane=3) for _ in range(32)], 3)]
        for current, lane in cases:
            with patch.object(ctl, 'jobs', return_value=current), patch.object(ctl.subprocess, 'run') as run:
                self.assertIsNone(ctl.submit(self.root, 'cpu', self.token, lane))
                run.assert_not_called()

    def test_global_producer_cap_is_independent_of_resident_region_count(self):
        cfg=ops.read(self.root/'config/campaign.json')
        cfg['max_total_producers']=210
        ops.atomic(self.root/'config/campaign.json',cfg)
        current=[dict(kind='cpu',lane=i%20) for i in range(210)]
        with patch.object(ctl,'jobs',return_value=current), patch.object(ctl.subprocess,'run') as run:
            self.assertIsNone(ctl.submit(self.root,'cpu',self.token,19))
            run.assert_not_called()

    def test_waiting_gpu_regions_do_not_block_input_admission(self):
        tokens=['T%06d'%i for i in range(10)]
        for token in tokens:
            ops.atomic(self.root/'regions'/token/'state/queue.json',{'phase':'PREDICT'})
        cfg=dict(max_build_regions=10,scratch=str(self.root),region_budget_bytes=256,
                 backlog_min_free_bytes=1024)
        state={'lanes':tokens+[None]*10}
        with patch.object(ctl.shutil,'disk_usage',return_value=types.SimpleNamespace(free=2000)):
            self.assertTrue(ctl.admit_more_inputs(self.root,state,cfg))
        with patch.object(ctl.shutil,'disk_usage',return_value=types.SimpleNamespace(free=1000)):
            self.assertFalse(ctl.admit_more_inputs(self.root,state,cfg))
        for token in tokens:
            ops.atomic(self.root/'regions'/token/'state/queue.json',{'phase':'BUILD'})
        self.assertFalse(ctl.admit_more_inputs(self.root,state,cfg))

    def test_cpu_submission_does_not_request_gpu_or_big_partition(self):
        response = types.SimpleNamespace(stdout='12345\n')
        with patch.object(ctl, 'jobs', return_value=[]), \
                patch.object(ctl.subprocess, 'run', return_value=response) as run:
            self.assertEqual(ctl.submit(self.root, 'cpu', self.token, 0), '12345')
        args = run.call_args.args[0]
        self.assertIn('--partition=work,small,gpu', args)
        self.assertIn('--cpus-per-task=4', args)
        self.assertIn('--mem=16G', args)
        self.assertIn('--time=24:00:00', args)
        self.assertFalse(any(arg.startswith('--gres') for arg in args))
        record = ops.read(self.root / 'state/submissions/12345.json')
        self.assertEqual(record['token'], self.token)
        self.assertEqual(record['lane'], 0)

    def test_cpu_consumer_submission_and_cap(self):
        cfg=ops.read(self.root/'config/campaign.json');cfg['max_cpu_consumers']=8
        ops.atomic(self.root/'config/campaign.json',cfg)
        with patch.object(ctl,'jobs',return_value=[]),patch.object(ctl.subprocess,'run',return_value=types.SimpleNamespace(stdout='12346')) as run:
            self.assertEqual(ctl.submit(self.root,'cpu-infer'),'12346')
            args=run.call_args.args[0]
            self.assertIn('--cpus-per-task=8',args)
            self.assertIn('--mem=32G',args)
            self.assertFalse(any(x.startswith('--gres') for x in args))
        with patch.object(ctl,'jobs',return_value=[dict(kind='cpu-infer',lane=None)]*8),patch.object(ctl.subprocess,'run') as run:
            self.assertIsNone(ctl.submit(self.root,'cpu-infer'));run.assert_not_called()

    def test_gpu_waits_for_legacy_handover_and_existing_gpu(self):
        for handed_over, rows in [(False, []), (True, [dict(name='wisp1c-gpu')])]:
            with patch.object(ctl, 'jobs', return_value=[]), \
                    patch.object(ctl, 'LEGACY', self.root / 'legacy'), \
                    patch.object(ctl, 'legacy_gpu_handover_ready', return_value=handed_over), \
                    patch.object(ctl, 'slurm_rows', return_value=rows), \
                    patch.object(ctl.subprocess, 'run') as run:
                self.assertIsNone(ctl.submit(self.root, 'gpu'))
                run.assert_not_called()

    def test_gpu_submission_requests_exactly_one_a100(self):
        with patch.object(ctl, 'jobs', return_value=[]), \
                patch.object(ctl, 'LEGACY', self.root / 'legacy'), \
                patch.object(ctl, 'legacy_gpu_handover_ready', return_value=True), \
                patch.object(ctl, 'slurm_rows', return_value=[]), \
                patch.object(ctl.subprocess, 'run', return_value=types.SimpleNamespace(stdout='54321')) as run:
            ctl.submit(self.root, 'gpu')
        args = run.call_args.args[0]
        self.assertIn('--gres=gpu:A100:1', args)
        self.assertIn('--partition=gpu', args)

    def test_wave_verification_uses_immutable_receipt_time_for_cleanup(self):
        self.state()
        products = types.SimpleNamespace(verify_wave_outputs=Mock(return_value={'completed_epoch': 1000}),
                                        cleanup_wave=Mock(), finalize_region=Mock())
        with patch.object(ctl, 'activate', return_value=(self.c, self.ad)), \
                patch.dict(sys.modules, batch_products=products):
            ctl.phase_job(self.root, self.token, 'wave')
        state = ops.read(self.rr / 'state/queue.json')
        self.assertEqual(state['phase'], 'CLEANUP_WAIT')
        self.assertEqual(state['cleanup_not_before_epoch'], 1600)
        self.assertTrue(state['cleanup_authorized'])

    def test_pending_cleanup_never_advances_wave(self):
        original = self.state('CLEANUP_WAIT')
        products = types.SimpleNamespace(verify_wave_outputs=Mock(),
                                        cleanup_wave=Mock(return_value={'pending': True}), finalize_region=Mock())
        with patch.object(ctl, 'activate', return_value=(self.c, self.ad)), \
                patch.dict(sys.modules, batch_products=products):
            ctl.phase_job(self.root, self.token, 'cleanup')
        self.assertEqual(original, ops.read(self.rr / 'state/queue.json'))

    def test_cleaned_wave_advances_and_last_wave_enters_merge(self):
        products = types.SimpleNamespace(verify_wave_outputs=Mock(),
                                        cleanup_wave=Mock(return_value={'cleaned': True}), finalize_region=Mock())
        for active, expected in [(0, 'BUILD'), (1, 'MERGE')]:
            self.state('CLEANUP_WAIT', active=active)
            with patch.object(ctl, 'activate', return_value=(self.c, self.ad)), \
                    patch.dict(sys.modules, batch_products=products):
                ctl.phase_job(self.root, self.token, 'cleanup')
            state = ops.read(self.rr / 'state/queue.json')
            self.assertEqual(state['phase'], expected)
            self.assertFalse(state['cleanup_authorized'])
            self.assertEqual(state['active_wave'], 1)

    def test_final_publication_records_complete_only_after_validator_returns(self):
        self.state('MERGE')
        receipt = {'validated': True, 'path': '/durable/result.nc', 'sha256': 'abc'}
        products = types.SimpleNamespace(verify_wave_outputs=Mock(), cleanup_wave=Mock(),
                                        finalize_region=Mock(return_value=receipt))
        with patch.object(ctl, 'activate', return_value=(self.c, self.ad)), \
                patch.dict(sys.modules, batch_products=products):
            ctl.phase_job(self.root, self.token, 'final')
        state = ops.read(self.rr / 'state/queue.json')
        self.assertEqual(state['phase'], 'COMPLETE')
        self.assertEqual(state['final'], receipt)
        self.state('MERGE')
        products.finalize_region.side_effect = ValueError('failed validation')
        with patch.object(ctl, 'activate', return_value=(self.c, self.ad)), \
                patch.dict(sys.modules, batch_products=products), self.assertRaises(ValueError):
            ctl.phase_job(self.root, self.token, 'final')
        self.assertEqual(ops.read(self.rr / 'state/queue.json')['phase'], 'MERGE')

    def test_transient_retry_count_survives_archiving_and_is_per_wave_stage(self):
        self.state()
        path = self.rr / 'reports/stage_error.json'
        for attempt in (1, 2, 3):
            ctl.record_failure(self.root, self.token, 'wave', OSError(errno.EIO, 'temporary source outage'))
            report = ops.read(path)
            self.assertEqual(report['attempts'], attempt)
            self.assertTrue(report['transient'])
            path.rename(self.rr / 'reports/errors' / ('archive_%d.json' % attempt))
        ctl.record_failure(self.root, self.token, 'final', ValueError('deterministic'))
        self.assertEqual(ops.read(path)['attempts'], 1)
        self.assertFalse(ops.read(path)['transient'])
        self.state(active=1)
        ctl.record_failure(self.root, self.token, 'wave', OSError(errno.EIO, 'temporary'))
        self.assertEqual(ops.read(path)['attempts'], 1)

    def test_producer_target_and_ready_order_do_not_oversubmit_or_cherry_pick(self):
        self.assertEqual(ops.producer_target(200, 0), 30)
        self.assertEqual(ops.producer_target(59, 29), 1)
        self.assertEqual(ops.producer_target(1, 1), 0)
        self.assertEqual(ops.producer_target(0, 0), 0)
        regions = {'T000003': {'status': 'READY', 'selected_days': 20},
                   'T000001': {'status': 'READY', 'selected_days': 10},
                   'T000002': {'status': 'READY', 'selected_days': 20},
                   'T000004': {'status': 'ACTIVE', 'selected_days': 100}}
        self.assertEqual(ops.ready_order(regions), ['T000002', 'T000003', 'T000001'])

    def registered_job(self, job_id, token=None, kind='cpu', state='PENDING', name=None):
        token = token or self.token
        name = name or ops.PREFIX + kind
        record = dict(job=job_id, name=name, kind=kind, token=token, lane=0,
                      args=['sbatch', '--chdir=' + str(self.root), str(self.root / 'code/run.sh')],
                      release_sha256=ops.sha(self.root / 'code/release.json'))
        ops.atomic(self.root / 'state/submissions' / (job_id + '.json'), record)
        return dict(id=job_id, name=name, kind=kind, token=token, lane=0, state=state)

    def test_retire_pending_producers_is_region_owned_state_guarded_and_audited(self):
        current = [self.registered_job('101'), self.registered_job('102', state='RUNNING'),
                   self.registered_job('103', token='T000178'), self.registered_job('104', kind='verify'),
                   self.registered_job('105', name='wisp2c-cpu')]
        with patch.object(ctl.subprocess, 'run', return_value=types.SimpleNamespace(stdout='')) as run:
            retired = ctl.retire_pending_producers(self.root, self.token, current)
        self.assertEqual(retired, ['101'])
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args[0], 'scancel')
        self.assertIn('--state=PENDING', args)
        self.assertIn('101', args)
        self.assertTrue(all(job not in args for job in ['102', '103', '104', '105']))
        records = list((self.root / 'reports/job_retirements').glob('*.json'))
        self.assertTrue(records)
        self.assertIn('101', ''.join(p.read_text() for p in records))

    def test_retirement_rejects_mismatched_registry_before_cancelling(self):
        current = [self.registered_job('101')]
        path = self.root / 'state/submissions/101.json'
        wrong = ops.read(path)
        wrong['token'] = 'T000178'
        ops.atomic(path, wrong)
        with patch.object(ctl.subprocess, 'run') as run, self.assertRaises(ValueError):
            ctl.retire_pending_producers(self.root, self.token, current)
        run.assert_not_called()

    def test_failed_initial_pilot_stops_gate_without_growing_cohort(self):
        state = dict(pilot_passed=False, pilot_tokens=[self.token, 'T000178'],
                     regions={self.token: {'status': 'BLOCKED', 'error': 'invalid input'},
                              'T000178': {'status': 'ACTIVE'}, 'T000179': {'status': 'READY'}})
        with self.assertRaises(ValueError):
            ctl.pilot_gate(self.root, state)
        self.assertEqual(state['pilot_tokens'], [self.token, 'T000178'])

    def test_control_stops_pilot_blocked_instead_of_admitting_third_region(self):
        state = dict(scan_complete=True, phase='PILOT', pilot_passed=False,
                     pilot_tokens=[self.token, 'T000178'], lanes=[None] * 10, lane_limit=2,
                     regions={self.token: {'status': 'BLOCKED', 'error': 'invalid input'},
                              'T000178': {'status': 'READY', 'selected_days': 100},
                              'T000179': {'status': 'READY', 'selected_days': 200}})
        ops.atomic(self.root / 'state/campaign.json', state)
        prepare = Mock()
        with patch.object(ctl, 'jobs', return_value=[]), \
                patch.object(ctl, 'submit') as submit, patch.object(ctl, 'report_status'), \
                patch.dict(sys.modules, enroll=types.SimpleNamespace(prepare_scratch=prepare)):
            ctl.control(self.root, once=True)
        result = ops.read(self.root / 'state/campaign.json')
        self.assertEqual(result['phase'], 'PILOT_BLOCKED')
        self.assertEqual(result['pilot_tokens'], [self.token, 'T000178'])
        self.assertEqual(result['regions']['T000179']['status'], 'READY')
        prepare.assert_not_called()
        submit.assert_not_called()

    def test_cpu_startup_failure_records_bounded_stage_evidence_before_fork(self):
        self.state('BUILD')
        backend = types.SimpleNamespace(qualify=Mock(side_effect=OSError(errno.EIO, 'source unavailable')))
        with patch.object(ctl, 'activate', return_value=(self.c, self.ad)), \
                patch.dict(sys.modules, {'wisp_stream': types.SimpleNamespace(backend=backend),
                                         'wisp_stream.backend': backend}), \
                patch.object(ctl.subprocess, 'Popen') as popen:
            for attempt in (1, 2, 3):
                with self.assertRaises(OSError):
                    ctl.cpu_parent(self.root, self.token)
                report = ops.read(self.rr / 'reports/stage_error.json')
                self.assertEqual(report['kind'], 'cpu-startup')
                self.assertEqual(report['attempts'], attempt)
                self.assertTrue(report['transient'])
            popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
