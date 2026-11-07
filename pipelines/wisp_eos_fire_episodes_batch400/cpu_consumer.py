"""CPU float32 prediction using the same exclusive task claims as the GPU.

This is an explicitly authorized alternative numerical backend. Input tensors,
weights, inference graph, softmax ordering and rendering remain frozen; CPU
float32 results are never described as CUDA/bfloat16 results. A task is pinned
to its first prediction backend, so partial outputs can resume without either
consumer recomputing the other consumer's dates.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import platform
import time
import traceback

import numpy as np

OUTPUT_SHAPES = {'points_yx': (10, 2), 'scores': (10,),
                 'logits': (10, 2), 'score_map': (96, 96)}
FIELDS = {'score_map': 'wisp_eos_fire_score', 'points_yx': 'query_points_yx',
          'scores': 'query_confidence', 'logits': 'query_logits'}
_THREADS_CONFIGURED = False


def cpu_model_name():
    try:
        for line in Path('/proc/cpuinfo').read_text().splitlines():
            if line.startswith('model name'):
                return line.split(':', 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def validate_prediction(result):
    if set(result) != set(OUTPUT_SHAPES):
        raise ValueError('CPU prediction has missing or extra fields')
    for name, shape in OUTPUT_SHAPES.items():
        value = result[name]
        if value.shape != shape or value.dtype != np.dtype('float32') or not np.isfinite(value).all():
            raise ValueError('Invalid CPU result shape, dtype or values: ' + name)
        if name != 'logits' and (np.any(value < 0) or np.any(value > 1)):
            raise ValueError('CPU prediction outside [0,1]: ' + name)


class CPUModel:
    """The frozen EOS model graph executed on CPU without autocast."""

    def __init__(self, c, threads=8):
        global _THREADS_CONFIGURED
        import torch
        from wisp_med.eos import FrozenEOS
        if not _THREADS_CONFIGURED:
            torch.set_num_threads(threads)
            torch.set_num_interop_threads(1)
            _THREADS_CONFIGURED = True
        torch.manual_seed(42)
        np.random.seed(42)
        loaded = FrozenEOS(c['checkpoint'], c['vendor_root'], device='cpu')
        self.model = loaded.model.float().eval()
        self.model.requires_grad_(False)
        self.identity = dict(loaded.identity, precision='float32',
                             inference_backend='cpu', batch_size=1)
        self.hardware = cpu_model_name()
        self.threads = threads
        self.check(c)

    def check(self, c):
        if (self.identity.get('checkpoint_sha256') != c['checkpoint_sha256']
                or self.identity.get('model_commit') != c['code_commit']
                or self.identity.get('precision') != 'float32'
                or self.identity.get('inference_backend') != 'cpu'
                or self.identity.get('history_mode') != 'observed_viirs'
                or self.model.training):
            raise ValueError('CPU model identity or evaluation mode differs')

    def predict(self, arrays):
        import torch
        from wisp_batch.common import SHAPES
        if set(arrays) != set(SHAPES):
            raise ValueError('CPU prediction requires all seven frozen input groups')
        for name, value in arrays.items():
            if value.shape != tuple(SHAPES[name]) or value.dtype != np.dtype('float32') or not np.isfinite(value).all():
                raise ValueError('Invalid frozen CPU input: ' + name)
        inputs = {k: torch.from_numpy(v).unsqueeze(0).contiguous() for k, v in arrays.items()}
        start = time.perf_counter()
        with torch.inference_mode():
            out = self.model.inference(**inputs, hist_mask=None,
                                       forecast_mode='parallel', zero_future_known=False)
            # Preserve the corrected validation's softmax-before-cast order.
            result = dict(points_yx=out['pred_points'][0].float().numpy(),
                scores=out['pred_class_logits'][0].softmax(-1)[..., 1].float().numpy(),
                logits=out['pred_class_logits'][0].float().numpy(),
                score_map=out['pred_union_map'][0].float().numpy())
        elapsed = time.perf_counter() - start
        validate_prediction(result)
        timing = dict(inference_seconds=elapsed, inference_backend='cpu',
            precision='float32', batch_size=1, hardware=self.hardware,
            threads=self.threads, provenance_source='explicit', host=platform.node(),
            job=os.environ.get('SLURM_JOB_ID'), real_members=1)
        return result, timing


class CPUOutput:
    """Frozen output arrays with an honest, resumable CPU execution identity.

The field values, per-day read-back hashes and day journalling use the same
writer methods as CUDA. Only execution metadata and its fingerprint differ.
"""

    def __init__(self, c, key, region, dates, input_fingerprint):
        import netCDF4 as nc
        from wisp_stream.common import digest, read_json
        from wisp_stream.sources import outage_history_flag
        self.c, self.key, self.region, self.dates = c, key, region, list(dates)
        self.meta = Path(c['root']) / 'manifests/tasks' / key
        self.meta.mkdir(parents=True, exist_ok=True)
        self.final = Path(c['root']) / 'monthly' / region['region_id'] / (key + '.nc')
        self.final.parent.mkdir(parents=True, exist_ok=True)
        self.partial = self.final.with_suffix('.partial.nc')
        self.journal = self.meta / 'prediction_days.json'
        self.receipts = read_json(self.journal) if self.journal.exists() else {}
        self.identity = {'deployment': read_json(Path(c['root']) / 'manifests/deployment.json')['fingerprint'],
                         'input': input_fingerprint, 'key': key, 'dates': self.dates,
                         'batch': 1, 'hardware': 'CPU', 'inference_backend': 'cpu', 'precision': 'float32'}
        self.fp = digest(self.identity)
        if self.final.exists() and not self.partial.exists() and not (self.meta / 'output.json').exists():
            os.replace(self.final, self.partial)
        if (self.meta / 'output.json').exists():
            raise ValueError('Existing completed prediction must be recovered, not recomputed: ' + key)
        self.ds = nc.Dataset(self.partial, 'a' if self.partial.exists() else 'w')
        ds = self.ds
        ds.set_auto_mask(False)
        if 'fingerprint' in ds.ncattrs():
            if ds.fingerprint != self.fp:
                self.close()
                raise ValueError('Partial prediction has another input or execution backend')
            return
        if self.receipts:
            self.close()
            raise ValueError('Prediction journal exists without its partial data')
        for name, size in [('time', len(dates)), ('lat', 96), ('lon', 96), ('query', 10),
                           ('axis', 2), ('class', 2), ('bounds', 2)]:
            ds.createDimension(name, size)
        ds.setncatts({'fingerprint': self.fp, 'region_id': region['region_id'],
            'deployment_fingerprint': self.identity['deployment'], 'checkpoint_sha256': c['checkpoint_sha256'],
            'model_commit': c['code_commit'], 'input_fingerprint': input_fingerprint,
            'batch_size': 1, 'precision': 'float32', 'inference_backend': 'cpu',
            'history_mode': 'observed_viirs', 'experiment': 'ERA5-reanalysis hindcast, not operational forecast',
            'score_interpretation': 'Unmodified EOS union score; not a calibrated probability or burned-area fraction',
            'input_protocol': 'history-only-vegetation/v1',
            'crop': '128x128 input; [16:112,16:112] native 96x96 output',
            'hardware': 'CPU', 'hardware_detail': 'Actual processor, host and threads recorded per date in prediction_days.json',
            'numerical_backend_note': 'Authorized CPU float32 inference; differences from A100 bfloat16 are possible',
            'source_policy': 'S-NPP with NOAA-20 raw hourly supplementation 2022-07-27 through 2022-08-10; low/nominal/high retained',
            'Conventions': 'CF-1.8'})
        for name in ('lat', 'lon'):
            var = ds.createVariable(name, 'f8', (name,))
            var[:] = np.asarray(region[name])[16:112]
            var.units = 'degrees_north' if name == 'lat' else 'degrees_east'
        t = ds.createVariable('time', 'i8', ('time',))
        t[:] = np.asarray(dates, dtype='datetime64[s]').astype('int64')
        t.units = 'seconds since 1970-01-01 00:00:00'
        t.calendar, t.bounds = 'proleptic_gregorian', 'time_bounds'
        bounds = ds.createVariable('time_bounds', 'i8', ('time', 'bounds'))
        bounds[:] = np.stack([t[:], t[:] + 86400], axis=-1)
        bounds.units = t.units
        for name in ('query', 'axis', 'class'):
            ds.createVariable(name, 'i2', (name,))[:] = np.arange(len(ds.dimensions[name]))
        ds['axis'].description = '0=y, 1=x, normalized over native 96x96 output'
        ds['class'].description = '0=no-object, 1=fire'
        dims = {'score_map': ('time', 'lat', 'lon'), 'points_yx': ('time', 'query', 'axis'),
                'scores': ('time', 'query'), 'logits': ('time', 'query', 'class')}
        for key, name in FIELDS.items():
            ds.createVariable(name, 'f4', dims[key], zlib=True, complevel=3, fill_value=np.nan).units = '1'
        for axis in ('latitude', 'longitude'):
            var = ds.createVariable('query_' + axis, 'f8', ('time', 'query'), zlib=True, complevel=3, fill_value=np.nan)
            var.units = 'degrees_north' if axis == 'latitude' else 'degrees_east'
        ds.createVariable('wisp_eos_valid_mask', 'u1', ('time', 'lat', 'lon'), zlib=True, complevel=3, fill_value=255)
        flag = ds.createVariable('noaa20_in_history', 'u1', ('time',))
        flag[:] = [outage_history_flag(d) for d in dates]
        flag.description = '96h historical input source footprint, including frozen half-hour rounding, intersects approved outage substitution interval'
        ds.sync()

    def has(self, i):
        from wisp_stream.outputs import MonthOutput
        return MonthOutput.has(self, i)

    def write(self, i, result, timing):
        from wisp_stream.outputs import MonthOutput
        validate_prediction(result)
        if timing.get('inference_backend') != 'cpu' or timing.get('precision') != 'float32' or timing.get('batch_size') != 1:
            raise ValueError('Missing explicit CPU numerical provenance')
        return MonthOutput.write(self, i, result, timing)

    def finish(self):
        import netCDF4 as nc
        from wisp_stream.common import atomic_json, file_hash, now
        if not all(self.has(i) for i in range(len(self.dates))):
            raise ValueError('Incomplete CPU prediction chunk')
        hardware = sorted({v['timing']['hardware'] for v in self.receipts.values()})
        self.ds.setncattr('cpu_processor_models_json', json.dumps(hardware))
        self.ds.close()
        with self.partial.open('rb') as handle:
            os.fsync(handle.fileno())
        os.replace(self.partial, self.final)
        with nc.Dataset(self.final) as ds:
            ds.set_auto_mask(False)
            if any(not np.isfinite(ds[name][:]).all() for name in FIELDS.values()):
                raise ValueError('Nonfinite CPU output after reopening')
        receipt = dict(key=self.key, path=str(self.final), sha256=file_hash(self.final),
            validated=True, fingerprint=self.fp, input_fingerprint=self.identity['input'],
            days=len(self.dates), date_start=self.dates[0], date_end=self.dates[-1],
            hardware='CPU', cpu_processor_models=hardware, inference_backend='cpu',
            inference_hardware_by_day=[self.receipts[str(i)]['timing']['hardware'] for i in range(len(self.dates))],
            precision='float32', batch_size=1, completed=now())
        atomic_json(self.meta / 'output.json', receipt)
        return receipt

    def close(self):
        if self.ds.isopen():
            self.ds.close()


@contextlib.contextmanager
def prediction_backend():
    previous = os.environ.get('WISP_INFERENCE_BACKEND')
    os.environ['WISP_INFERENCE_BACKEND'] = 'cpu'
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('WISP_INFERENCE_BACKEND', None)
        else:
            os.environ['WISP_INFERENCE_BACKEND'] = previous


def run_region(c, model=None, max_tasks=1, stop_requested=None):
    """Predict up to max_tasks and promptly return (model, completed_count).

The queue stage remains ``gpu`` for both numerical backends. It means
prediction, not hardware, and supplies the same lease and per-task file lock.
Only the controller/cleanup stages can delete inputs after durable validation.
"""
    import adapter as ad
    from batch_products import validate_chunk_provenance
    from wisp_batch.store import InputStore
    root = Path(c['root'])
    ad.verify_execution(c)
    queue = ad.Queue(c)
    done = 0
    for _ in range(max_tasks):
        if ad.rt.STOP or (stop_requested and stop_requested()):
            break
        before = queue.snapshot()
        with prediction_backend():
            try:
                claimed = queue.claim('gpu')
            except ad.WaveFinished:
                break
        if not claimed:
            break
        key, task = claimed
        writer = None
        try:
            with ad.locked(root / 'locks/tasks' / (key + '.lock'), False):
                if task.get('prediction_backend') != 'cpu':
                    raise ValueError('Shared prediction lease did not pin CPU backend')
                if model is None:
                    model = CPUModel(c, threads=int(os.environ.get('SLURM_CPUS_PER_TASK', '8')))
                model.check(c)
                store = InputStore(ad.common.task_paths(c, key)[0])
                if (not (store.path / '_SUCCESS.json').exists() or store.fingerprint != task['input_fingerprint']
                        or store.dates != task['dates']):
                    raise ValueError('CPU inference input is not complete or differs from its task')
                region = ad.read_json(root / 'manifests/regions.json')[task['region']]
                writer = CPUOutput(c, key, region, task['dates'], store.fingerprint)
                for i, day in enumerate(task['dates']):
                    if writer.has(i):
                        continue
                    if (ad.rt.STOP or (root / 'state/STOP').exists()
                            or (stop_requested and stop_requested())):
                        raise ad.rt.StopAtCheckpoint()
                    started = time.perf_counter()
                    arrays = store.read(i)
                    read_seconds = time.perf_counter() - started
                    result, timing = model.predict(arrays)
                    writer.write(i, result, dict(timing, input_read_seconds=read_seconds))
                    del arrays, result
                    ad.heartbeat(c, 'cpu-infer', task=key, date=day, **timing)
                receipt = writer.finish()
                report = validate_chunk_provenance(c, key, receipt, task)
                ad.atomic_json(root / 'manifests/tasks' / key / 'prediction_verified.json', report)
                queue.update(key, status='done', output_sha256=receipt['sha256'],
                             inference_backend='cpu', inference_precision='float32')
                done += 1
                print('CPU_PREDICTED', key, len(task['dates']), flush=True)
        except ad.rt.StopAtCheckpoint:
            if writer is not None:
                writer.close()
            queue.update(key, status='queued', checkpointed_at=ad.now())
            break
        except Exception as exc:
            if writer is not None:
                writer.close()
            queue.update(key, status='blocked', failed_stage='gpu', error=str(exc),
                         inference_backend='cpu', inference_precision='float32')
            ad.record_gpu_failure(c, exc, before)
            ad.atomic_json(root / 'reports/errors' / ('cpu_infer_' + str(time.time_ns()) + '.json'),
                           dict(task=key, error=str(exc), traceback=traceback.format_exc(), at=ad.now()))
            raise
    return model, done
