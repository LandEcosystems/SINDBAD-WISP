# Two concurrent cubes: authorized extension of the 60-job pipeline

The original 60-job engine is retained unchanged. The 16 previously frozen cubes
are split deterministically into two disjoint lanes (alternating original order).
Lane A starts with Granada `T000830`; lane B starts with Agadir `T000001`.
Each lane advances only after its own full input verification, prediction,
NetCDF verification, 10-minute retention interval, and independent cleanup.
Each lane stops after its eight assigned cubes; this does not select more cubes.

| Setting | Per lane | Combined maximum |
|---|---:|---:|
| CPU preparation jobs, queued + running | 60 | 120 |
| Month producers per job | 2 | 240 concurrent months |
| Resource request per CPU job | 4 CPU, 16 GiB | 480 CPU, 1,920 GiB |
| Input storage admission ceiling | 1 TiB | 2 TiB |
| GPU inference | Shared A100, batch 8 | One GPU job |

Controller and validation jobs are additional small jobs; **120 is the CPU
preparation job cap**, replacing the old total-WISP-job cap. Actual running
counts depend on Slurm and remaining months. CPU jobs request `work,small,gpu`,
never `big`, and do not request GPU devices. No other users' jobs or unrelated
Batch1 tasks are changed.

## Preserving work and ownership

- Only the previous controller is stopped during handover. Granada CPU workers,
  month leases, and daily checkpoints remain intact.
- Lane A retains the original run root. Lane B uses `lanes/lane_b` inside it.
- Each lane has independent queues, task locks, receipts and results. No cube
  belongs to both lanes. Shared fire-index cache locks point to the same existing
  lock inodes, avoiding concurrent cache creation under different locks.
- Input tensors stay at their registered scratch paths; no tensor duplicates.
- The sum of two unchanged 1 TiB worker/admission ceilings is at most 2 TiB.
  Admission assigns all B-token directories to B, other parked inputs and
  quarantine to A; shared cache reserve is conservatively counted in both.
- The original normalization, raw sources, weather margins, model weights and
  numerical Python engine are unchanged. B's engine differs only in its launcher
  path. Its resource pilot is explicitly inherited from the byte-identical
  validated Python engine, not misreported as a fresh B-cube scientific result.
- Full seven-input and final-product verification still runs separately for
  each cube. Completed final products are never removed by input cleanup.

## Deployed paths

Root:
`/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/wisp_eos_cube_first_v1`

Dual manifest and launchers: `campaigns/dual120_v1/`.
The original 16-cube manifest is retained and hash-bound by the dual manifest.
Lane A reports remain in `reports/`; B reports are in `lanes/lane_b/reports/`.
Their final outputs likewise remain within their respective roots.

`dual_campaign.py` wraps the validated engine controller. A shared submission
lock enforces the global CPU and GPU caps, including pending jobs. A shared
admission lock protects capacity checks. `install_dual.py` stages the new lane;
`--attach` refuses an active old controller and preserves all live task leases.
Do not rerun the old single-lane campaign controller after this handover.

## Validation

14 campaign/dual tests and 56 engine tests pass. Tests cover lane ownership,
complete/disjoint partitioning, counting scratch directories once, rejecting
shared roots/changed budgets, and existing scientific/lifecycle regression
checks. Operational handover, reservations, Slurm registrations, and initial
worker progress are checked separately in the deployment audit.

No claim is made that twice the concurrent CPU work halves either cube's wall
time: shared storage throughput can limit speed. Completion time must follow
measured progress, especially the slowest month.

## Recovering an index-lock access failure

Transient ENXIO errors were observed when B workers opened the shared index-lock
path. A later probe on an affected node succeeded; the exact filesystem cause
was not established. `repair_index_links.py` converts the B symlinks into hard
links to the same source inodes, without recreating source locks or interrupting
lock holders. It requeues only unowned tasks matching the recorded error.

`resume_failed_months.py` can replace failed child capacity while its sibling is
still working. This one-shot recovery may temporarily exceed 60 parent jobs in
one lane, but always holds the global submission lock and stays within **120
CPU jobs across both lanes**. It submits at most one job per two queued months.
No extra cube is activated and no input data is deleted. Error logs are retained.
