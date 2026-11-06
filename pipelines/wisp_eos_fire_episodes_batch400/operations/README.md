# Node-specific startup recovery

The frozen numerical probe rejected `node-r6-he05` (AMD EPYC 7662). On
2026-11-05, 119 startup failures across seven regions occurred before task
claiming or input creation; 128 job qualifications on 21 other hosts passed.
This is a compute-backend incompatibility, not missing source data.

The versioned `sbatch` wrapper adds an explicit `--exclude=node-r6-he05` only
to this campaign's `w4e-*` submissions. Its directory is prepended to PATH
only when launching this campaign controller. The inherited PATH covers
producer jobs and controller renewals. Existing workers and other experiments
are untouched. The installed Slurm ignored `SBATCH_EXCLUDE` in a dry run,
so that environment variable is not used as the exclusion mechanism.

## 2026-11-06: qualified-host policy and broader recovery

The first single-node exclusion was insufficient. The overnight audit found
123 regions blocked on six additional AMD EPYC 7662 hosts. All seven failing
hosts were in the GPU partition. These are CPU preprocessing qualification
failures, before children are created or input tasks are claimed; they do not
mean the source regions are unusable. Healthy workers' input checkpoints stay
in place.

`qualified_sbatch.py`, deployed as `operations/node_policy_v2/bin/sbatch`,
restricts only this campaign's non-GPU submissions to hosts with recorded
successful frozen numerical probes. The policy initially contains 25 hosts;
the current Slurm inventory is consulted at every submission so unknown hosts
are excluded too. Existing explicit exclusions are preserved. `w4e-gpu` and
unrelated jobs pass through unchanged: excluding every GPU host from GPU
inference would make the A100 request impossible. The producer's own numerical
probe remains mandatory. Host evidence and policy belong in
`operations/node_policy_v2/node_policy.json` in the shared run directory.

`recover_startup_blocks.py` replaces the hard-coded seven-region recovery for
this incident. First use `freeze_manifest` to pin the explicitly reviewed
regions, failure hosts, frozen release, error files, counters and queue hashes.
Apply requires the controller to have stopped and no target-region job or lease
to remain. A write-ahead journal preserves the original bytes. Only the matched
startup error, its counter and the controller's empty STOP signal are removed;
queues and inputs are never rewritten. Regions return to READY or ACTIVE
according to existing lane ownership. Different errors are refused.

Restart the campaign controller with the new wrapper directory first in its
private PATH. Controller renewals inherit that PATH. Do not change global shell
configuration or edit the immutable numerical deployment. Confirm the effective
`ExcNodeList` on the submitted controller and CPU jobs.

The independent legacy GPU handover repair is documented in
`LEGACY_GPU_GATE_RECOVERY.md`.

Recovery preserves the immutable scientific/operational release, task queues,
completed days, and input data. Stop only the campaign controller, use the
recovery helper to archive matching node failures and restore their scheduling
state, then restart the same controller through the policy wrapper. Never
clear unrelated errors or disable the frozen numerical probe. Confirm
`ExcNodeList=node-r6-he05` on the newly submitted jobs.

Before enabling the wrapper, exercise it with `sbatch -vv --test-only` and
verify the reported `exclude` option. Runtime receipts and the wrapper hash
belong in the campaign's `operations/node_policy_v1/` audit directory.
