# Batch1 WISP EOS fire-episode inference: 400 cubes, ten bounded lanes

This pipeline extends the audited two-region event pilot to the **fixed Batch1
400-cube catalog**. It performs frozen-model inference, not training. Source
NetCDFs, the existing full-decade runs, and the scientific WISP implementation
are read-only. It does not execute SINDBAD or replace its fire forcing.

## What is selected

For each accepted source cube, read `viirs_fire_binary` in the centre
`[16:112,16:112]`. A date with any pixel equal to 1 is an observed fire day.
Missing/nonbinary labels block selection; they never become no fire. The
accepted VIIRS product already applies the agreed S-NPP quality/type filter
and the audited NOAA-20 outage replacement.

Expand each observed fire date by five days on either side, union the dates,
merge adjacent intervals, and clip to **2014-01-12 through 2023-12-31**. Date D
denotes `[D 00:00 UTC, (D+1) 00:00 UTC)`, without another day shift. Split each
continuous episode into balanced chunks of at most seven prediction days.
Chunks partition the selected dates exactly once. They are scheduling units,
not a separate seven-day input dataset for each daily prediction.

The frozen reader uses `[D−96h,D+24h)` for every selected day. Within a chunk,
weather and historical fire reuse rolling caches; independent chunks reload
their initial history. Daily window normalization and tensor assembly remain
separate. Frozen training mean/std, the full 120-hour/native 128×128 radiation
normalization, both float16 conversions, seven float32 input groups, observed
VIIRS history, and bfloat16 inference are unchanged. Future fire, FRP and
vegetation never enter the model. Future ERA5 weather makes this a hindcast.

- Scientific model commit: `482aec67d44caf35b320aaee095a0499e8f92bbf`.
- Checkpoint SHA256: `efbf7db3208660efba7e98359a6fce5e01c4186f9039395087b3b59a538cccc3`.
- EOS corrected / Q10 / seed 42 / epoch 46; batch=8 on one A100.

## Scheduling and bounded storage

Ten lanes each own one region at a time. Ready regions are ordered by descending
selected-date count, then stable production tile ID. The first two lanes must
pass actual input/reference and memory qualification before expansion to ten.
Zero-fire regions publish an audited empty-time NetCDF and use no CPU/GPU
production workers. The two existing pilot products are reused only after
source/model/grid/selection and final-product checks.

| Limit | Setting |
|---|---|
| Active regions |10|
| Queued/running non-GPU jobs |320 total;32 per lane including auxiliary jobs|
| Producers |Up to30 jobs per lane, limited to the actual pending work|
| Producer resources |4 CPUs,16GiB,24h; two processes, two threads each|
| CPU partitions |`work,small,gpu`; no GPU device for CPU jobs; never `big`|
| Region input cache |256GiB|
| One wave |At most1000 dates; whole chunks; total reservations including8GiB fixed cache ≤240GiB|
| GPU |One shared A100,8CPU,64GiB,24h,batch8|
| Idle GPU |Exit after five minutes without a ready wave|

Resource limits are ceilings, not promises of simultaneous Slurm allocation.
CPU preparation can start while the old Granada/Agadir full-decade workflows
finish. The new GPU consumer waits until both legacy lanes are COMPLETE and
paused/deferred, then uses their existing global submission lock. This avoids
changing old controllers, which do not recognize the new job names. The older
fourteen deferred full-decade regions remain deferred.

The two-lane qualification requires completed short chunks, the two planned
independent raw-window parity dates per region, and job peak RSS ≤12 GiB with no unresolved
resource errors. Recovered transient I/O failures remain in the audit history.
A blocked initial qualification region stops expansion explicitly; it is not
silently replaced by a third qualification region. The region budget includes reservations, measured scratch and
quarantine. CPU workers never delete inputs. If thirty-minute median read
latency doubles while throughput falls (at least 20 dates in each comparison
window), the controller halves the producer ceiling and retires surplus
workers after their current chunk, recording the adjustment.

## Paths and outputs

Campaign root:
`/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/wisp_eos_fire_episodes_batch1_v2`

Scratch:
`/Net/Groups/BGI/tscratch/ybai/wisp_eos_fire_episodes_batch1_v2`

- `manifests/`: frozen 400 catalog, numerical deployment and checked fire-index source.
- `regions/Txxxxxx/manifests/`: per-region selection, waves, input hashes, source and QA identities.
- `regions/Txxxxxx/outputs/<region>/`: one final sparse-date NetCDF for newly inferred regions.
- `regions/Txxxxxx/reports/final.json`: validated final pointer, including a reused pilot path when applicable.
- `outputs/catalog.json`: all 400 region statuses, selected dates and final pointers.
- `reports/progress.json`: active phases, input/prediction dates, jobs, cache and blockers.
- `reports/io_monitor.json`: measured read latency and throughput windows.
- `logs/`: scheduler logs; detailed per-date timing journals stay with each region.

Products preserve real selected dates, UTC bounds, episode ID, central 96×96
score and validity maps, all Q10 points, logits/confidences, and provenance.
Empty products explicitly state `no_observed_fire`; no absent date is written
as a prediction of zero.

Each wave follows `BUILD → INPUT_VERIFY → PREDICT → WAVE_VERIFY → CLEANUP_WAIT`.
Registered temporary inputs can be removed only after durable chunk predictions
are reopened/validated, at least 600 seconds have elapsed, no owner remains, and
exclusive task locks are acquired. Input-day hashes and prediction receipts
remain. After all waves, the region merges and validates its complete selected
date product. Cleanup never reaches old campaigns or source data. Interrupted
cleanup resumes safely; completed GPU work is not confused with cached input.

Pending producers are retired once their wave inputs are complete, using a
server-side PENDING-state guard; running workers are not interrupted. Startup,
worker-owner recovery, and GPU I/O failures have persistent bounded counters.

Source-access errors allow at most three attempts; deterministic errors block
that region without substitution. A frozen identity change prevents further
execution. Failed or awaiting regions are not counted as completed. A campaign
is COMPLETE only when all 400 are verified predictions, audited reuse, or
confirmed no-observed-fire products.

## Deployment and operation

The deployment copies this folder and its unchanged sibling
`wisp_eos_fire_episodes` dependency into an immutable `code/` release. It records
Git identity and all code/config/catalog/source hashes. Do not patch a live
release. Tests require the existing frozen Python environment and its audited
WISP application on `PYTHONPATH`.

```bash
python -m unittest discover -s pipelines/wisp_eos_fire_episodes_batch400 -p 'test_*.py' -v
python pipelines/wisp_eos_fire_episodes_batch400/deploy.py --commit COMMIT_SHA
# After staged release verification:
CAMPAIGN_ROOT/code/run.sh submit-control
CAMPAIGN_ROOT/code/run.sh status
```

Before any input production has been admitted, `upgrade.py --commit COMMIT_SHA`
can install an operational fix after this campaign’s scanner/controller stop.
It refuses active jobs or started input work, archives the old immutable release,
and rebinds only operational identity hashes. Frozen selection, scientific
configuration, and source records remain unchanged. An interrupted upgrade is
resumable. Never use this helper to alter a campaign with active producers.

`submit-control` is idempotent while a registered controller exists. The
controller submits one source scanner, two qualification lanes, and then up to
ten lanes. It renews before its wall limit. Use the status command or progress
JSON instead of counting Slurm jobs as completed scientific outputs.

## Acceptance and interpretation

All inputs are reread with seven-array hash,shape,dtype and finiteness checks.
Each region has independent full 120-hour reference checks at its first selected
day and a block boundary. Frozen first-day checks at every chunk also verify
future-input isolation and reference assembly. Each region's first ready wave
records batch=1 repeat versus batch=8 numerical differences, explicitly padding
the gate only when fewer than eight distinct dates exist. GPU batching need not
be bitwise identical. Final output dates, grids, numerical values, metadata and
checksums are validated before completion.

The pilot's 390 days and two regions are not a representative timing sample of
all 400. Report total selected dates first, then update the remaining-time
estimate from measured production throughput. The pilot's pure GPU 48 seconds
excludes model startup, input construction, validation, output I/O and queueing.

This is an **observation-conditioned retrospective event experiment**. It
cannot measure whole-year false-positive rates, full-year CO₂ emissions, or
operational forecasting skill. Scores are not calibrated probabilities or
burned-area fractions. The workflow never silently fills unselected dates.
