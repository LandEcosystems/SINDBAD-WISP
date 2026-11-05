"""Operations tests; tiny fake stores never read raw sources or run the model."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

# The workstation need not have the 821 MiB model's scientific environment.
# In Jena these tests use the actual imported common helpers automatically.
if importlib.util.find_spec('wisp_batch') is None:
    batch = types.ModuleType('wisp_batch'); batch.__path__ = []
    bc = types.ModuleType('wisp_batch.common')
    def atomic_json(path, value):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True))
    bc.atomic_json = atomic_json
    bc.file_hash = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    bc.SHAPES = {name: (1,) for name in ('hist_weather', 'hist_quasi_static', 'static_land',
                    'hist_fire', 'future_weather', 'hist_fire_any_48h', 'hist_fire_delta_norm_48h')}
    stream = types.ModuleType('wisp_stream'); stream.__path__ = []
    sc = types.ModuleType('wisp_stream.common')
    sc.now = lambda: '2026-11-05T16:00:00+00:00'
    sc.config = lambda p: dict(json.loads(Path(p).read_text()), config_path=str(p))
    sc.verify_code = lambda c: json.loads((Path(c['root']) / 'manifests/deployment.json').read_text())
    sc.task_paths = lambda c, k: (Path(c['scratch']) / 'inputs' / k, Path(c['root']) / 'manifests/tasks' / k)
    stream.common = sc
    sys.modules.update({'wisp_batch': batch, 'wisp_batch.common': bc,
                        'wisp_stream': stream, 'wisp_stream.common': sc})

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter as a


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.campaign = self.base / 'results/campaign'
        self.root = self.campaign / 'regions/T000830'
        self.scratch = self.base / 'scratch/campaign/regions/T000830'
        self.scratch.mkdir(parents=True)
        self.c = {'root': str(self.root), 'scratch': str(self.scratch),
                  'scratch_budget_bytes': a.BUDGET_BYTES,
                  'scratch_min_free_bytes': 0, 'shared_cache_reserve_bytes': 2**20,
                  'source': 'frozen', 'precision': 'bfloat16'}
        self.original = dict(self.c, root=str(self.base / 'original'),
                             scratch=str(self.base / 'old_scratch'), scratch_budget_bytes=2**40)
        self.keys = ['T000830_evt_test_B001', 'T000830_evt_test_B002']
        self.tasks = {self.keys[0]: self.task('2016-01-30T00:00:00'),
                      self.keys[1]: self.task('2016-02-10T00:00:00')}
        self.waves = [{'wave_id': i, 'keys': [key], 'days': 1,
                       'reserved_bytes': a.FIXED_CACHE_BYTES + a.rt.task_reservation(self.tasks[key])}
                      for i, key in enumerate(self.keys)]
        self.put('manifests/waves.json', {'waves': self.waves})
        self.put('state/queue.json', {'phase': 'BUILD', 'active_wave': 0,
                                    'waves': self.waves, 'tasks': self.tasks})
        self.q = a.Queue(self.c)
        a.rt.STOP = False
        a.rt._claims.clear()

    def put(self, path, value):
        a.atomic_json(self.root / path, value)

    @staticmethod
    def task(day):
        return {'region': 'T000830', 'kind': 'episode', 'dates': [day],
                'input_status': 'queued', 'prediction_status': 'queued',
                'status': 'queued', 'reserved': 0, 'priority': 10}

    def test_only_active_wave_claimed_without_removing_future_tasks(self):
        key, task = self.q.claim('cpu')
        self.assertEqual(key, self.keys[0])
        self.assertIsNone(self.q.claim('cpu'))
        self.assertEqual(set(self.q.snapshot()['tasks']), set(self.keys))
        self.q.update(key, status='ready')
        with self.q.edit() as state:
            state['phase'] = 'PREDICT'
        predicted, _ = self.q.claim('gpu')
        self.assertEqual(predicted, key)
        self.q.update(key, status='done', reserved=0)
        state = self.q.snapshot()
        self.assertEqual(state['tasks'][key]['reserved'], task['reserved'])
        self.assertEqual(state['tasks'][key]['input_status'], 'ready')
        self.assertEqual(state['tasks'][self.keys[1]]['input_status'], 'queued')
        with self.assertRaises(a.WaveFinished):
            self.q.claim('gpu')
        with self.q.edit() as state:
            state.update(active_wave=1, phase='BUILD')
            state['tasks'][key].update(input_status='cleaned', reserved=0)
        self.assertEqual(self.q.claim('cpu')[0], self.keys[1])
        self.assertEqual(set(self.q.snapshot()['tasks']), set(self.keys))

    def test_claim_respects_phases_and_existing_owner(self):
        self.assertIsNone(self.q.claim('gpu'))
        key, _ = self.q.claim('cpu')
        self.assertIsNone(self.q.claim('cpu'))
        with self.q.edit() as state:
            state['tasks'][key]['pid'] += 1
        with self.assertRaisesRegex(RuntimeError, 'Stale'):
            self.q.update(key, status='ready')

    def test_reduced_job_allowlist_stops_only_new_chunk_claims(self):
        with mock.patch.dict(os.environ, {'SLURM_JOB_ID': '101'}):
            key, task = self.q.claim('cpu')
            with self.q.edit() as state:
                state['allowed_cpu_jobs'] = ['102']
            # A non-admitted job may finish its currently leased chunk.
            self.q.update(key, status='ready')
            with self.q.edit() as state:
                state['active_wave'] = 1
            self.assertIsNone(self.q.claim('cpu'))
        with mock.patch.dict(os.environ, {'SLURM_JOB_ID': '102'}):
            self.assertEqual(self.q.claim('cpu')[0], self.keys[1])

    def test_signal_handler_preserves_controller_flag_without_recursive_chaining(self):
        original = mock.Mock()
        handlers = {a.signal.SIGTERM: original, a.signal.SIGINT: a.signal.default_int_handler}
        def install(sig, handler):
            handlers[sig] = handler
        with mock.patch.object(a.signal, 'getsignal', side_effect=lambda sig: handlers[sig]), \
             mock.patch.object(a.signal, 'signal', side_effect=install) as setter:
            a.install_signal_handlers()
            term = handlers[a.signal.SIGTERM]
            a.install_signal_handlers()
            self.assertIs(handlers[a.signal.SIGTERM], term)
            self.assertEqual(setter.call_count, 2)
            term(a.signal.SIGTERM, None)
        self.assertTrue(a.rt.STOP)
        original.assert_called_once_with(a.signal.SIGTERM, None)

    def test_claim_rejects_mutated_wave_plan(self):
        with self.q.edit() as state:
            state['waves'][0]['keys'] = [self.keys[1]]
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.q.claim('cpu')

    def test_cache_reservation_pauses_and_never_claims_future_wave(self):
        tiny = dict(self.c, scratch_budget_bytes=1)
        self.assertIsNone(a.Queue(tiny).claim('cpu'))
        state = self.q.snapshot()
        self.assertIn('256 GiB', state['admission_pause']['reason'])
        self.assertTrue(all(t['input_status'] == 'queued' for t in state['tasks'].values()))

    def test_checkpoint_resume_keeps_single_reservation(self):
        key, first = self.q.claim('cpu')
        self.q.update(key, status='queued')
        same, second = self.q.claim('cpu')
        self.assertEqual(key, same)
        self.assertEqual(first['reserved'], second['reserved'])
        self.assertNotEqual(first['lease'], second['lease'])

    def test_input_and_prediction_completion_remain_separate(self):
        key, _ = self.q.claim('cpu')
        with self.assertRaisesRegex(ValueError, 'stage transition'):
            self.q.update(key, status='done')
        self.assertEqual(self.q.snapshot()['tasks'][key]['prediction_status'], 'queued')

    def test_scientific_settings_and_paths_cannot_change(self):
        a.check_operational_config(self.c, self.original, self.campaign)
        for field in ['precision', 'source']:
            with self.assertRaisesRegex(ValueError, 'scientific'):
                a.check_operational_config(dict(self.c, **{field: 'changed'}), self.original, self.campaign)
        with self.assertRaisesRegex(ValueError, '256 GiB'):
            a.check_operational_config(dict(self.c, scratch_budget_bytes=2**40), self.original, self.campaign)
        with self.assertRaisesRegex(ValueError, 'independent'):
            a.check_operational_config(dict(self.c, scratch=self.original['scratch']), self.original, self.campaign)
        with self.assertRaisesRegex(ValueError, 'root'):
            a.check_operational_config(dict(self.c, root=str(self.campaign / 'other/T000830')),
                                       self.original, self.campaign)

    def test_waves_partition_complete_tasks_and_enforce_reserved_bound(self):
        a.validate_waves(self.waves, self.tasks)
        for mutation in (self.waves[:1], self.waves + [self.waves[0]]):
            with self.assertRaises(ValueError):
                a.validate_waves(mutation, self.tasks)
        mutated = copy.deepcopy(self.waves)
        mutated[0]['reserved_bytes'] += 1
        with self.assertRaisesRegex(ValueError, 'reservation'):
            a.validate_waves(mutated, self.tasks)

    def test_immutable_region_identity_required(self):
        deployment = {'fingerprint': 'frozen'}
        a.atomic_json(Path(self.original['root']) / 'manifests/deployment.json', deployment)
        self.put('manifests/deployment.json', deployment)
        self.put('config/deployment.json', self.c)
        self.put('manifests/selection.json', {'regions': {'T000830': {}}, 'tasks': self.tasks})
        self.put('manifests/regions.json', {'T000830': {'cube_sha256': 'cube'}})
        self.put('manifests/parity_dates.json', {'T000830': [self.tasks[self.keys[0]]['dates'][0]]})
        files = {'config_sha256': 'config/deployment.json', 'selection_sha256': 'manifests/selection.json',
                 'regions_sha256': 'manifests/regions.json', 'parity_dates_sha256': 'manifests/parity_dates.json',
                 'waves_sha256': 'manifests/waves.json'}
        identity = {key: a.file_hash(self.root / path) for key, path in files.items()}
        self.put('manifests/identity.json', dict(identity, deployment_fingerprint='frozen'))
        with mock.patch.object(a, '_CAMPAIGN', self.campaign), \
             mock.patch.object(a.common, 'config', return_value=self.original), \
             mock.patch.object(a, '_ORIGINAL_VERIFY', return_value=deployment), \
             mock.patch.object(a, 'verify_release', return_value={}):
            self.assertEqual(a.verify_execution(self.c), deployment)
            self.put('manifests/parity_dates.json', {'T000830': []})
            with self.assertRaisesRegex(ValueError, 'identity changed'):
                a.verify_execution(self.c)

    def test_release_requires_pilot_dependencies_and_catches_edit(self):
        code = self.campaign / 'code'
        files = {'batch400/adapter.py': 'adapter', 'wisp_eos_fire_episodes/runtime.py': 'runtime',
                 'wisp_eos_fire_episodes/products.py': 'products'}
        for name, text in files.items():
            path = code / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)
        a.atomic_json(code / 'release.json', {'files': {k: a.file_hash(code / k) for k in files}})
        with mock.patch.object(a, 'HERE', code / 'batch400'):
            a.verify_release(self.campaign)
            (code / 'wisp_eos_fire_episodes/runtime.py').write_text('modified')
            with self.assertRaisesRegex(ValueError, 'release changed'):
                a.verify_release(self.campaign)

    def test_heartbeat_only_reassembles_planned_dates(self):
        self.put('manifests/parity_dates.json', {'T000830': ['2016-01-30T00:00:00']})
        with mock.patch.object(a.rt, 'verify_pilot_day') as verify:
            a.heartbeat(self.c, 'cpu', task=self.keys[0], date='2016-01-30T00:00:00')
            self.assertEqual(verify.call_count, 1)
            a.heartbeat(self.c, 'cpu', task=self.keys[0], date='2016-01-31T00:00:00')
            self.assertEqual(verify.call_count, 1)
        journals = list((self.root / 'logs/worker_events').glob('*.jsonl'))
        self.assertEqual(len(journals), 1)
        self.assertEqual(len(journals[0].read_text().splitlines()), 2)

    def fake_store_setup(self):
        import numpy as np
        small_shapes = {name: (1,) for name in a.SHAPES}
        key = self.keys[0]
        path, meta = a.common.task_paths(self.c, key)
        path.mkdir(parents=True)
        (path / '_SUCCESS.json').write_text('{}')
        day = self.tasks[key]['dates'][0]
        self.put('manifests/tasks/' + key + '/input_days.json', {day: {'metadata': {
            'frozen_checks': {name: {k: True for k in small_shapes}
                              for name in ('future_input_isolation', 'frozen_reference_parity')}}}})
        self.put('manifests/parity_dates.json', {'T000830': [day]})
        self.put('reports/input_parity/' + key + '_' + day[:10] + '.json', {
            'passed': True, 'date': day, 'key': key, 'checks': {k: True for k in small_shapes}})
        with self.q.edit() as state:
            state['phase'] = 'INPUT_VERIFY'
            state['tasks'][key].update(input_status='ready', input_fingerprint='fake')
        a.atomic_json(self.campaign / 'code/release.json', {})
        store = types.SimpleNamespace(path=path, dates=[day], fingerprint='fake',
                    read=lambda i: {name: np.ones((1,), dtype='float32') for name in small_shapes})
        module = types.ModuleType('wisp_batch.store')
        module.InputStore = lambda p: store
        return small_shapes, module, store

    def test_verify_wave_reads_seven_arrays_without_future_wave_or_full_reference(self):
        shapes, module, store = self.fake_store_setup()
        with mock.patch.object(a, 'verify_execution', return_value={}), \
             mock.patch.object(a, 'SHAPES', shapes), \
             mock.patch.object(a, '_CAMPAIGN', self.campaign), \
             mock.patch.dict(sys.modules, {'wisp_batch.store': module}):
            report = a.verify_wave(self.c)
        self.assertTrue(report['passed'])
        state = self.q.snapshot()
        self.assertEqual(state['phase'], 'PREDICT')
        self.assertEqual(state['tasks'][self.keys[1]]['input_status'], 'queued')
        self.assertFalse((self.root / 'manifests/full_roots.json').exists())

    def test_verify_wave_rejects_missing_input_and_does_not_advance(self):
        shapes, module, store = self.fake_store_setup()
        original_read = store.read
        store.read = lambda i: {k: v for k, v in original_read(i).items() if k != 'future_weather'}
        with mock.patch.object(a, 'verify_execution', return_value={}), \
             mock.patch.object(a, 'SHAPES', shapes), \
             mock.patch.dict(sys.modules, {'wisp_batch.store': module}):
            with self.assertRaisesRegex(ValueError, 'Missing or extra'):
                a.verify_wave(self.c)
        self.assertEqual(self.q.snapshot()['phase'], 'INPUT_VERIFY')

    def test_completed_checkpoint_repairs_only_missing_parity(self):
        shapes, module, store = self.fake_store_setup()
        path = next((self.root / 'reports/input_parity').glob('*.json'))
        record = a.read_json(path); path.unlink()
        def repair(c, key, day, dest):
            a.atomic_json(dest, record)
        with mock.patch.object(a, 'verify_execution', return_value={}), \
             mock.patch.object(a, 'SHAPES', shapes), \
             mock.patch.object(a, '_CAMPAIGN', self.campaign), \
             mock.patch.object(a.rt, 'verify_pilot_day', side_effect=repair) as verify, \
             mock.patch.dict(sys.modules, {'wisp_batch.store': module}):
            report = a.verify_wave(self.c)
        self.assertTrue(report['passed'])
        self.assertEqual(verify.call_count, 1)


if __name__ == '__main__':
    unittest.main()
