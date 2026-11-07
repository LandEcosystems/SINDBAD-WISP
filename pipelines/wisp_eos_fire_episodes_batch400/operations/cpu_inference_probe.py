"""Read-only CPU inference experiment against existing A100 results.

Never writes production predictions or claims campaign tasks. CPU precision
results stay in this experiment until their differences have been reviewed.
"""
from pathlib import Path
import argparse
import contextlib
import json
import os
import platform
import resource
import time

import numpy as np
import torch
import zarr
from wisp_batch.store import InputStore
from wisp_batch.benchmark import compare, OUTPUTS
from wisp_batch.common import atomic_json, array_hash
from wisp_med.eos import FrozenEOS


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--threads', type=int, default=8)
    args = p.parse_args()
    c = json.loads(args.config.read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    store = InputStore(c['reference_store'])
    baseline_path = Path(c['reference_store']).parents[1] / 'predictions/batches.zarr'
    baseline = zarr.open_group(str(baseline_path), mode='r')
    dates = ['2014-01-12T00:00:00', '2016-02-04T00:00:00', '2023-12-31T00:00:00']
    report = dict(host=platform.node(), job=os.environ.get('SLURM_JOB_ID'),
        threads=args.threads, torch=torch.__version__, device='cpu',
        cpu_model=next((x.split(':',1)[1].strip() for x in Path('/proc/cpuinfo').read_text().splitlines() if x.startswith('model name')), 'unknown'),
        input_store=str(store.path), a100_reference=str(baseline_path),
        production_outputs_modified=False, results=[], errors=[])
    start = time.perf_counter()
    model = FrozenEOS(c['checkpoint'], c['vendor_root'], device='cpu')
    model.model.requires_grad_(False)
    report.update(model_load_seconds=time.perf_counter()-start, model_identity=model.identity)
    for precision in ['bfloat16', 'float32']:
        for date in dates:
            index = store.dates.index(date)
            arrays = store.read(index)
            inputs = {k:torch.from_numpy(v).unsqueeze(0).contiguous() for k,v in arrays.items()}
            ref = {k:np.asarray(baseline['b1'][k][index,0]) for k in OUTPUTS}
            if not all(np.isfinite(a).all() for a in ref.values()):
                raise ValueError('A100 reference is incomplete for '+date)
            before = time.perf_counter()
            try:
                amp = torch.autocast('cpu', dtype=torch.bfloat16) if precision=='bfloat16' else contextlib.nullcontext()
                with torch.inference_mode(), amp:
                    out = model.model.inference(**inputs, hist_mask=None,
                        forecast_mode='parallel', zero_future_known=False)
                    result = dict(points_yx=out['pred_points'][0].float().numpy(),
                        scores=out['pred_class_logits'][0].softmax(-1)[...,1].float().numpy(),
                        logits=out['pred_class_logits'][0].float().numpy(),
                        score_map=out['pred_union_map'][0].float().numpy())
                elapsed = time.perf_counter()-before
                if any(a.shape!=OUTPUTS[k] or not np.isfinite(a).all() for k,a in result.items()):
                    raise ValueError('CPU result shape or finiteness failed')
                entry = dict(date=date, precision=precision, seconds=elapsed,
                    samples_per_second=1/elapsed, input_hashes={k:array_hash(v) for k,v in arrays.items()},
                    comparison_to_a100_b1=compare(result,ref))
                report['results'].append(entry)
                np.savez_compressed(args.output/(date[:10]+'_'+precision+'.npz'), **result)
                print(json.dumps({'date':date,'precision':precision,'seconds':elapsed,
                    'score_max_abs':entry['comparison_to_a100_b1']['arrays']['scores']['max_abs']}), flush=True)
            except Exception as exc:
                report['errors'].append(dict(date=date, precision=precision, error=repr(exc)))
                print('CPU PROBE ERROR',precision,repr(exc),flush=True)
                break
            finally:
                report['peak_rss_gib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2
                atomic_json(args.output/'report.json',report)
            del inputs, arrays, result, out
    report['complete']=True
    report['interpretation']='CPU timing and numeric differences only; not automatic production qualification'
    atomic_json(args.output/'report.json', report)


if __name__ == '__main__':
    main()
