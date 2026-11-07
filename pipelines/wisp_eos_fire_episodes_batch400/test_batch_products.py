"""Synthetic file-backed tests for bounded-wave publication and cleanup."""
import contextlib
import copy
import fcntl
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import netCDF4 as nc
import numpy as np

import batch_products as b

sys.path.insert(0, str(b.PILOT))
spec = importlib.util.spec_from_file_location('_batch400_pilot_test_products', b.PILOT / 'test_products.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


@contextlib.contextmanager
def locked(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as stream:
        fcntl.lockf(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.lockf(stream, fcntl.LOCK_UN)


class Queue:
    def __init__(self, c):
        self.path = Path(c['root']) / 'state/queue.json'

    def snapshot(self):
        return b.p._read(self.path)

    @contextlib.contextmanager
    def edit(self):
        value = self.snapshot()
        yield value
        b.p._atomic(self.path, value)


class BatchProductsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProductsTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c, self.root, self.region = self.fixture.c, self.fixture.root, self.fixture.region
        self.region['cube_sha256'] = 'accepted-source-sha'
        b.p._atomic(self.root / 'manifests/regions.json', {'token': self.region})
        dates = [str(d) for d in np.arange(np.datetime64('2014-01-12'), np.datetime64('2014-01-20'))]
        self.manifest = self.selection(dates + ['2014-02-01'])
        self.keys = list(self.manifest['tasks'])
        self.tasks = copy.deepcopy(self.manifest['tasks'])
        for key, task in self.tasks.items():
            self.fixture.make_month(key, task['dates'])
            task.update(input_status='ready', prediction_status='done', lease=None,
                        job=None, pid=None, reserved=1000)
            path = Path(self.c['scratch']) / 'inputs' / key
            path.mkdir(parents=True)
            (path / 'tensor.bin').write_bytes(b'input')
            journal = {date: {'arrays': {name: {'sha256': 'hash-' + name}
                                         for name in b.p.INPUT_ARRAYS}} for date in task['dates']}
            b.p._atomic(self.root / 'manifests/tasks' / key / 'input_days.json', journal)
        self.state = dict(tasks=self.tasks, active_wave=0, phase='WAVE_VERIFY',
                          waves=[dict(wave_id=0, keys=self.keys[:2], days=8),
                                 dict(wave_id=1, keys=self.keys[2:], days=1)])
        b.p._atomic(self.root / 'state/queue.json', self.state)
        self.adapter = types.SimpleNamespace(Queue=Queue, locked=locked, actual_budget=lambda c: None)
        self.patcher = patch.dict(sys.modules, adapter=self.adapter, runtime=self.adapter)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def selection(self, fires):
        audit = dict(all_centre_labels_finite_binary=True, date_count=3641,
                     cube_sha256=self.region['cube_sha256'])
        region = b.selection_tools.build_region_selection('token', self.region, fires,
                                                          margin_days=0, audit=audit)
        manifest = b.selection_tools.make_manifest([region], margin_days=0)
        b.p._atomic(self.root / 'manifests/selection.json', manifest)
        return manifest

    def authorize(self):
        result = b.verify_wave_outputs(self.c)
        with Queue(self.c).edit() as state:
            state.update(phase='CLEANUP_WAIT', cleanup_authorized=True,
                         cleanup_not_before_epoch=result['completed_epoch'] + 600)
        return result['completed_epoch'] + 601

    def clean_all_inputs(self):
        for wave_id in (0, 1):
            with Queue(self.c).edit() as state:
                state.update(active_wave=wave_id, phase='WAVE_VERIFY', cleanup_authorized=False)
            later = self.authorize()
            with patch.object(b.time, 'time', return_value=later):
                b.cleanup_wave(self.c)
        with Queue(self.c).edit() as state:
            state['phase'] = 'MERGE'

    def test_wave_validation_is_idempotent_and_does_not_reset_retention(self):
        first = b.verify_wave_outputs(self.c)
        with patch.object(b.time, 'time', return_value=first['completed_epoch'] + 999):
            second = b.verify_wave_outputs(self.c)
        self.assertEqual(first, second)
        self.assertEqual(first['days'], 8)

    def test_cleanup_requires_delay_authorization_and_no_lease(self):
        receipt = b.verify_wave_outputs(self.c)
        self.assertTrue(b.cleanup_wave(self.c)['pending'])
        later = self.authorize()
        with patch.object(b.time, 'time', return_value=receipt['completed_epoch'] + 599):
            self.assertTrue(b.cleanup_wave(self.c)['pending'])
        with Queue(self.c).edit() as state:
            state['tasks'][self.keys[0]]['lease'] = 'active-reader'
        with patch.object(b.time, 'time', return_value=later):
            result = b.cleanup_wave(self.c)
        self.assertIn('lease', result['reason'])
        self.assertTrue((Path(self.c['scratch']) / 'inputs' / self.keys[0]).exists())

    def test_cleanup_rejects_short_retention_even_after_clock_advanced(self):
        later = self.authorize()
        with Queue(self.c).edit() as state:
            state['cleanup_not_before_epoch'] -= 1
        with patch.object(b.time, 'time', return_value=later), self.assertRaisesRegex(ValueError, '600-second'):
            b.cleanup_wave(self.c)

    def test_cleanup_keeps_other_wave_and_all_durable_prediction_and_hash_files(self):
        later = self.authorize()
        with patch.object(b.time, 'time', return_value=later):
            self.assertTrue(b.cleanup_wave(self.c)['cleaned'])
            self.assertTrue(b.cleanup_wave(self.c)['cleaned'])
        state = Queue(self.c).snapshot()
        for key in self.keys[:2]:
            self.assertFalse((Path(self.c['scratch']) / 'inputs' / key).exists())
            self.assertEqual(state['tasks'][key]['input_status'], 'cleaned')
            self.assertEqual(state['tasks'][key]['reserved'], 0)
            self.assertTrue((self.root / 'manifests/tasks' / key / 'input_days.json').exists())
            self.assertTrue((self.root / 'monthly/REG' / (key + '.nc')).exists())
        self.assertTrue((Path(self.c['scratch']) / 'inputs' / self.keys[-1]).exists())
        self.assertEqual(state['tasks'][self.keys[-1]]['reserved'], 1000)

    def test_corrupt_prediction_prevents_any_cleanup(self):
        later = self.authorize()
        receipt = b.p._read(self.root / 'manifests/tasks' / self.keys[0] / 'output.json')
        with nc.Dataset(receipt['path'], 'a') as ds:
            ds['query_logits'][0, 0, 0] = 123
        with patch.object(b.time, 'time', return_value=later), self.assertRaisesRegex(ValueError, 'checksum'):
            b.cleanup_wave(self.c)
        self.assertTrue((Path(self.c['scratch']) / 'inputs' / self.keys[0]).exists())

    def test_cleanup_refreshes_actual_disk_budget_only_after_deletion(self):
        later = self.authorize()
        refreshed = []
        def refresh(path):
            self.assertFalse(Path(path).exists())
            refreshed.append(Path(path).name)
            return {'actual_input_bytes': 0}
        self.adapter.actual_budget = lambda c: types.SimpleNamespace(refresh_path=refresh)
        with patch.object(b.time, 'time', return_value=later):
            self.assertTrue(b.cleanup_wave(self.c)['cleaned'])
            self.assertEqual(refreshed, self.keys[:2])
            # Durable cleanup receipts must not skip a retry's disk-ledger refresh.
            self.assertTrue(b.cleanup_wave(self.c)['cleaned'])
            self.assertEqual(refreshed, self.keys[:2] * 2)

    def test_budget_refresh_failure_after_deletion_recovers_without_false_completion(self):
        later = self.authorize()
        attempts = []
        def refresh(path):
            self.assertFalse(Path(path).exists())
            attempts.append(Path(path).name)
            if len(attempts) == 1:
                raise OSError('temporary budget ledger unavailable')
        self.adapter.actual_budget = lambda c: types.SimpleNamespace(refresh_path=refresh)
        with patch.object(b.time, 'time', return_value=later):
            with self.assertRaisesRegex(OSError, 'ledger unavailable'):
                b.cleanup_wave(self.c)
            key = self.keys[0]
            self.assertEqual(Queue(self.c).snapshot()['tasks'][key]['input_status'], 'ready')
            self.assertTrue((self.root / 'manifests/tasks' / key / 'wave_cleanup_intent.json').exists())
            self.assertFalse((self.root / 'manifests/tasks' / key / 'wave_cleanup.json').exists())
            self.assertTrue(b.cleanup_wave(self.c)['cleaned'])
        self.assertEqual(attempts, [self.keys[0], *self.keys[:2]])

    def test_partial_rmtree_recovers_with_intent_and_durable_hashes(self):
        later = self.authorize()
        original = b.shutil.rmtree
        calls = []
        def interrupted(path):
            original(path)
            calls.append(str(path))
            if len(calls) == 1:
                raise OSError('simulated interruption after deletion')
        with patch.object(b.time, 'time', return_value=later):
            with patch.object(b.shutil, 'rmtree', side_effect=interrupted), self.assertRaises(OSError):
                b.cleanup_wave(self.c)
            self.assertTrue(b.cleanup_wave(self.c)['cleaned'])
        self.assertEqual(Queue(self.c).snapshot()['tasks'][self.keys[0]]['input_status'], 'cleaned')

    def test_missing_hashes_or_symlink_prevents_cleanup(self):
        later = self.authorize()
        input_path = Path(self.c['scratch']) / 'inputs' / self.keys[0]
        (input_path / 'forbidden-link').symlink_to(self.root / 'manifests/selection.json')
        with patch.object(b.time, 'time', return_value=later), self.assertRaisesRegex(ValueError, 'Symlink'):
            b.cleanup_wave(self.c)
        (input_path / 'forbidden-link').unlink()
        b.p._atomic(self.root / 'manifests/tasks' / self.keys[0] / 'input_days.json', {})
        with patch.object(b.time, 'time', return_value=later), self.assertRaisesRegex(ValueError, 'journal'):
            b.cleanup_wave(self.c)

    def test_active_task_lock_keeps_inputs(self):
        later = self.authorize()
        @contextlib.contextmanager
        def busy(path, blocking=True):
            if '/tasks/' in str(path):
                raise BlockingIOError('reader')
            yield
        with patch.object(self.adapter, 'locked', busy), patch.object(b.time, 'time', return_value=later):
            self.assertEqual(b.cleanup_wave(self.c)['reason'], 'active task lock')

    def test_merge_all_waves_after_input_cleanup(self):
        self.clean_all_inputs()
        result = b.finalize_region(self.c)
        self.assertEqual(result['days'], 9)
        with nc.Dataset(result['path']) as ds:
            self.assertEqual(len(ds.dimensions['time']), 9)
            self.assertEqual(list(ds['episode_id'][:]), [1] * 8 + [2])
        self.assertTrue((self.root / 'reports/final.json').exists())
        self.assertTrue((self.root / 'reports/final_chunk_cleanup.json').exists())
        for key in self.keys:
            self.assertFalse((self.root / 'monthly/REG' / (key + '.nc')).exists())
            self.assertTrue((self.root / 'manifests/tasks' / key / 'output.json').exists())
            self.assertTrue((self.root / 'manifests/tasks' / key / 'input_days.json').exists())
        self.assertEqual(b.finalize_region(self.c), result)

    def test_empty_product_preserves_schema_and_has_no_fake_dates(self):
        self.selection([])
        result = b.empty_result(self.c)
        self.assertEqual(result['status'], 'no_observed_fire')
        self.assertEqual(result['days'], 0)
        with nc.Dataset(result['path']) as ds:
            self.assertTrue(ds.dimensions['time'].isunlimited())
            self.assertEqual(ds['wisp_eos_fire_score'].shape, (0, 96, 96))
            self.assertEqual(ds['query_logits'].shape, (0, 10, 2))
        self.assertEqual(result, b.empty_result(self.c))

    def test_empty_requires_complete_known_source_and_empty_selection(self):
        with self.assertRaisesRegex(ValueError, 'Nonempty'):
            b.empty_result(self.c)
        manifest = self.selection([])
        region = manifest['regions']['token']
        region['source_audit']['all_centre_labels_finite_binary'] = False
        manifest = b.selection_tools.make_manifest([region], margin_days=0)
        b.p._atomic(self.root / 'manifests/selection.json', manifest)
        with self.assertRaisesRegex(ValueError, 'audited'):
            b.empty_result(self.c)

    def test_reuse_requires_source_model_grid_and_selection_agreement(self):
        old_final = b.p.consolidate(self.c, self.region, self.tasks)
        b.p._atomic(self.root / 'config/deployment.json', self.c)
        new_root = self.fixture.base / 'new'
        new_c = dict(self.c, root=str(new_root), scratch=str(self.fixture.base / 'new-scratch'))
        for relative in ('manifests/regions.json', 'manifests/deployment.json', 'manifests/selection.json'):
            b.p._atomic(new_root / relative, b.p._read(self.root / relative))
        result = b.verify_reuse(self.root, new_c)
        self.assertEqual(result['path'], old_final['path'])
        self.assertTrue(result['reused'])
        self.assertFalse((new_root / 'outputs').exists())
        with self.assertRaisesRegex(ValueError, 'model'):
            b.verify_reuse(self.root, dict(new_c, code_commit='changed'))
        with nc.Dataset(old_final['path'], 'a') as ds:
            ds['query_logits'][0, 0, 0] = 123
        with self.assertRaisesRegex(ValueError, 'checksum'):
            b.verify_reuse(self.root, new_c)

    def test_final_chunk_cleanup_rejects_live_inputs_and_active_owners(self):
        with self.assertRaisesRegex(ValueError, 'input cleanup'):
            b.finalize_region(self.c)
        self.clean_all_inputs()
        tasks = Queue(self.c).snapshot()['tasks']
        final = b.p.consolidate(self.c, self.region, tasks)
        with Queue(self.c).edit() as state:
            state['tasks'][self.keys[0]]['lease'] = 'active-reader'
        with self.assertRaisesRegex(ValueError, 'no task owners'):
            b.cleanup_final_chunks(self.c, self.region, tasks, final)
        self.assertTrue((self.root / 'monthly/REG' / (self.keys[0] + '.nc')).exists())

    def test_final_chunk_cleanup_fails_closed_for_corrupt_prediction(self):
        self.clean_all_inputs()
        tasks = Queue(self.c).snapshot()['tasks']
        final = b.p.consolidate(self.c, self.region, tasks)
        path = self.root / 'monthly/REG' / (self.keys[1] + '.nc')
        with nc.Dataset(path, 'a') as ds:
            ds['query_logits'][0, 0, 0] = 123
        with self.assertRaisesRegex(ValueError, 'checksum'):
            b.cleanup_final_chunks(self.c, self.region, tasks, final)
        self.assertTrue((self.root / 'monthly/REG' / (self.keys[0] + '.nc')).exists())
        self.assertTrue(Path(final['path']).exists())

    def test_final_chunk_cleanup_refuses_symlinks_and_preserves_external_files(self):
        self.clean_all_inputs()
        tasks = Queue(self.c).snapshot()['tasks']
        final = b.p.consolidate(self.c, self.region, tasks)
        path = self.root / 'monthly/REG' / (self.keys[0] + '.nc')
        protected = self.fixture.base / 'protected.nc'
        path.rename(protected)
        path.symlink_to(protected)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            b.cleanup_final_chunks(self.c, self.region, tasks, final)
        self.assertTrue(protected.exists())
        self.assertTrue(Path(final['path']).exists())

    def test_final_chunk_cleanup_recovers_after_unlink_before_completion_journal(self):
        self.clean_all_inputs()
        original = Path.unlink
        calls = []
        def interrupted(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if '/monthly/' in str(path):
                calls.append(str(path))
                if len(calls) == 1:
                    raise OSError('simulated interruption after chunk unlink')
            return value
        with patch.object(Path, 'unlink', interrupted), self.assertRaises(OSError):
            b.finalize_region(self.c)
        self.assertFalse((self.root / 'reports/final.json').exists())
        result = b.finalize_region(self.c)
        self.assertEqual(result['days'], 9)
        self.assertTrue(Path(result['path']).exists())
        self.assertTrue(all(not (self.root / 'monthly/REG' / (key + '.nc')).exists() for key in self.keys))
        self.assertEqual(b.finalize_region(self.c)['sha256'], result['sha256'])

    def test_reuse_accepts_changed_global_digest_and_ignores_operational_source_paths(self):
        # Add a second pilot region. Its presence changes all pilot task keys,
        # although this region's dates, inputs and model identity are unchanged.
        selected = self.manifest['regions']['token']
        other_record = dict(self.region, region_id='OTHER')
        other = b.selection_tools.build_region_selection('other', other_record, [], margin_days=0,
                                                        audit=selected['source_audit'])
        pilot_manifest = b.selection_tools.make_manifest([selected, other], margin_days=0)
        pilot_tasks = copy.deepcopy(pilot_manifest['tasks'])
        for key, task in pilot_tasks.items():
            self.fixture.make_month(key, task['dates'])
        b.p._atomic(self.root / 'manifests/regions.json', {'token': self.region, 'other': other_record})
        b.p._atomic(self.root / 'manifests/selection.json', pilot_manifest)
        old_final = b.p.consolidate(self.c, self.region, pilot_tasks)
        b.p._atomic(self.root / 'config/deployment.json', self.c)
        new_root = self.fixture.base / 'one-region'
        new_c = dict(self.c, root=str(new_root), scratch=str(self.fixture.base / 'one-region-scratch'))
        new_record = dict(self.region, coverage_qa='/new/private/qa/path')
        new_selected = copy.deepcopy(selected)
        new_selected['cube_record'] = new_record
        new_manifest = b.selection_tools.make_manifest([new_selected], margin_days=0)
        b.p._atomic(new_root / 'manifests/regions.json', {'token': new_record})
        b.p._atomic(new_root / 'manifests/selection.json', new_manifest)
        b.p._atomic(new_root / 'manifests/deployment.json', {'fingerprint': 'frozen-original'})
        self.assertNotEqual(pilot_manifest['selection_sha256'], new_manifest['selection_sha256'])
        result = b.verify_reuse(self.root, new_c)
        self.assertEqual(result['path'], old_final['path'])


    def make_cpu_chunk(self, key):
        path = self.root / 'manifests/tasks' / key / 'output.json'
        receipt = b.p._read(path)
        metadata = dict(inference_backend='cpu', precision='float32', batch_size=1,
                        hardware='Intel Xeon test', provenance_source='explicit')
        with nc.Dataset(receipt['path'], 'a') as ds:
            ds.setncatts(metadata)
        receipt.update(metadata, sha256=b.p._sha(receipt['path']))
        b.p._atomic(path, receipt)
        journal_path = self.root / 'manifests/tasks' / key / 'prediction_days.json'
        journal = b.p._read(journal_path)
        for day in journal.values():
            day['timing'] = metadata
        b.p._atomic(journal_path, journal)
        return receipt

    def test_cpu_chunk_requires_explicit_matching_backend_and_daily_journal(self):
        key = self.keys[0]
        receipt = self.make_cpu_chunk(key)
        self.assertTrue(b.validate_chunk_provenance(self.c, key, receipt, self.tasks[key])['passed'])
        bad = dict(receipt, precision='bfloat16')
        with self.assertRaisesRegex(ValueError, 'precision'):
            b.validate_chunk_provenance(self.c, key, bad, self.tasks[key])
        journal_path = self.root / 'manifests/tasks' / key / 'prediction_days.json'
        journal = b.p._read(journal_path)
        journal['0']['timing']['inference_backend'] = 'cuda'
        b.p._atomic(journal_path, journal)
        with self.assertRaisesRegex(ValueError, 'daily prediction journal'):
            b.validate_chunk_provenance(self.c, key, receipt, self.tasks[key])

    def test_mixed_final_retains_daily_backend_without_changing_scientific_values(self):
        cpu_key = self.keys[1]
        self.make_cpu_chunk(cpu_key)
        original = []
        for key in self.keys:
            with nc.Dataset(self.root / 'monthly/REG' / (key + '.nc')) as ds:
                original.append(np.asarray(ds['wisp_eos_fire_score'][:]))
        self.clean_all_inputs()
        final = b.finalize_region(self.c)
        with nc.Dataset(final['path']) as ds:
            np.testing.assert_array_equal(ds['wisp_eos_fire_score'][:], np.concatenate(original))
            expected_device = np.concatenate([np.full(len(self.tasks[k]['dates']), 2 if k == cpu_key else 1)
                                              for k in self.keys])
            np.testing.assert_array_equal(ds['inference_device_code'][:], expected_device)
            np.testing.assert_array_equal(ds['inference_precision_code'][:], expected_device)
            np.testing.assert_array_equal(ds['inference_batch_size'][:], np.where(expected_device == 2, 1, 8))
            np.testing.assert_array_equal(ds['inference_provenance_source_code'][:], np.where(expected_device == 2, 1, 2))
            self.assertEqual(ds.precision, 'mixed')
            self.assertEqual(ds.inference_backend, 'mixed')
            self.assertEqual(ds.batch_size, 0)
            catalog = json.loads(ds.inference_backend_catalog_json)
            self.assertEqual({r['hardware'] for r in catalog}, {'A100', 'Intel Xeon test'})
        self.assertEqual(b.finalize_region(self.c), final)

    def test_cpu_resume_processor_change_remains_traceable_per_date(self):
        key = self.keys[0]
        receipt = self.make_cpu_chunk(key)
        hardware = ['Intel model A'] * receipt['days']
        hardware[-1] = 'Intel model B'
        with nc.Dataset(receipt['path'], 'a') as ds:
            ds.hardware = 'CPU'
        receipt.update(hardware='CPU', inference_hardware_by_day=hardware,
                       cpu_processor_models=sorted(set(hardware)), sha256=b.p._sha(receipt['path']))
        b.p._atomic(self.root / 'manifests/tasks' / key / 'output.json', receipt)
        journal_path = self.root / 'manifests/tasks' / key / 'prediction_days.json'
        journal = b.p._read(journal_path)
        for index, model in enumerate(hardware):
            journal[str(index)]['timing']['hardware'] = model
        b.p._atomic(journal_path, journal)
        self.assertTrue(b.validate_chunk_provenance(self.c, key, receipt, self.tasks[key])['passed'])
        self.clean_all_inputs()
        final = b.finalize_region(self.c)
        with nc.Dataset(final['path']) as ds:
            catalog = json.loads(ds.inference_backend_catalog_json)
            ids = ds['inference_backend_id'][:len(hardware)]
            self.assertEqual([catalog[i]['hardware'] for i in ids], hardware)

    def test_cpu_first_chunk_merges_with_legacy_gpu(self):
        self.make_cpu_chunk(self.keys[0])
        self.clean_all_inputs()
        final = b.finalize_region(self.c)
        with nc.Dataset(final['path']) as ds:
            self.assertEqual(ds['inference_device_code'][0], 2)
            self.assertEqual(ds['inference_device_code'][-1], 1)

    def test_tampered_daily_backend_rejected_even_with_refreshed_file_checksum(self):
        self.make_cpu_chunk(self.keys[1])
        self.clean_all_inputs()
        final = b.finalize_region(self.c)
        with nc.Dataset(final['path'], 'a') as ds:
            ds['inference_device_code'][0] = 2
        receipt_path = b.p._final_paths(self.c, self.region)[1]
        receipt = b.p._read(receipt_path)
        receipt['sha256'] = b.p._sha(final['path'])
        b.p._atomic(receipt_path, receipt)
        with self.assertRaisesRegex(ValueError, 'Daily inference provenance'):
            b.p.validate_final(self.c, self.region, self.tasks)

    def test_unidentified_legacy_device_cannot_be_silently_labeled_gpu(self):
        with self.assertRaisesRegex(ValueError, 'Unidentified legacy'):
            b._backend_record(dict(hardware='Intel CPU', precision='bfloat16', batch_size=8))

    def test_prediction_reader_shared_lock_prevents_cleanup(self):
        later = self.authorize()
        path = self.root / 'locks/prediction_readers.lock'
        with path.open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            with patch.object(b.time, 'time', return_value=later):
                result = b.cleanup_wave(self.c)
            self.assertTrue(result['pending'])
            self.assertIn('regional prediction reader', result['reason'])
            self.assertTrue((Path(self.c['scratch']) / 'inputs' / self.keys[0]).exists())
            fcntl.flock(stream, fcntl.LOCK_UN)
        with patch.object(b.time, 'time', return_value=later):
            self.assertTrue(b.cleanup_wave(self.c)['cleaned'])

    def test_activation_is_idempotent(self):
        first = b.p.validate_chunk
        self.assertIs(b.activate_products(), b.p)
        self.assertIs(first, b.p.validate_chunk)


if __name__ == '__main__':
    unittest.main()
