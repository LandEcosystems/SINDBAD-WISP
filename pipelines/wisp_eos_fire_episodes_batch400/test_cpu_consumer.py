"""CPU execution identity, durable resume and frozen numerical interface tests."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import netCDF4 as nc
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cpu_consumer import CPUOutput, CPUModel, prediction_backend, validate_prediction, run_region


def result(value=0.3):
    return {'points_yx': np.full((10, 2), value, 'f4'),
            'scores': np.full((10,), value, 'f4'),
            'logits': np.full((10, 2), -value, 'f4'),
            'score_map': np.full((96, 96), value, 'f4')}


class CPUOutputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'manifests').mkdir()
        (self.root / 'manifests/deployment.json').write_text(json.dumps({'fingerprint': 'frozen-input-identity'}))
        self.c = dict(root=str(self.root), checkpoint_sha256='weights', code_commit='model')
        self.key = 'T000830_evt_test_B001'
        self.region = dict(region_id='T000830', lat=np.linspace(40, 39, 128).tolist(),
                           lon=np.linspace(-4, -3, 128).tolist())
        self.dates = ['2019-01-01T00:00:00', '2019-01-02T00:00:00']
        self.timing = dict(inference_backend='cpu', precision='float32', batch_size=1,
                           hardware='Intel test A', host='testhost', provenance_source='explicit')

    def writer(self, fingerprint='inputs'):
        return CPUOutput(self.c, self.key, self.region, self.dates, fingerprint)

    def test_partial_resume_preserves_first_day_and_records_processor_per_day(self):
        w = self.writer()
        w.write(0, result(0.3), self.timing)
        first = w.receipts['0']
        w.close()
        resumed = self.writer()
        self.assertTrue(resumed.has(0))
        self.assertFalse(resumed.has(1))
        resumed.write(0, result(0.8), self.timing)  # A completed date is never recomputed/overwritten.
        self.assertEqual(resumed.receipts['0'], first)
        resumed.write(1, result(0.4), dict(self.timing, hardware='Intel test B'))
        receipt = resumed.finish()
        self.assertEqual(receipt['cpu_processor_models'], ['Intel test A', 'Intel test B'])
        self.assertEqual((receipt['inference_backend'], receipt['precision'], receipt['batch_size']), ('cpu', 'float32', 1))
        with nc.Dataset(receipt['path']) as ds:
            self.assertEqual((ds.inference_backend, ds.precision, ds.batch_size), ('cpu', 'float32', 1))
            np.testing.assert_array_equal(ds['wisp_eos_fire_score'][0], result(0.3)['score_map'])
            np.testing.assert_array_equal(ds['wisp_eos_fire_score'][1], result(0.4)['score_map'])
        with self.assertRaisesRegex(ValueError, 'recovered, not recomputed'):
            self.writer()

    def test_wrong_inputs_or_backend_cannot_reuse_partial(self):
        w = self.writer()
        w.write(0, result(), self.timing)
        w.close()
        with self.assertRaisesRegex(ValueError, 'another input or execution backend'):
            self.writer('different')
        with nc.Dataset(w.partial, 'a') as ds:
            ds.fingerprint = 'gpu-fingerprint'
        with self.assertRaisesRegex(ValueError, 'another input or execution backend'):
            self.writer()

    def test_partial_corruption_is_detected_on_resume(self):
        w = self.writer()
        w.write(0, result(), self.timing)
        w.close()
        with nc.Dataset(w.partial, 'a') as ds:
            ds['query_confidence'][0, 0] = 0.99
        w = self.writer()
        try:
            with self.assertRaisesRegex(ValueError, 'Corrupt partial'):
                w.has(0)
        finally:
            w.close()

    def test_finish_requires_all_dates_and_write_requires_explicit_cpu_provenance(self):
        w = self.writer()
        try:
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                w.finish()
            with self.assertRaisesRegex(ValueError, 'provenance'):
                w.write(0, result(), dict(self.timing, precision='bfloat16'))
        finally:
            w.close()

    def test_rename_before_receipt_can_resume_without_repredicting(self):
        w = self.writer()
        for i in range(2):
            w.write(i, result(), self.timing)
        w.close()
        os.replace(w.partial, w.final)  # Simulate termination before publication receipt.
        resumed = self.writer()
        self.assertTrue(all(resumed.has(i) for i in range(2)))
        receipt = resumed.finish()
        self.assertTrue(Path(receipt['path']).exists())


class CPUInterfaceTests(unittest.TestCase):
    def test_backend_context_restores_gpu_default_or_original_setting(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with prediction_backend():
                self.assertEqual(os.environ['WISP_INFERENCE_BACKEND'], 'cpu')
            self.assertNotIn('WISP_INFERENCE_BACKEND', os.environ)
        with mock.patch.dict(os.environ, {'WISP_INFERENCE_BACKEND': 'cuda'}):
            with prediction_backend():
                self.assertEqual(os.environ['WISP_INFERENCE_BACKEND'], 'cpu')
            self.assertEqual(os.environ['WISP_INFERENCE_BACKEND'], 'cuda')

    def test_bad_predictions_rejected_before_publication(self):
        for name, value in [('scores', np.nan), ('points_yx', -0.1), ('score_map', 1.1)]:
            output = result()
            output[name].flat[0] = value
            with self.assertRaises(ValueError):
                validate_prediction(output)
        output = result()
        output['scores'] = output['scores'].astype('f8')
        with self.assertRaises(ValueError):
            validate_prediction(output)

    def test_cpu_model_uses_seven_inputs_frozen_kwargs_and_float32_softmax(self):
        import torch
        import wisp_batch.common as common
        torch.set_num_threads(1)
        seen = {}
        class Graph:
            def inference(self, **kwargs):
                seen.update(kwargs)
                self.assertions = (torch.is_inference_mode_enabled(), torch.is_grad_enabled())
                return dict(pred_points=torch.full((1, 10, 2), 0.2),
                            pred_class_logits=torch.tensor([[[0., 1.]]] * 10).reshape(1, 10, 2),
                            pred_union_map=torch.full((1, 96, 96), 0.4))
        model = CPUModel.__new__(CPUModel)
        model.model, model.hardware, model.threads = Graph(), 'test cpu', 8
        shapes = {name: (2,) for name in common.SHAPES}
        arrays = {name: np.ones(shape, 'f4') for name, shape in shapes.items()}
        with mock.patch.object(common, 'SHAPES', shapes):
            output, timing = model.predict(arrays)
        self.assertEqual(model.model.assertions, (True, False))
        self.assertEqual(set(seen), set(shapes) | {'hist_mask', 'forecast_mode', 'zero_future_known'})
        self.assertEqual((seen['hist_mask'], seen['forecast_mode'], seen['zero_future_known']), (None, 'parallel', False))
        self.assertTrue(all(seen[name].shape == (1, 2) and seen[name].dtype == torch.float32 for name in shapes))
        np.testing.assert_array_equal(output['scores'], torch.tensor([0., 1.]).softmax(-1)[1].repeat(10).numpy())
        self.assertEqual((timing['inference_backend'], timing['precision'], timing['batch_size']), ('cpu', 'float32', 1))


class CPUSharedQueueTests(unittest.TestCase):
    def setUp(self):
        import adapter as ad
        self.ad = ad
        ad.rt.STOP = False
        ad.rt._claims.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'campaign/regions/T000830'
        self.scratch = self.base / 'scratch/campaign/regions/T000830'
        self.c = dict(root=str(self.root), scratch=str(self.scratch),
                      checkpoint_sha256='weights', code_commit='model',
                      shared_cache_reserve_bytes=0, scratch_budget_bytes=2**40,
                      scratch_min_free_bytes=0)
        self.key = 'T000830_evt_test_B001'
        self.dates = ['2019-01-01T00:00:00', '2019-01-02T00:00:00']
        self.region = dict(region_id='T000830', lat=np.linspace(40, 39, 128).tolist(),
                           lon=np.linspace(-4, -3, 128).tolist())
        self.task = dict(region='T000830', kind='episode', dates=self.dates,
                         input_status='ready', prediction_status='queued', status='ready',
                         reserved=123, priority=10, input_fingerprint='inputs')
        self.wave = dict(wave_id=0, keys=[self.key], days=2)
        self.put('state/queue.json', dict(phase='PREDICT', active_wave=0,
                                        waves=[self.wave], tasks={self.key: self.task}))
        self.put('manifests/waves.json', {'waves': [self.wave]})
        self.put('manifests/regions.json', {'T000830': self.region})
        self.put('manifests/deployment.json', {'fingerprint': 'deployment'})
        self.inputpath = self.scratch / 'inputs' / self.key
        self.inputpath.mkdir(parents=True)
        (self.inputpath / '_SUCCESS.json').write_text('{}')
        self.put('manifests/tasks/' + self.key + '/registration.json',
                 dict(key=self.key, scratch_path=str(self.inputpath),
                      identity=dict(self.region, region='T000830', deployment='deployment', dates=self.dates)))
        parent = self
        class Store:
            def __init__(self, path):
                self.path, self.fingerprint, self.dates = Path(path), 'inputs', parent.dates
            def read(self, index):
                parent.reads.append(index)
                return {'fake': np.array([index], 'f4')}
        class Model:
            def check(self, c):
                pass
            def predict(self, arrays):
                parent.predicted.append(int(arrays['fake'][0]))
                return result(), dict(inference_backend='cpu', precision='float32', batch_size=1,
                                      hardware='Intel CPU test', provenance_source='explicit')
        self.store, self.model, self.reads, self.predicted = Store, Model(), [], []

    def put(self, relative, value):
        self.ad.atomic_json(self.root / relative, value)

    def run_cpu(self, **kwargs):
        with mock.patch.object(self.ad, 'verify_execution', return_value={}), \
             mock.patch('wisp_batch.store.InputStore', self.store):
            return run_region(self.c, model=self.model, **kwargs)

    def test_cpu_and_gpu_share_claims_and_completed_input_is_not_repredicted(self):
        model, count = self.run_cpu()
        self.assertEqual(count, 1)
        self.assertEqual(self.predicted, [0, 1])
        self.assertTrue((self.inputpath / '_SUCCESS.json').exists())  # No consumer-side deletion.
        task = self.ad.Queue(self.c).snapshot()['tasks'][self.key]
        self.assertEqual((task['prediction_status'], task['prediction_backend']), ('done', 'cpu'))
        self.assertIsNone(task['lease'])
        self.assertEqual(self.run_cpu()[1], 0)
        with self.assertRaises(self.ad.WaveFinished):
            self.ad.Queue(self.c).claim('gpu')
        self.assertEqual(self.predicted, [0, 1])

    def test_cpu_day_checkpoint_is_pinned_against_gpu_and_resumes_only_missing_day(self):
        _, count = self.run_cpu(stop_requested=lambda: len(self.predicted) == 1)
        self.assertEqual(count, 0)
        task = self.ad.Queue(self.c).snapshot()['tasks'][self.key]
        self.assertEqual((task['prediction_status'], task['prediction_backend']), ('queued', 'cpu'))
        self.assertIsNone(task['lease'])
        self.assertIsNone(self.ad.Queue(self.c).claim('gpu'))
        self.assertEqual(self.run_cpu()[1], 1)
        self.assertEqual(self.predicted, [0, 1])
        self.assertEqual(self.reads, [0, 1])

    def test_cpu_cannot_take_an_active_gpu_lease(self):
        claimed = self.ad.Queue(self.c).claim('gpu')
        self.assertEqual(claimed[1]['prediction_backend'], 'cuda')
        self.assertEqual(self.run_cpu()[1], 0)
        self.assertEqual(self.predicted, [])


if __name__ == '__main__':
    unittest.main()
