"""Small synthetic protocol tests; no cluster jobs or production writes."""
import contextlib
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import netCDF4 as nc
import numpy as np

import products as p


class ProductsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.c = {'root': str(self.base / 'run'), 'scratch': str(self.base / 'scratch'),
                  'start': '2014-01-12T00:00:00', 'end_exclusive': '2024-01-01T00:00:00',
                  'checkpoint_sha256': 'checkpoint', 'code_commit': 'model'}
        self.root = Path(self.c['root'])
        self.region = {'region_id': 'REG', 'lat': np.linspace(-70, 70, 128).tolist(),
                       'lon': np.linspace(-170, 170, 128).tolist()}
        p._atomic(self.root / 'manifests/deployment.json', {'fingerprint': 'frozen-original'})
        p._atomic(self.root / 'manifests/regions.json', {'token': self.region})
        self.key = 'token_evt_selected_B001'
        self.dates = ['2014-01-12T00:00:00', '2014-01-13T00:00:00']
        self.receipt = self.make_month(self.key, self.dates)
        self.tasks = {self.key: {'region': 'token', 'kind': 'episode', 'dates': self.dates}}
        self.selection = {'selected_dates': self.dates}
        p._atomic(self.root / 'manifests/selection.json', {'regions': {'token': self.selection}})

    def make_month(self, key, dates):
        path = self.root / 'monthly/REG' / (key + '.nc')
        path.parent.mkdir(parents=True, exist_ok=True)
        identity = dict(self.region, region='REG', deployment='frozen-original', dates=dates)
        p._atomic(self.root / 'manifests/tasks' / key / 'registration.json',
                  {'key': key, 'scratch_path': str(Path(self.c['scratch']) / 'inputs' / key),
                   'identity': identity})
        ticks = np.asarray(dates, dtype='datetime64[s]').astype('int64')
        with nc.Dataset(path, 'w') as ds:
            for name, size in {'time': len(dates), 'lat': 96, 'lon': 96, 'query': 10,
                               'axis': 2, 'class': 2, 'bounds': 2}.items():
                ds.createDimension(name, size)
            ds.setncatts({'deployment_fingerprint': 'frozen-original', 'checkpoint_sha256': 'checkpoint',
                          'model_commit': 'model', 'region_id': 'REG', 'batch_size': 8,
                          'precision': 'bfloat16', 'history_mode': 'observed_viirs',
                          'input_protocol': 'history-only-vegetation/v1', 'fingerprint': 'prediction',
                          'input_fingerprint': 'input', 'hardware': 'A100'})
            for name, dims in p.DIMS.items():
                dtype = ('i8' if name in {'time', 'time_bounds'} else 'i2' if name in {'query', 'axis', 'class'}
                         else 'u1' if name in {'wisp_eos_valid_mask', 'noaa20_in_history'}
                         else 'f8' if name in {'lat', 'lon', 'query_latitude', 'query_longitude'} else 'f4')
                ds.createVariable(name, dtype, dims)
            ds['time'][:] = ticks
            ds['time'].units = 'seconds since 1970-01-01 00:00:00'
            ds['time'].calendar = 'proleptic_gregorian'
            ds['time_bounds'][:] = np.stack([ticks, ticks + p.DAY], axis=1)
            for axis in ('lat', 'lon'):
                ds[axis][:] = self.region[axis][16:112]
            for axis in ('query', 'axis', 'class'):
                ds[axis][:] = np.arange(len(ds.dimensions[axis]))
            for name in ('wisp_eos_fire_score', 'query_points_yx', 'query_confidence', 'query_logits'):
                ds[name][:] = 0.5
            ds['wisp_eos_valid_mask'][:] = 1
            ds['noaa20_in_history'][:] = 0
            for axis, name in enumerate(('latitude', 'longitude')):
                points = ds['query_points_yx'][:]
                grid = np.asarray(self.region['lat' if axis == 0 else 'lon'])[16:112]
                ds['query_' + name][:] = np.interp(points[..., axis] * 95, np.arange(96), grid)
            # An unlisted variable tests that the merger checks every variable.
            extra = ds.createVariable('extra_audit', 'i4', ('time', 'query'))
            extra[:] = np.arange(len(dates) * 10).reshape(len(dates), 10)
        receipt = {'validated': True, 'key': key, 'path': str(path), 'sha256': p._sha(path),
                   'fingerprint': 'prediction', 'input_fingerprint': 'input', 'hardware': 'A100',
                   'days': len(dates), 'date_start': dates[0], 'date_end': dates[-1]}
        with nc.Dataset(path) as ds:
            ds.set_auto_maskandscale(False)
            journal = {str(i): {'date': date, 'fingerprint': 'prediction',
                                'arrays': {name: p._array_sha(ds[name][i]) for name in p.JOURNAL_ARRAYS}}
                       for i, date in enumerate(dates)}
        p._atomic(self.root / 'manifests/tasks' / key / 'prediction_days.json', journal)
        p._atomic(self.root / 'manifests/tasks' / key / 'output.json', receipt)
        return receipt

    def modify(self, name, value, index=0):
        with nc.Dataset(self.receipt['path'], 'a') as ds:
            ds[name][index] = value
        self.receipt['sha256'] = p._sha(self.receipt['path'])

    def runtime(self, state=None, locked=None):
        state = state if state is not None else {}
        class Queue:
            def __init__(self, c):
                pass
            def snapshot(self):
                return copy.deepcopy(state)
            @contextlib.contextmanager
            def edit(self):
                candidate = copy.deepcopy(state)
                yield candidate
                state.clear()
                state.update(candidate)
        @contextlib.contextmanager
        def default_lock(path, blocking=True):
            yield
        return patch.dict(sys.modules, runtime=types.SimpleNamespace(Queue=Queue, locked=locked or default_lock))

    def test_valid_chunk_and_cross_month_allowed(self):
        self.assertTrue(p.validate_chunk(self.c, self.key, self.receipt)['passed'])
        dates = ['2016-12-31T00:00:00', '2017-01-01T00:00:00']
        receipt = self.make_month(self.key, dates)
        self.assertTrue(p.validate_chunk(self.c, self.key, receipt)['passed'])

    def test_chunk_journal_detects_rehashed_changed_values(self):
        self.modify('query_confidence', .9)
        with self.assertRaisesRegex(ValueError, 'journal checksum'):
            p.validate_chunk(self.c, self.key, self.receipt)

    def test_chunk_gaps_and_more_than_seven_days_rejected(self):
        for dates in [['2014-01-12T00:00:00', '2014-01-14T00:00:00'],
                      [str(x.astype('datetime64[s]')) for x in np.arange(
                          np.datetime64('2014-01-12'), np.datetime64('2014-01-20'))]]:
            receipt = self.make_month(self.key, dates)
            with self.assertRaises(ValueError):
                p.validate_chunk(self.c, self.key, receipt)

    def add_sparse_chunk(self):
        key = 'token_evt_selected_B002'
        dates = ['2014-02-01T00:00:00']
        receipt = self.make_month(key, dates)
        self.tasks[key] = {'region': 'token', 'kind': 'episode', 'dates': dates}
        self.selection = {'selected_dates': self.dates + dates,
                          'episodes': [{'start': '2014-01-12', 'end': '2014-01-13'},
                                       {'start': '2014-02-01', 'end': '2014-02-01'}]}
        p._atomic(self.root / 'manifests/selection.json', {'regions': {'token': self.selection}})
        return key, receipt

    def test_sparse_merge_preserves_values_and_omits_unselected_days(self):
        self.add_sparse_chunk()
        with self.runtime():
            final = p.consolidate(self.c, self.region, self.tasks)
            again = p.consolidate(self.c, self.region, self.tasks)
        self.assertEqual(final['sha256'], again['sha256'])
        self.assertEqual(final['days'], 3)
        self.assertEqual(final['chunks'], 2)
        self.assertTrue(Path(self.receipt['path']).exists())
        with nc.Dataset(final['path']) as ds:
            self.assertTrue(np.array_equal(ds['episode_id'][:], [1, 1, 2]))
            self.assertTrue(np.array_equal(ds['time'][:], p._ticks(self.selection['selected_dates'])))
            self.assertIn('Absent', ds.unselected_dates)
            self.assertEqual(ds['extra_audit'][2, 0], 0)
            self.assertEqual(ds['time_bounds'][2, 1] - ds['time_bounds'][2, 0], p.DAY)

    def test_missing_duplicate_and_empty_selections_rejected(self):
        self.add_sparse_chunk()
        with self.assertRaisesRegex(ValueError, 'time mismatch'):
            p._selection_contract(self.c, self.region, {self.key: self.tasks[self.key]})
        with self.assertRaisesRegex(ValueError, 'No event'):
            p._selection_contract(self.c, self.region, {})
        duplicate = 'token_evt_duplicate'
        self.make_month(duplicate, self.dates)
        self.tasks[duplicate] = dict(self.tasks[self.key])
        with self.assertRaisesRegex(ValueError, 'unique increasing'):
            p._selection_contract(self.c, self.region, self.tasks)

    def test_bad_episode_catalog_and_wrong_registered_grid_rejected(self):
        self.add_sparse_chunk()
        bad = copy.deepcopy(self.selection)
        bad['episodes'][0]['end'] = '2014-01-14'
        with self.assertRaisesRegex(ValueError, 'episode bounds'):
            p._selection_contract(self.c, self.region, self.tasks, bad)
        regpath = self.root / 'manifests/tasks' / self.key / 'registration.json'
        reg = p._read(regpath)
        reg['identity']['lat'][0] += 1
        p._atomic(regpath, reg)
        with self.assertRaisesRegex(ValueError, 'grid'):
            p._selection_contract(self.c, self.region, self.tasks)

    def test_invalid_arrays_and_paths_rejected(self):
        for name, value, message in [('query_logits', np.nan, 'Nonfinite'),
                                     ('wisp_eos_fire_score', 1.2, 'outside'),
                                     ('wisp_eos_valid_mask', 0, 'coverage'),
                                     ('query_latitude', 1234, 'coordinate')]:
            self.receipt = self.make_month(self.key, self.dates)
            self.modify(name, value)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, message):
                p.validate_chunk(self.c, self.key, self.receipt)
        self.receipt = self.make_month(self.key, self.dates)
        bad = dict(self.receipt, path=str(self.base / 'old/result.nc'))
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            p.validate_chunk(self.c, self.key, bad)
        path = Path(self.receipt['path'])
        old = self.base / 'protected.nc'
        path.rename(old)
        path.symlink_to(old)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            p.validate_chunk(self.c, self.key, self.receipt)

    def cleanup_state(self, final):
        state = {'phase': 'CLEANUP_WAIT', 'cleanup_authorized': True,
                 'cleanup_not_before_epoch': final['completed_epoch'] + 600,
                 'tasks': copy.deepcopy(self.tasks)}
        for key, value in state['tasks'].items():
            value.update(input_status='ready', prediction_status='done',
                         lease=None, job=None, pid=None, reserved=123)
            path = Path(self.c['scratch']) / 'inputs' / key
            path.mkdir(parents=True)
            (path / 'input').write_text('registered')
        return state

    def test_cleanup_waits_for_delay_and_lease_then_deletes_only_registered_inputs(self):
        with self.runtime():
            final = p.consolidate(self.c, self.region, self.tasks)
        state = self.cleanup_state(final)
        unrelated = Path(self.c['scratch']) / 'inputs/unrelated'
        unrelated.mkdir()
        (unrelated / 'input').write_text('protected')
        with self.runtime(state):
            pending = p.cleanup_region(self.c, 'token', self.tasks)
            self.assertTrue(pending['pending'])
        with self.runtime(state), patch.object(p.time, 'time', return_value=state['cleanup_not_before_epoch'] + 1):
            state['tasks'][self.key]['lease'] = 'active'
            self.assertTrue(p.cleanup_region(self.c, 'token', self.tasks)['pending'])
            state['tasks'][self.key]['lease'] = None
            cleaned = p.cleanup_region(self.c, 'token', self.tasks)
            self.assertTrue(cleaned['cleaned'])
            self.assertTrue(p.cleanup_region(self.c, 'token', self.tasks)['cleaned'])
        self.assertFalse((Path(self.c['scratch']) / 'inputs' / self.key).exists())
        self.assertTrue((unrelated / 'input').exists())
        self.assertTrue(Path(self.receipt['path']).exists())
        self.assertEqual(state['tasks'][self.key]['reserved'], 0)

    def test_cleanup_rejects_symlink_and_busy_lock(self):
        with self.runtime():
            final = p.consolidate(self.c, self.region, self.tasks)
        state = self.cleanup_state(final)
        @contextlib.contextmanager
        def busy(path, blocking=True):
            if 'tasks' in str(path):
                raise BlockingIOError('busy')
            yield
        with self.runtime(state, busy), patch.object(p.time, 'time', return_value=state['cleanup_not_before_epoch'] + 1):
            self.assertTrue(p.cleanup_region(self.c, 'token', self.tasks)['pending'])
        input_path = Path(self.c['scratch']) / 'inputs' / self.key
        input_path.rename(input_path.with_name('protected'))
        input_path.symlink_to(input_path.with_name('protected'), target_is_directory=True)
        with self.runtime(state), patch.object(p.time, 'time', return_value=state['cleanup_not_before_epoch'] + 1):
            with self.assertRaisesRegex(ValueError, 'Symlink'):
                p.cleanup_region(self.c, 'token', self.tasks)

    def test_full_hash_comparison_pending_and_mismatch(self):
        event = {date: {'arrays': {name: {'sha256': name} for name in p.INPUT_ARRAYS}} for date in self.dates}
        p._atomic(self.root / 'manifests/tasks' / self.key / 'input_days.json', event)
        full = self.base / 'full'
        p._atomic(full / 'manifests/regions.json', {'token': self.region})
        p._atomic(full / 'manifests/deployment.json', {'fingerprint': 'frozen-original'})
        self.assertEqual(p.compare_full_input_hashes(self.c, self.region, self.tasks, [full])['status'], 'pending')
        p._atomic(full / 'manifests/tasks/token_2014-01/input_days.json', event)
        self.assertEqual(p.compare_full_input_hashes(self.c, self.region, self.tasks, [full])['status'], 'passed')
        event[self.dates[0]]['arrays']['hist_weather']['sha256'] = 'changed'
        p._atomic(full / 'manifests/tasks/token_2014-01/input_days.json', event)
        report = p.compare_full_input_hashes(self.c, self.region, self.tasks, [full])
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['comparisons'][0]['mismatched_arrays'], ['hist_weather'])

    def test_full_prediction_comparison_pending_without_reference(self):
        with self.runtime():
            p.consolidate(self.c, self.region, self.tasks)
        report = p.compare_full_predictions(self.c, self.region, self.tasks, [])
        self.assertEqual(report['status'], 'pending')

    def test_full_prediction_comparison_reports_batch_differences(self):
        import shutil
        with self.runtime():
            final = p.consolidate(self.c, self.region, self.tasks)
        full = self.base / 'full'
        p._atomic(full / 'manifests/regions.json', {'token': self.region})
        p._atomic(full / 'manifests/deployment.json', {'fingerprint': 'frozen-original'})
        path = full / 'outputs/REG/reference.nc'
        path.parent.mkdir(parents=True)
        shutil.copyfile(final['path'], path)
        with nc.Dataset(path, 'a') as ds:
            ds['query_confidence'][0, 0] = .49
        p._atomic(full / 'reports/regions/REG.json', dict(final, path=str(path), sha256=p._sha(path)))
        report = p.compare_full_predictions(self.c, self.region, self.tasks, [full])
        self.assertEqual(report['status'], 'compared')
        metric = report['comparisons'][0]['metrics']['query_confidence']
        self.assertAlmostEqual(metric['max_abs'], .01)
        self.assertEqual(metric['threshold_decision_changes_ge']['0.5'], 1)
        self.assertEqual(report['comparisons'][0]['metrics']['query_logits']['rmse'], 0)

    def test_cleanup_recovery_after_delete_before_state_commit(self):
        with self.runtime():
            final = p.consolidate(self.c, self.region, self.tasks)
        state = self.cleanup_state(final)
        original = p.shutil.rmtree
        def delete_then_crash(path):
            original(path)
            raise RuntimeError('simulated interruption')
        with self.runtime(state), patch.object(p.time, 'time', return_value=state['cleanup_not_before_epoch'] + 1):
            with patch.object(p.shutil, 'rmtree', side_effect=delete_then_crash):
                with self.assertRaisesRegex(RuntimeError, 'interruption'):
                    p.cleanup_region(self.c, 'token', self.tasks)
            self.assertEqual(state['tasks'][self.key]['input_status'], 'ready')
            self.assertTrue(p.cleanup_region(self.c, 'token', self.tasks)['cleaned'])
        self.assertEqual(state['tasks'][self.key]['input_status'], 'cleaned')


if __name__ == '__main__':
    unittest.main()
