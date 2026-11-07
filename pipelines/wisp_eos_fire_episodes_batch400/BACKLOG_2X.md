# Two-fold input backlog and fixed producer capacity

This operational revision retains the same 400 source cubes, pre-five-day date
selection, frozen model, normalization, task identities and per-region waves.

- Up to 20 resident regions, each bounded by its existing 256 GiB reservation:
  total conservative input budget **5 TiB**, twice the former 2.5 TiB.
- Up to 10 BUILD regions concurrently, 21 producer jobs per region and **210
  producer jobs globally**. Each job requests 4 CPUs and 16 GiB and runs two
  independent task owners. Controller/validation jobs are additional.
- A region waiting for the A100 retains its inputs but frees a CPU-building
  slot. Another READY region can enter a free resident slot. Existing complete,
  reused and no-observed-fire regions are never admitted again.
- IO measurements remain visible. Under the explicit `observe` policy they
  cannot repeatedly halve the producer cap. Disk guards, memory limits, task
  locks and validation remain enforced. Fixed concurrency does not guarantee
  fixed throughput on a shared filesystem.
- Stop admission when 20 slots are occupied or free filesystem space is below
  one region reservation plus 1 TiB. Do not overrun the budget to keep CPUs busy.
- Keep the single A100 consumer, batch=8. Publish and verify wave predictions,
  wait at least 600 seconds, confirm no active task owners, then clean inputs.
  Region finalization and intermediate-prediction cleanup remain unchanged.

`operations/cpu_inference_probe.py` is a separate CPU benchmark. It reads the
existing three reference days and compares CPU bfloat16/float32 outputs with
saved A100 batch=1 outputs, including threshold and query-position differences.
It does not claim production tasks or modify the scientific prediction products.
