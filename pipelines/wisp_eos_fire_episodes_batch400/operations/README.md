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

Recovery preserves the immutable scientific/operational release, task queues,
completed days, and input data. Stop only the campaign controller, use the
recovery helper to archive matching node failures and restore their scheduling
state, then restart the same controller through the policy wrapper. Never
clear unrelated errors or disable the frozen numerical probe. Confirm
`ExcNodeList=node-r6-he05` on the newly submitted jobs.

Before enabling the wrapper, exercise it with `sbatch -vv --test-only` and
verify the reported `exclude` option. Runtime receipts and the wrapper hash
belong in the campaign's `operations/node_policy_v1/` audit directory.
