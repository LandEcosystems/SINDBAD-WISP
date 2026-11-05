# WISP EOS: observed-fire episode pilot

This experiment processes **390 selected days across two existing cubes**, using the frozen WISP EOS corrected / Q10 / seed42 / epoch46 model. It tests whether limiting raw-input construction to observed-fire episodes reduces computing cost. It does not run SINDBAD or modify the 400 source cubes.

| Cube | Observed fire days | Selected days | Episodes | Input chunks |
|---|---:|---:|---:|---:|
| Granada, T000830 | 28 | 258 | 21 | 44 |
| Agadir, T000001 | 12 | 132 | 12 | 24 |
| Total | 40 | 390 | 33 | 68 |

## Selection

Read `viirs_fire_binary` from `[16:112,16:112]` of each accepted cube. Any pixel equal to one makes that date an observed-fire date. Missing or nonbinary observations fail selection; they are not interpreted as no fire. The accepted cube already applies type=0, confidence>1, and the audited NOAA-20 replacement of the 2022 S-NPP outage.

For each true fire date, include five days before and after it, once. Union these dates, merge adjacent intervals, and clip to 2014-01-12–2023-12-31. Prediction date D means `[D 00:00 UTC, D+1 00:00 UTC)`; there is no extra one-day shift. An extended boundary containing no new fire does not trigger further extension.

Each episode of L days is split into ceil(L/7) balanced contiguous chunks, at most seven days each. Eleven days becomes 6+5. Chunks may cross calendar months and years, but cannot cross an unselected gap. The frozen manifest and CSVs are in `selection_frozen_v1/`; the SHA256 includes source identities and the complete selection rules.

## Frozen scientific calculation

Every selected day still uses a complete raw 120-hour window `[D−96 h,D+24 h)`. Selection margins do not substitute for model history. A chunk processes days sequentially with the unchanged rolling reader. Different chunks independently load their initial historical context.

The adapter reuses the audited raw reader, full-window radiation normalization, frozen training mean/std, two original float16 conversions, and assembly of seven float32 model inputs. It does not reconstruct hourly data from the SINDBAD cube. Model weather for the target day is ERA5 reanalysis, so this is a hindcast. Future fire, FRP and vegetation are excluded from model inputs; the frozen first-day checks perturb these channels and verify unchanged tensors.

Scientific code: `482aec67d44caf35b320aaee095a0499e8f92bbf`.
Checkpoint SHA256: `efbf7db3208660efba7e98359a6fce5e01c4186f9039395087b3b59a538cccc3`.

The operational configuration may change only output/scratch locations and storage guards. It verifies the original scientific deployment, all frozen code checksums, its own immutable release, and the date-selection identity before execution.

## Execution and resources

```text
Freeze observed-fire dates and 68 chunks
    → two CPU pilot jobs (four producers; at least three fresh days each)
    → fresh 120-hour parity and ≤12 GiB job RSS gate
    → expand short-chunk raw construction
    → reread and verify all 390 daily inputs
    → one shared A100, batch=8
    → two sparse-date NetCDFs, final verification
    → wait at least ten minutes, isolated cleanup
    → finish comparison against the retained full-decade references
```

Each producer job requests 4 CPUs, 16 GiB and 24 hours, and runs two processes. Each process uses two computational threads; compression uses one. The ceiling of **32 queued/running non-GPU event jobs includes control and validation jobs**. Production uses at most 30 producer jobs (60 simultaneous chunks), leaving room for its controller and successor. This conservatively meets the requested maximum; actual concurrency depends on Slurm. Partitions are `work,small,gpu`, never `big`; CPU jobs request no GPU devices.

The event GPU job requests one A100 and runs batch=8. Eight different dates (four per region) are compared against batch=1 and repeated batch=1 first. They are eight dates, not eight independent regions. Numeric differences are measured, not asserted to be zero. A shared submission mutex prevents simultaneous GPU jobs across the event pilot and both existing full-decade lanes.

The current Granada and Agadir full-decade workers keep their leases and checkpoints. Separate continuation controllers let them finish prediction, validation and cleanup, then pause. The remaining fourteen cubes are deferred; the original sixteen-cube campaign is not reported as completed.

## Paths and isolation

Run root:

```text
/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/wisp_eos_fire_episodes_v1
```

Temporary inputs and private caches:

```text
/Net/Groups/BGI/tscratch/ybai/wisp_eos_fire_episodes_v1
```

The input/cache budget is 128 GiB. Per-task reservations and measured private-cache occupancy prevent new admission beyond the budget. A small audited fire index is copied, with checksums, into this private cache. No event lock controls a writable full-decade cache file.

Task keys contain region, selection digest and chunk ID. Stable POSIX lock files and owner leases prevent duplicate claims. CPU and GPU states are separate. GPU completion cannot delete input; only the final cleanup stage can do so. Transient I/O errors have bounded retries; deterministic errors are recorded and require inspection.

## Outputs and verification

- `manifests/`: selection, fire dates, episodes, chunks, source and normalization identities, per-day tensor hashes.
- `outputs/<region>/wisp_eos_q10_<region>_fire_episodes.nc`: selected dates only, central96 scores and valid mask, Q10 coordinates/logits/confidences, UTC time bounds and episode ID.
- `reports/`: pilot resource/parity gate, full input verification, batch-size differences, same-day comparisons against full-decade results, validation receipts and resource report.
- `logs/`: scheduler logs and append-only per-producer timing events.

All input days are reread and checked against their seven tensor hashes. Fresh full-window checks cover pilot dates and eight specified first/last/chunk-boundary cases. Existing retained full-decade tensor hashes must match exactly; a mismatch blocks inference. Predictions can differ slightly because of bfloat16 batch composition; their errors and threshold changes are reported.

Missing full-decade references remain explicitly pending. Event products and their safe input cleanup can finish first; a lightweight continuation refreshes comparisons when the original decades finish. `REFERENCE_WAIT` is not a claim that all comparisons passed.

Cleanup is restricted to this experiment's registered input paths, requires final output validation plus a 600-second retention period, and acquires exclusive task locks. It preserves final products, small chunk prediction files, source receipts, logs and hashes. It never deletes full-decade inputs.

## Deploy and operate

Use the existing frozen Python environment, and set `PYTHONPATH` to the audited `experiments/sindbad_wisp_batch` directory shown in the deployment configuration.

```bash
python -m unittest discover -s pipelines/wisp_eos_fire_episodes -p 'test_*.py' -v
python pipelines/wisp_eos_fire_episodes/install.py stage
```

Staging does not submit or cancel jobs. Verify the staged release, identify and stop **only** the two legacy controller jobs, then run `install.py attach`; it refuses attachment while a legacy controller exists and verifies that every live task payload is unchanged. Submit the two `continuation.py submit-control --manifest <original-dual-manifest> --lane a|b` controllers and the event `controller.py submit-control` from the staged code. Do not submit duplicate controllers or edit a live immutable release.

Progress is in `reports/progress.json`; blocked tasks and stage errors are separate. The installed `code/job.sh status` performs a checked readout. Recovery always resumes the same selected task; it does not change dates or substitute cubes.

## Interpretation

This is an **observation-conditioned retrospective event experiment**. It retains only 5.36% of the two full-decade prediction dates, but setup, background rereading and I/O mean runtime will not necessarily be 5.36%. Report measured CPU allocation hours, CPU seconds, wall time, RAM/cache peaks and GPU time separately.

Unselected dates are absent, never prediction zero. These outputs cannot establish annual false-positive rates, full-year emissions or real-time forecast skill. A continuous Gaussian score is neither a calibrated probability nor an observed burned-area fraction.
