"""Operational safety tests; every state mutation is confined to a temporary root.

Run with the frozen application's directory on PYTHONPATH. WISP_TEST_TMPDIR may
select a writable temporary parent on a deployment host; it is never a live root.
The tests neither submit Slurm jobs nor modify scientific source/data products.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import runtime


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


class TemporaryRuntime(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix='wisp-cube-first-test-', dir=os.environ.get('WISP_TEST_TMPDIR'))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.c = {'root': str(self.root), 'scratch': str(self.root / 'scratch'),
                  'scratch_budget_bytes': 1000, 'shared_cache_reserve_bytes': 20,
                  'scratch_min_free_bytes': 100, 'month_reservation_bytes': 80}
        Path(self.c['scratch']).mkdir()
        for target, value in [('STOP', False), ('ROOT', self.root)]:
            context = patch.object(runtime, target, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.dict(runtime._claims, {}, clear=True)
        context.start()
        self.addCleanup(context.stop)
        self.q = runtime.Queue(self.c)

    def task(self, **changes):
        result = {'region': 'cube-a', 'kind': 'month', 'input_status': 'queued',
                  'prediction_status': 'queued', 'status': 'queued', 'reserved': 80,
                  'dates': ['2014-01-12T00:00:00'], 'priority': 10}
        result.update(changes)
        return result

    def ledger(self, tasks=None, phase='BUILD', **changes):
        result = {'phase': phase, 'active_cube': 'cube-a',
                  'tasks': tasks if tasks is not None else {'cube-a_2014-01': self.task()},
                  'quarantine_bytes': 0}
        result.update(changes)
        write_json(self.q.path, result)
        return result


class QueueSafetyTests(TemporaryRuntime):
    def test_cpu_claims_only_active_cube_month(self):
        self.ledger({'other': self.task(region='cube-b', priority=0),
                     'validation': self.task(kind='validation', priority=0),
                     'owned': self.task(lease='other-worker', priority=0),
                     'target': self.task(priority=5)})
        key, task = self.q.claim('cpu')
        self.assertEqual(key, 'target')
        self.assertEqual(task['input_status'], 'building')
        self.assertEqual(task['prediction_status'], 'queued')
        self.assertTrue(task['lease'])
        self.assertIsNone(self.q.claim('cpu'))
        self.assertEqual(self.q.snapshot()['tasks']['other']['input_status'], 'queued')

    def test_cpu_and_gpu_phase_gates_do_not_change_tasks(self):
        for phase in ['BUILD', 'PREDICT', 'CONSOLIDATE', 'CLEANUP_WAIT', 'COMPLETE']:
            for stage in ['cpu', 'gpu']:
                if (phase, stage) in {('BUILD', 'cpu'), ('PREDICT', 'gpu')}:
                    continue
                with self.subTest(phase=phase, stage=stage):
                    before = self.ledger(phase=phase, tasks={'target': self.task(
                        input_status='queued' if stage == 'cpu' else 'ready')})
                    self.assertIsNone(self.q.claim(stage))
                    self.assertEqual(self.q.snapshot(), before)

    def test_gpu_requires_ready_input_and_queued_prediction_in_active_cube(self):
        self.ledger(phase='PREDICT', tasks={
            'other': self.task(region='cube-b', input_status='ready', priority=0),
            'missing': self.task(input_status='queued', priority=0),
            'done': self.task(input_status='ready', prediction_status='done', priority=0),
            'target': self.task(input_status='ready')})
        key, task = self.q.claim('gpu')
        self.assertEqual(key, 'target')
        self.assertEqual(task['prediction_status'], 'predicting')
        self.assertEqual(task['input_status'], 'ready')
        self.assertIsNone(self.q.claim('gpu'))

    def test_completed_prediction_with_missing_input_still_builds(self):
        task = self.task(prediction_status='done', output_sha256='existing-product')
        self.assertEqual(runtime.compatible_status(task), 'queued')
        self.ledger(tasks={'target': task})
        key, _ = self.q.claim('cpu')
        self.q.update(key, status='ready', input_fingerprint='rebuilt', job=None, pid=None)
        current = self.q.snapshot()['tasks'][key]
        self.assertEqual(current['input_status'], 'ready')
        self.assertEqual(current['prediction_status'], 'done')
        self.assertEqual(current['status'], 'done')
        self.assertEqual(current['output_sha256'], 'existing-product')
        self.assertEqual(current['reserved'], 80)
        with self.q.edit() as ledger:
            ledger['phase'] = 'PREDICT'
        self.assertIsNone(self.q.claim('gpu'))

    def test_gpu_done_preserves_input_reservation_and_cache(self):
        cache = Path(self.c['scratch']) / 'target' / 'tensor.bin'
        cache.parent.mkdir()
        cache.write_bytes(b'input must remain until cube final is validated')
        self.ledger(phase='PREDICT', tasks={'target': self.task(input_status='ready')})
        key, _ = self.q.claim('gpu')
        self.q.update(key, status='done', reserved=0, output_sha256='prediction',
                      job=None, pid=None)
        task = self.q.snapshot()['tasks'][key]
        self.assertEqual(task['reserved'], 80)
        self.assertEqual(task['input_status'], 'ready')
        self.assertEqual(task['prediction_status'], 'done')
        self.assertIsNone(task['lease'])
        self.assertTrue(cache.exists())
        with self.assertRaisesRegex(RuntimeError, 'Stale'):
            self.q.update(key, status='done')

    def test_replaced_lease_rejects_stale_worker_without_changing_ledger(self):
        self.ledger()
        key, _ = self.q.claim('cpu')
        with self.q.edit() as ledger:
            ledger['tasks'][key]['lease'] = 'replacement-owner'
        before = self.q.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'Stale'):
            self.q.update(key, status='ready')
        self.assertEqual(self.q.snapshot(), before)

    def test_missing_local_lease_cannot_update_even_with_matching_process_identity(self):
        job, pid, host = runtime.process_id()
        self.ledger(tasks={'target': self.task(input_status='building', stage='cpu',
                                             lease=None, job=job, pid=pid, host=host)})
        before = self.q.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'Stale'):
            self.q.update('target', status='ready')
        self.assertEqual(self.q.snapshot(), before)

    def test_matching_lease_cannot_be_used_by_another_process(self):
        self.ledger()
        key, _ = self.q.claim('cpu')
        job, pid, host = runtime.process_id()
        with patch.object(runtime, 'process_id', return_value=(job, pid + 1, host)):
            with self.assertRaisesRegex(RuntimeError, 'Stale'):
                self.q.update(key, status='ready')

    def test_stop_file_and_in_process_stop_prevent_claims(self):
        before = self.ledger()
        stop = self.root / 'state/STOP'
        stop.write_text('test stop')
        self.assertIsNone(self.q.claim('cpu'))
        stop.unlink()
        with patch.object(runtime, 'STOP', True):
            self.assertIsNone(self.q.claim('cpu'))
        self.assertEqual(self.q.snapshot(), before)

    def test_global_cache_budget_counts_other_cubes_quarantine_and_shared_cache(self):
        # 80 active + 800 inactive + 101 quarantine + 20 shared > 1000.
        before = self.ledger(tasks={'target': self.task(),
                                    'other': self.task(region='cube-b', reserved=800)},
                             quarantine_bytes=101)
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            self.q.claim('cpu')
        self.assertEqual(self.q.snapshot(), before)

    def test_exact_budget_boundary_is_usable(self):
        self.ledger(tasks={'target': self.task(reserved=880)}, quarantine_bytes=100)
        with patch.object(runtime.shutil, 'disk_usage', return_value=SimpleNamespace(free=100)):
            self.assertIsNotNone(self.q.claim('cpu'))

    def test_free_space_guard_does_not_claim_or_change_reservations(self):
        before = self.ledger()
        with patch.object(runtime.shutil, 'disk_usage', return_value=SimpleNamespace(free=99)):
            with self.assertRaisesRegex(RuntimeError, 'free-space'):
                self.q.claim('cpu')
        self.assertEqual(self.q.snapshot(), before)

    def test_queue_read_failure_is_not_an_empty_queue(self):
        with self.assertRaises(FileNotFoundError):
            self.q.snapshot()
        write_json(self.q.path, {'tasks': []})
        with self.assertRaisesRegex(ValueError, 'Invalid queue'):
            self.q.claim('cpu')

    def test_recovery_requires_terminal_scheduler_evidence(self):
        self.ledger()
        with patch.object(runtime, 'process_id', return_value=('123', 321, 'worker-host')):
            key, _ = self.q.claim('cpu')
        before = self.q.snapshot()
        with patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout='123|RUNNING|\n')):
            runtime.recover(self.c, set())
        self.assertEqual(self.q.snapshot(), before)
        with patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout='123|FAILED|\n')):
            runtime.recover(self.c, set())
        current = self.q.snapshot()['tasks'][key]
        self.assertEqual(current['input_status'], 'queued')
        self.assertIsNone(current['lease'])
        with self.assertRaisesRegex(RuntimeError, 'Stale'):
            self.q.update(key, status='ready')


class PosixLockTests(TemporaryRuntime):
    def test_nested_lock_rejected_without_releasing_outer_lock(self):
        lock_path = self.root / 'locks/example.lock'
        with runtime.locked(lock_path):
            with self.assertRaisesRegex(RuntimeError, 'Nested POSIX lock'):
                with runtime.locked(lock_path):
                    self.fail('Nested acquisition unexpectedly succeeded')
            self.assertEqual(self.try_external_lock(lock_path), 11)
        self.assertEqual(self.try_external_lock(lock_path), 0)

    def try_external_lock(self, path):
        program = """import fcntl, sys
with open(sys.argv[1], 'a+') as handle:
    try:
        fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(11)
"""
        result = subprocess.run([sys.executable, '-c', program, str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=10)
        self.assertIn(result.returncode, [0, 11], result.stderr)
        return result.returncode

    def test_lock_excludes_another_process_and_retains_stable_inode(self):
        lock_path = self.root / 'locks/example.lock'
        with runtime.locked(lock_path):
            inode = lock_path.stat().st_ino
            self.assertEqual(self.try_external_lock(lock_path), 11)
        self.assertEqual(self.try_external_lock(lock_path), 0)
        self.assertEqual(lock_path.stat().st_ino, inode)


class FrozenConfigurationTests(TemporaryRuntime):
    def setUp(self):
        super().setUp()
        self.code = self.root / 'code'
        self.code.mkdir()
        self.source = self.code / 'operation.py'
        self.source.write_text('operational code only\n')
        self.original = {'root': '/frozen/root', 'precision': 'bfloat16',
                         'channels': ['temperature', 'precipitation'],
                         'model': {'checkpoint': 'frozen.pt'}}
        self.identity = {'fingerprint': 'frozen-scientific-identity'}
        write_json(self.root / 'manifests/deployment.json', self.identity)
        write_json(self.code / 'release.json', {'files': {
            'operation.py': runtime.file_hash(self.source)}})
        for context in [patch.object(runtime, 'HERE', self.code),
                        patch.object(runtime.common, 'config', return_value=self.original),
                        patch.object(runtime, '_original_verify', return_value=self.identity)]:
            context.start()
            self.addCleanup(context.stop)

    def test_only_operational_root_may_differ_from_frozen_configuration(self):
        c = dict(self.original, root=str(self.root))
        self.assertEqual(runtime.verify_execution(c), self.identity)
        runtime._original_verify.assert_called_once_with(self.original)
        self.assertEqual(runtime.config(), c)
        self.assertEqual(self.original['root'], '/frozen/root')
        for field, value in [('precision', 'float32'), ('root', '/another/root'),
                             ('channels', ['temperature']),
                             ('model', {'checkpoint': 'different.pt'}),
                             ('new_option', 'unapproved')]:
            with self.subTest(field=field):
                changed = copy.deepcopy(c)
                changed[field] = value
                with self.assertRaisesRegex(ValueError, 'Only operational output root'):
                    runtime.verify_execution(changed)

    def test_frozen_deployment_identity_must_match(self):
        write_json(self.root / 'manifests/deployment.json', {'fingerprint': 'changed'})
        with self.assertRaisesRegex(ValueError, 'scientific deployment identity'):
            runtime.verify_execution(dict(self.original, root=str(self.root)))

    def test_operational_release_hash_must_match(self):
        self.source.write_text('changed after release\n')
        with self.assertRaisesRegex(ValueError, 'Operational release changed'):
            runtime.verify_execution(dict(self.original, root=str(self.root)))


@unittest.skipUnless((Path(__file__).parent / 'products.py').exists(), 'products.py unavailable')
class CleanupIntegrationTests(TemporaryRuntime):
    def setUp(self):
        super().setUp()
        import products
        self.products = products
        self.key = 'cube-a_2014-01'
        self.input_marker = Path(self.c['scratch']) / 'inputs' / self.key / 'tensor.bin'
        self.input_marker.parent.mkdir(parents=True)
        self.input_marker.write_bytes(b'input still needed')
        self.monthly = self.root / 'monthly/REG' / (self.key + '.nc')
        self.monthly.parent.mkdir(parents=True)
        self.monthly.write_bytes(b'monthly output still needed')
        write_json(self.root / 'manifests/regions.json', {'cube-a': {'region_id': 'REG'}})

    def cleanup_state(self):
        tasks = {'cube-a_%04d-%02d' % (year, month): self.task(
            input_status='ready', prediction_status='done', lease=None, job=None, pid=None)
            for year in range(2014, 2024) for month in range(1, 13)}
        final = {'completed_epoch': 1000, 'sha256': 'verified-final',
                 'monthly_receipts': [{'key': key} for key in tasks]}
        self.ledger(tasks=tasks, phase='CLEANUP_WAIT', cleanup_authorized=True,
                    cleanup_not_before_epoch=1600)
        return final

    def assert_products_retained(self):
        self.assertEqual(self.input_marker.read_bytes(), b'input still needed')
        self.assertEqual(self.monthly.read_bytes(), b'monthly output still needed')

    def test_gpu_cleanup_request_only_publishes_validation(self):
        before = self.ledger(tasks={self.key: self.task(input_status='ready',
                                                       prediction_status='predicting')})
        receipt = {'sha256': 'monthly-hash'}
        report = {'passed': True, 'sha256': receipt['sha256']}
        with patch.object(self.products, 'validate_month', return_value=report) as validate:
            runtime.request_cleanup(self.c, self.key, receipt)
        validate.assert_called_once_with(self.c, self.key, receipt)
        self.assertEqual(runtime.read_json(self.root / 'manifests/tasks' / self.key /
                                           'prediction_verified.json'), report)
        self.assertEqual(self.q.snapshot(), before)
        self.assert_products_retained()

    def test_cleanup_cannot_delete_without_a_valid_final_product(self):
        self.cleanup_state()
        before = self.q.snapshot()
        with patch.object(self.products, 'validate_final', side_effect=ValueError('final missing')):
            with self.assertRaisesRegex(ValueError, 'final missing'):
                self.products.cleanup_cube(self.c, 'cube-a')
        self.assertEqual(self.q.snapshot(), before)
        self.assert_products_retained()

    def test_gpu_terminal_recovery_reuses_only_validated_output_and_retains_input(self):
        self.ledger(tasks={self.key: self.task(input_status='ready')}, phase='PREDICT')
        with patch.object(runtime, 'process_id', return_value=('123', 321, 'worker-host')):
            self.q.claim('gpu')
        receipt = {'sha256': 'existing-valid-output'}
        write_json(self.root / 'manifests/tasks' / self.key / 'output.json', receipt)
        before = self.q.snapshot()
        with patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout='123|FAILED|\n')), \
                patch.object(self.products, 'validate_month', side_effect=ValueError('bad checksum')):
            with self.assertRaisesRegex(ValueError, 'bad checksum'):
                runtime.recover(self.c, set())
        self.assertEqual(self.q.snapshot(), before)
        with patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout='123|FAILED|\n')), \
                patch.object(self.products, 'validate_month', return_value={'passed': True}) as validate:
            runtime.recover(self.c, set())
        validate.assert_called_once_with(self.c, self.key, receipt)
        task = self.q.snapshot()['tasks'][self.key]
        self.assertEqual(task['prediction_status'], 'done')
        self.assertEqual(task['input_status'], 'ready')
        self.assertEqual(task['output_sha256'], receipt['sha256'])
        self.assertEqual(task['reserved'], 80)
        self.assertIsNone(task['lease'])
        self.assert_products_retained()

    def test_real_queue_cleanup_gate_blocks_early_phase_delay_and_leases(self):
        cases = [('less-than-600-seconds', {}, {}, 1599),
                 ('wrong-phase', {'phase': 'PREDICT'}, {}, 2000),
                 ('unauthorized', {'cleanup_authorized': False}, {}, 2000),
                 ('shortened-delay', {'cleanup_not_before_epoch': 1599}, {}, 2000),
                 ('live-lease', {}, {'lease': 'active-worker'}, 2000)]
        for label, changes, task_changes, clock in cases:
            with self.subTest(case=label):
                final = self.cleanup_state()
                with self.q.edit() as ledger:
                    ledger.update(changes)
                    ledger['tasks'][self.key].update(task_changes)
                before = self.q.snapshot()
                with patch.object(self.products, 'validate_final', return_value=final), \
                        patch.object(self.products.time, 'time', return_value=clock):
                    with self.assertRaises(ValueError):
                        self.products.cleanup_cube(self.c, 'cube-a')
                self.assertEqual(self.q.snapshot(), before)
                self.assert_products_retained()


@unittest.skipUnless((Path(__file__).parent / 'controller.py').exists(), 'controller.py unavailable')
class ControllerTests(TemporaryRuntime):
    def setUp(self):
        super().setUp()
        import controller
        self.controller = controller
        code = self.root / 'code'
        code.mkdir()
        (code / 'release.json').write_text('{"release":"test"}\n')
        context = patch.object(runtime, 'HERE', code)
        context.start()
        self.addCleanup(context.stop)

    def test_submission_uses_bounded_resources_and_never_big_partition(self):
        expected = {'control': ('1', '2G'), 'cpu': ('4', '16G'),
                    'verify': ('4', '16G'), 'gpu': ('8', '64G'),
                    'merge': ('2', '8G'), 'cleanup': ('1', '4G')}
        for kind, (cpus, memory) in expected.items():
            with self.subTest(kind=kind), \
                    patch.object(self.controller, 'jobs', return_value=[]), \
                    patch.object(self.controller.subprocess, 'run',
                                 return_value=SimpleNamespace(stdout='123;cluster\n')) as run, \
                    patch('builtins.print'):
                self.assertEqual(self.controller.submit(self.c, kind), '123')
            argv = run.call_args[0][0]
            self.assertIn('--cpus-per-task=' + cpus, argv)
            self.assertIn('--mem=' + memory, argv)
            partitions = next(arg.split('=', 1)[1].split(',') for arg in argv
                              if arg.startswith('--partition='))
            self.assertNotIn('big', partitions)
            if kind == 'gpu':
                self.assertIn('--gres=gpu:A100:1', argv)
            else:
                self.assertFalse(any(arg.startswith('--gres=') for arg in argv))
            record = runtime.read_json(self.root / 'state/submissions/123.json')
            self.assertEqual(record['args'], argv)
            self.assertEqual(record['kind'], kind)

    def test_submission_refuses_120_existing_jobs_without_calling_slurm(self):
        current = [{'id': str(i), 'kind': 'control'} for i in range(120)]
        with patch.object(self.controller, 'jobs', return_value=current), \
                patch.object(self.controller.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, '120'):
                self.controller.submit(self.c, 'cpu')
        run.assert_not_called()

    def test_submission_refuses_sixty_existing_cpu_jobs_without_calling_slurm(self):
        current = [{'id': str(i), 'kind': 'cpu'} for i in range(60)]
        with patch.object(self.controller, 'jobs', return_value=current), \
                patch.object(self.controller.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, '60'):
                self.controller.submit(self.c, 'cpu')
        run.assert_not_called()

    def test_controller_renewal_can_wait_for_current_job_to_release_lock(self):
        with patch.object(self.controller, 'jobs', return_value=[]), \
                patch.object(self.controller.subprocess, 'run',
                             return_value=SimpleNamespace(stdout='124\n')) as run, \
                patch('builtins.print'):
            self.controller.submit(self.c, 'control', dependency='123')
        self.assertIn('--dependency=afterany:123', run.call_args[0][0])

    def test_cleanup_worker_waits_until_deadline_before_deleting(self):
        import products
        self.ledger(phase='CLEANUP_WAIT', cleanup_not_before_epoch=1600)
        current_time = [1599]
        calls = []
        def advance_clock(seconds):
            calls.append(('wait', seconds))
            current_time[0] += seconds
        def cleanup(config, token):
            self.assertGreaterEqual(current_time[0], 1600)
            calls.append(('cleanup', token))
            with self.q.edit() as state:
                state['phase'] = 'CLEANED'
        with patch.object(self.controller.time, 'time', side_effect=lambda: current_time[0]), \
                patch.object(self.controller.time, 'monotonic', return_value=0), \
                patch.object(self.controller.time, 'sleep', side_effect=advance_clock), \
                patch.object(products, 'cleanup_cube', side_effect=cleanup):
            self.controller.cleanup(self.c)
        self.assertEqual(calls, [('wait', 1), ('cleanup', 'cube-a')])

    def test_gpu_initialization_failure_records_stage_error_before_any_claim(self):
        def fail_gpu(config):
            raise RuntimeError('GPU initialization failed before claiming work')
        pipeline = SimpleNamespace(gpu_worker=fail_gpu)
        with patch.object(sys, 'argv', ['controller.py', 'gpu']), \
                patch.object(runtime, 'config', return_value=self.c), \
                patch.object(runtime, 'verify_execution'), patch.object(runtime, 'install'), \
                patch.dict(sys.modules, {'wisp_stream.pipeline': pipeline}):
            with self.assertRaisesRegex(RuntimeError, 'GPU initialization failed'):
                self.controller.main()
        report = runtime.read_json(self.root / 'reports/stage_error.json')
        self.assertEqual(report['command'], 'gpu')
        self.assertIn('before claiming work', report['error'])

    def test_controller_scales_from_six_pilot_jobs_to_at_most_sixty(self):
        for passed, existing, expected in [(False, 0, 6), (False, 3, 3),
                                            (True, 0, 60), (True, 59, 1), (True, 60, 0)]:
            with self.subTest(passed=passed, existing=existing):
                self.ledger(tasks={'month-%03d' % i: self.task() for i in range(120)})
                jobs = [{'id': str(i), 'kind': 'cpu'} for i in range(existing)]
                with patch.object(self.controller, 'jobs', return_value=jobs), \
                        patch.object(runtime, 'recover'), \
                        patch.object(self.controller, 'pilot_gate', return_value=passed), \
                        patch.object(self.controller, 'submit') as submit, \
                        patch.object(self.controller, 'status'):
                    self.controller.control(self.c, once=True)
                self.assertEqual(submit.call_count, expected)
                self.assertTrue(all(call.args == (self.c, 'cpu') for call in submit.call_args_list))

    def test_controller_pauses_on_blocked_month_without_submitting(self):
        self.ledger(tasks={'target': self.task(input_status='blocked')})
        with patch.object(self.controller, 'jobs', return_value=[]), \
                patch.object(runtime, 'recover'), \
                patch.object(self.controller, 'submit') as submit, \
                patch.object(self.controller, 'status'):
            self.controller.control(self.c, once=True)
        submit.assert_not_called()
        self.assertTrue((self.root / 'reports/blocked.json').exists())

    def test_controller_requires_all_120_ready_months_before_input_verification(self):
        for count, expected_phase in [(119, 'BUILD'), (120, 'INPUT_VERIFY')]:
            with self.subTest(months=count):
                self.ledger(tasks={'month-%03d' % i: self.task(input_status='ready')
                                   for i in range(count)})
                with patch.object(self.controller, 'jobs', return_value=[]), \
                        patch.object(runtime, 'recover'), \
                        patch.object(self.controller, 'pilot_gate', return_value=True), \
                        patch.object(self.controller, 'submit') as submit, \
                        patch.object(self.controller, 'status'):
                    self.controller.control(self.c, once=True)
                self.assertEqual(self.q.snapshot()['phase'], expected_phase)
                submit.assert_not_called()

    def pilot_fixture(self):
        for i in range(6):
            jid = str(i + 100)
            write_json(self.root / 'state/submissions' / (jid + '.json'),
                       {'job': jid, 'kind': 'cpu', 'at': '2026-11-04T00:00:%02dZ' % i})
            write_json(self.root / 'state/job_resources' / (jid + '.json'),
                       {'job': jid, 'max_aggregate_rss_bytes': 8 * 2**30,
                        'resource_error': False, 'child_exit_codes': [0, 0]})
            write_json(self.root / 'reports/qualification' / (jid + '.json'), {'passed': True})
            for n in range(2):
                pid = 1000 + i * 2 + n
                write_json(self.root / 'state/workers' / ('cpu_%s_%d.json' % (jid, pid)),
                           {'job': jid, 'pid': pid, 'fresh_days': [
                               {'task': 'cube-a_2014-01', 'date': '2014-01-%02d' % day}
                               for day in [12, 13, 14]]})
                write_json(self.root / 'reports/pilot_parity' / ('%s_%d.json' % (jid, pid)),
                           {'passed': True})

    def test_pilot_requires_six_jobs_two_producers_three_new_days_and_parity(self):
        self.pilot_fixture()
        worker_path = self.root / 'state/workers/cpu_100_1000.json'
        worker = runtime.read_json(worker_path)
        worker['fresh_days'].pop()
        write_json(worker_path, worker)
        self.assertFalse(self.controller.pilot_gate(self.c))
        self.pilot_fixture()
        (self.root / 'state/workers/cpu_100_1000.json').unlink()
        self.assertFalse(self.controller.pilot_gate(self.c))
        self.pilot_fixture()
        (self.root / 'reports/pilot_parity/100_1000.json').unlink()
        self.assertFalse(self.controller.pilot_gate(self.c))
        self.pilot_fixture()
        (self.root / 'state/submissions/100.json').unlink()
        self.assertFalse(self.controller.pilot_gate(self.c))
        self.pilot_fixture()
        (self.root / 'reports/qualification/100.json').unlink()
        self.assertFalse(self.controller.pilot_gate(self.c))
        self.pilot_fixture()
        write_json(self.root / 'state/job_resources/100.json',
                   {'job': '100', 'max_aggregate_rss_bytes': 0})
        self.assertFalse(self.controller.pilot_gate(self.c))
        self.assertFalse((self.root / 'reports/pilot_passed.json').exists())
        self.pilot_fixture()
        self.assertTrue(self.controller.pilot_gate(self.c))
        result = runtime.read_json(self.root / 'reports/pilot_passed.json')
        self.assertEqual(result['producers'], 12)
        self.assertEqual(len(result['jobs']), 6)
        self.assertTrue(result['seven_input_direct_reference_checks'])

    def test_pilot_rejects_resource_failure_and_rss_over_twelve_gib(self):
        for field, value in [('resource_error', True), ('max_aggregate_rss_bytes', 12 * 2**30 + 1)]:
            with self.subTest(field=field):
                self.pilot_fixture()
                path = self.root / 'state/job_resources/100.json'
                report = runtime.read_json(path)
                report[field] = value
                write_json(path, report)
                with self.assertRaisesRegex(ValueError, 'resource gate'):
                    self.controller.pilot_gate(self.c)
                self.assertFalse((self.root / 'reports/pilot_passed.json').exists())

    def test_pilot_acceptance_is_bound_to_operational_release(self):
        self.pilot_fixture()
        self.assertTrue(self.controller.pilot_gate(self.c))
        (runtime.HERE / 'release.json').write_text('{"release":"different"}\n')
        with self.assertRaisesRegex(ValueError, 'another code release'):
            self.controller.pilot_gate(self.c)


if __name__ == '__main__':
    unittest.main(verbosity=2)
