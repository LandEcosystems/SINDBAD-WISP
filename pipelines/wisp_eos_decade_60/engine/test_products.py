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
        self.key = 'token_2014-01'
        self.dates = ['2014-01-12T00:00:00', '2014-01-13T00:00:00']
        self.receipt = self.make_month(self.key, self.dates)

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

    def test_valid_month_preserves_frozen_identity(self):
        self.assertTrue(p.validate_month(self.c, self.key, self.receipt)['passed'])
        self.assertTrue(Path(self.receipt['path']).exists())

    def test_bad_hash_rejected(self):
        self.receipt['sha256'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'checksum'):
            p.validate_month(self.c, self.key, self.receipt)

    def test_journal_detects_finite_data_changed_with_rehashed_file(self):
        self.modify('query_confidence', 0.75)
        with self.assertRaisesRegex(ValueError, 'journal checksum'):
            p.validate_month(self.c, self.key, self.receipt)

    def test_missing_journal_array_or_day_rejected(self):
        path = self.root / 'manifests/tasks' / self.key / 'prediction_days.json'
        original = p._read(path)
        changed = copy.deepcopy(original)
        changed['0']['arrays'].pop('query_logits')
        p._atomic(path, changed)
        with self.assertRaisesRegex(ValueError, 'array coverage'):
            p.validate_month(self.c, self.key, self.receipt)
        original.pop('1')
        p._atomic(path, original)
        with self.assertRaisesRegex(ValueError, 'journal incomplete'):
            p.validate_month(self.c, self.key, self.receipt)

    def test_old_root_or_symlink_receipt_rejected(self):
        receipt = dict(self.receipt, path=str(self.base / 'old/monthly/REG' / (self.key + '.nc')))
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            p.validate_month(self.c, self.key, receipt)
        target = Path(self.receipt['path'])
        saved = self.base / 'saved.nc'
        target.rename(saved)
        target.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            p.validate_month(self.c, self.key, self.receipt)

    def test_registration_cannot_authorize_different_input(self):
        path = self.root / 'manifests/tasks' / self.key / 'registration.json'
        registration = p._read(path)
        registration['scratch_path'] = str(self.base)
        p._atomic(path, registration)
        with self.assertRaisesRegex(ValueError, 'registration'):
            p.validate_month(self.c, self.key, self.receipt)

    def test_rejects_nan_range_invalid_mask_and_query_coordinates(self):
        changes = [('query_logits', np.nan, 'Nonfinite'), ('wisp_eos_fire_score', 1.2, 'outside'),
                   ('wisp_eos_valid_mask', 0, 'coverage'), ('query_latitude', 1234, 'coordinate'),
                   ('noaa20_in_history', 255, 'flag')]
        for name, value, message in changes:
            with self.subTest(variable=name):
                self.receipt = self.make_month(self.key, self.dates)
                self.modify(name, value)
                with self.assertRaisesRegex(ValueError, message):
                    p.validate_month(self.c, self.key, self.receipt)

    def test_date_axis_uses_real_3641_day_interval(self):
        expected = p.expected_dates(self.c)
        self.assertEqual(len(expected), 3641)
        self.assertEqual(str(np.datetime64(int(expected[0]), 's')), self.c['start'])
        self.assertEqual(str(np.datetime64(int(expected[-1]), 's')), '2023-12-31T00:00:00')
        p.validate_date_axis(expected, expected)
        for bad in (expected[:-1], expected[::-1], np.roll(expected, 1), expected.astype('f8')):
            with self.assertRaises(ValueError):
                p.validate_date_axis(bad, expected)
        duplicate = expected.copy()
        duplicate[20] = duplicate[19]
        with self.assertRaises(ValueError):
            p.validate_date_axis(duplicate, expected)
        with self.assertRaises(ValueError):
            p.expected_dates(dict(self.c, start='2014-01-01'))

    def test_month_wrong_time_or_bounds_rejected_even_with_fresh_hash(self):
        self.modify('time', 0)
        with self.assertRaisesRegex(ValueError, 'time mismatch'):
            p.validate_month(self.c, self.key, self.receipt)
        self.receipt = self.make_month(self.key, self.dates)
        self.modify('time_bounds', [0, 86400])
        with self.assertRaisesRegex(ValueError, 'bounds'):
            p.validate_month(self.c, self.key, self.receipt)

    def test_small_merge_keeps_sources_and_compares_extra_variable(self):
        second = self.make_month('token_2014-02', ['2014-01-14T00:00:00'])
        receipts = [self.receipt, second]
        temporary = self.base / 'merged.nc'
        dates = np.asarray(self.dates + ['2014-01-14T00:00:00'], dtype='datetime64[s]').astype('int64')
        with patch.object(p, 'TOTAL_DAYS', 3), patch.object(p, 'expected_dates', return_value=dates):
            p._stream_merge(self.c, self.region, receipts, temporary)
        self.assertTrue(all(Path(r['path']).exists() for r in receipts))
        with nc.Dataset(temporary, 'a') as ds:
            ds['extra_audit'][2, 0] += 1
        with nc.Dataset(temporary) as out, nc.Dataset(second['path']) as src:
            out.set_auto_maskandscale(False)
            src.set_auto_maskandscale(False)
            with self.assertRaisesRegex(ValueError, 'extra_audit'):
                p._compare_source(out, src, 2)

    def cleanup_state(self):
        tasks = {'m' + str(i): {'region': 'token', 'kind': 'month', 'input_status': 'ready',
                               'prediction_status': 'done', 'lease': None, 'job': None, 'pid': None}
                 for i in range(120)}
        final = {'completed_epoch': 1000, 'monthly_receipts': [{'key': key} for key in tasks]}
        state = {'active_cube': 'token', 'phase': 'CLEANUP_WAIT', 'cleanup_authorized': True,
                 'cleanup_not_before_epoch': 1600, 'tasks': tasks}
        return state, final

    def test_cleanup_gate_requires_delay_permission_all_months_and_no_leases(self):
        state, final = self.cleanup_state()
        self.assertEqual(len(p._cleanup_gate(state, 'token', final, 1600)), 120)
        for field, value in [('cleanup_authorized', False), ('phase', 'MERGE'),
                             ('cleanup_not_before_epoch', 1599), ('cleanup_not_before_epoch', float('nan'))]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                p._cleanup_gate(dict(state, **{field: value}), 'token', final, 2000)
        with self.assertRaises(ValueError):
            p._cleanup_gate(state, 'token', final, 1599)
        for field, value in [('lease', 'worker'), ('job', '123'), ('pid', 22),
                             ('prediction_status', 'predicting'), ('input_status', 'building')]:
            changed = copy.deepcopy(state)
            changed['tasks']['m0'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                p._cleanup_gate(changed, 'token', final, 2000)
        state['tasks'].pop('m0')
        with self.assertRaises(ValueError):
            p._cleanup_gate(state, 'token', final, 2000)

    def test_registered_input_parent_symlink_rejected(self):
        scratch = Path(self.c['scratch'])
        scratch.mkdir()
        outside = self.base / 'protected'
        outside.mkdir()
        (scratch / 'inputs').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            p._owned_path(scratch / 'inputs' / self.key, scratch / 'inputs', False)

    def test_cleanup_resumes_after_monthly_delete_and_preserves_other_roots(self):
        """Use two days/one task to exercise real deletion and crash retry."""
        input_path = Path(self.c['scratch']) / 'inputs' / self.key
        input_path.mkdir(parents=True)
        (input_path / 'chunk').write_text('registered input')
        old_month = self.base / 'old/monthly/REG/month.nc'
        old_month.parent.mkdir(parents=True)
        old_month.write_text('protected old result')
        quarantine = Path(self.c['scratch']) / 'quarantine/old'
        quarantine.mkdir(parents=True)
        (quarantine / 'chunk').write_text('protected quarantine')
        state = {'active_cube': 'token', 'phase': 'CLEANUP_WAIT', 'cleanup_authorized': True,
                 'cleanup_not_before_epoch': 1600, 'tasks': {self.key: {'region': 'token',
                 'kind': 'month', 'input_status': 'ready', 'prediction_status': 'done',
                 'lease': None, 'job': None, 'pid': None, 'reserved': 20}}}
        final = {'completed_epoch': 1000, 'monthly_receipts': [self.receipt], 'sha256': 'final-hash'}
        class Queue:
            def __init__(self, config):
                pass
            def snapshot(self):
                return copy.deepcopy(state)
            @contextlib.contextmanager
            def edit(self):
                candidate = copy.deepcopy(state)
                yield candidate
                state.clear()
                state.update(candidate)
        held = []
        @contextlib.contextmanager
        def locked(path, blocking=True):
            held.append(str(path))
            try:
                yield
            finally:
                held.remove(str(path))
        stub = types.SimpleNamespace(Queue=Queue, locked=locked)
        original_atomic = p._atomic
        interrupted = [False]
        def fail_after_delete(path, value):
            if Path(path).name == 'cleanup.json' and not interrupted[0]:
                interrupted[0] = True
                self.assertEqual(len(held), 2)
                raise OSError('simulated crash after removals')
            return original_atomic(path, value)
        with patch.dict(sys.modules, {'runtime': stub}), patch.object(p, 'MONTHS', 1), \
                patch.object(p, 'validate_final', return_value=final), patch.object(p.time, 'time', return_value=2000):
            with patch.object(p, '_atomic', side_effect=fail_after_delete), self.assertRaises(OSError):
                p.cleanup_cube(self.c, 'token')
            self.assertFalse(input_path.exists())
            self.assertFalse(Path(self.receipt['path']).exists())
            self.assertEqual(state['phase'], 'CLEANUP_WAIT')
            result = p.cleanup_cube(self.c, 'token')
            self.assertTrue(result['cleaned'])
            self.assertTrue(p.cleanup_cube(self.c, 'token')['cleaned'])
        self.assertEqual(state['phase'], 'CLEANED')
        self.assertEqual(state['tasks'][self.key]['reserved'], 0)
        self.assertEqual(state['tasks'][self.key]['input_status'], 'cleaned')
        self.assertTrue(old_month.is_file())
        self.assertTrue((quarantine / 'chunk').is_file())
        self.assertTrue((self.root / 'manifests/tasks' / self.key / 'registration.json').is_file())


if __name__ == '__main__':
    unittest.main()
