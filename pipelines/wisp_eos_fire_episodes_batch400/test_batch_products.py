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
        self.adapter = types.SimpleNamespace(Queue=Queue, locked=locked)
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


if __name__ == '__main__':
    unittest.main()
