# WISP EOS: one complete decade cube at a time

This operational release prioritizes `T000830` (Granada, Spain), followed by the
remaining accepted regions in the frozen 400-region catalog, in numeric tile
order. It changes scheduling and output lifecycle, not WISP mathematics.

## Execution

`BUILD -> INPUT_VERIFY -> PREDICT -> MERGE -> CLEANUP_WAIT -> CLEANED -> next cube`

The first six CPU jobs each request 4 CPUs and 16 GiB and launch two independent
Python producer processes. Each process owns one month; dates within a month
remain chronological and reuse the rolling window. After each of twelve pilot
producers commits at least three new dates, its first new date has exactly
matched a fresh frozen full-window reference, and all six jobs have aggregate
RSS at most 12 GiB without resource errors, the controller permits at most 60
CPU jobs (120 concurrent months). CPU partitions are `work,small,gpu`; `big` is
excluded everywhere. CPU work on a GPU node requests no GPU device. CPU job
wall time is 24 hours. A hard ceiling of 120 queued/running jobs includes all
jobs belonging to this operational release. Each producer retains two math
threads; Blosc compression uses one thread. The controller records actual CPU
usage, elapsed time and aggregate process RSS, rather than interpreting memory
allocation as memory consumption.

The GPU phase starts only after all 120 input months have been reopened and all
seven tensors checked for every one of the 3,641 dates. One A100 runs batch 8.
The original mixed-batch qualification report and numerical CPU probe remain
required. Reused predictions must match the rebuilt input identity.

## Scientific protocol

* Forecast dates: 2014-01-12 through 2023-12-31, UTC midnight starts.
* Weather for forecast start `t0`: `[t0-96h, t0+24h)`. The first raw weather hour
  is 2014-01-08 00:00 UTC; the last is 2023-12-31 23:00 UTC.
* Each month independently loads its necessary history margin. Vegetation
  remains causal, using the frozen composite lookback policy. Observed VIIRS
  history and the approved NOAA-20 outage substitution remain unchanged.
* Frozen training mean/std come from the existing provenance constants. No
  statistics are refitted on these cubes. The two radiation fields retain the
  prescribed 120-hour/native-tile min-max step before global normalization.
* Frozen special handling and both float16 conversions remain unchanged; the
  stored seven tensors are float32 and are not normalized again during inference.
* Model: EOS corrected/Q10/seed42/epoch46; commit
  `482aec67d44caf35b320aaee095a0499e8f92bbf`; checkpoint SHA256
  `efbf7db3208660efba7e98359a6fce5e01c4186f9039395087b3b59a538cccc3`.

The scientific configuration stays frozen. The only runtime configuration
difference is the operational output root; the adapter checks the complete
original scientific deployment and its packages, source configuration and
normalization identity before using that root. Operational files have a separate
immutable release manifest. Existing scientific modules are imported read-only.

## Storage and independent output

Root: `/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/wisp_eos_cube_first_v1`

* `inputs/<token>/decade_manifest.json`: verified index into 120 monthly Zarr
  input shards on the registered scratch filesystem. There is no second full
  decade input copy.
* `outputs/<region_id>/wisp_eos_q10_<region_id>_20140112_20231231.nc`: final
  96-by-96 daily score map, valid mask, all Q10 points/confidence/logits,
  geographic coordinates, time bounds and frozen provenance.
* `reports/progress.json`: active cube, phase, job and month state.
* `reports/pilot_passed.json`, `reports/pilot_parity/`, `state/job_resources/`:
  scientific and resource checks authorizing expansion.
* `reports/regions/`: final product checksums and monthly provenance.
* `code/migration/`: stop audit, original records, copied-output validation and
  actual scratch allocation accounting.

The final score is the frozen EOS union score, not calibrated probability or a
measured burned-area fraction. These files do not modify the 400 SINDBAD cubes.

The 1 TiB input-cache limit includes current-cube reservations, actual allocated
blocks of parked inputs, retained quarantine and shared cache allowance. It is
separate from Slurm RAM. All 120 active months reserve 8 GiB each. Insufficient
capacity stops admission; it does not authorize deleting parked inputs.

## Recovery and cleanup

Input and prediction states are separate. A validated prediction whose inputs
were previously deleted is retained, but its inputs are rebuilt before the full
decade input checkpoint. Partial month work resumes from committed daily array
receipts. New worker signals are handled at daily checkpoints. The migration of
old workers preserves committed days; any interrupted uncommitted day is rebuilt.

All writers use stable-inode POSIX record locks and unique leases. Old scheduling
and cleanup stop before new workers start; the new ledger admits only its active
cube and stage. Transient source failures have bounded retries; deterministic
source/model or validation errors stop progress for investigation.

Monthly predictions remain until the merged final NetCDF is reopened and every
variable compared against its source months. A separate cleaner waits at least
600 seconds after final validation, verifies the final checksum and 120 exclusive
task locks, then removes only registered active-cube input shards and this run's
monthly products. It retains final outputs, original old-run products, logs,
receipts and provenance. Cleanup is journaled and resumes after interruption;
the next cube is admitted only after cleanup completes.

## Timing

The earlier 5–7 hours was an aggregate-throughput extrapolation, not measured
single-cube latency. Concentrated month scheduling is intended to deliver the
first complete cube sooner. Read-stage cost includes I/O, decoding and spatial
processing; allocating more CPU cores alone does not establish a speedup.
Progress and new resource observations must be used for subsequent estimates.
