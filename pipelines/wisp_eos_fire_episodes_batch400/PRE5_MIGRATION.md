# Pre-fire-only selection: Batch1 v3

For observed fire days F, select the union of [f−5 days, f], inclusive, clipped
to 2014-01-12—2023-12-31. Overlaps are counted once. A day after one fire may
still be selected as a preceding day of a later fire. Include the fire day.

The 400 audited source cubes yield 56,714 observed fire region-days and
**196,456 selected region-days**, versus 285,874 with ±5 days: **89,418 fewer
(31.28%)**. Seven cubes have no observed centre-crop fire and need no inference.
These are the total selected dates, not the remaining workload.

This changes date selection only. Every selected day still uses the frozen
120-hour window [D−96h,D+24h), training normalization, two float16 conversions,
seven float32 tensors, observed fire history, and EOS Q10 bfloat16 inference.

## Versioned cutover

1. Request old controller/region checkpoint stops. Preserve the old release,
   selection, results, hashes and input checkpoints. Confirm no old registered
   jobs remain before import; no two controllers may use the same job namespace.
2. Stage a separate immutable release with `deploy.py --migration-source OLD`.
   Use the `wisp_eos_fire_episodes_batch1_v3_pre5` output and scratch roots.
3. `run.sh migrate-init` derives all 400 selections from already audited source
   fire dates. Source grid, file stat, QA and scientific deployment stay fixed.
   There is no region search and no source-cube rebuild.
4. `run.sh migrate-region TOKEN` imports selected existing inputs/predictions.
   A source 1–7-day chunk is intersected with the new selection, splitting gaps.
   This preserves prior boundaries and prevents mixtures of completed and
   uncomputed parent tasks. The result has 38,526 short chunks.
5. `run.sh migrate-finish` requires 400 completed migration reports. Resume the
   controller with the qualified-host `sbatch` wrapper, ten lanes and a maximum
   of 14 producer jobs per lane (140 total); one shared A100, batch 8.

## Reuse without changing scientific values

- Prediction sources must pass the original final/chunk validation. Every copied
  date is checked against its original per-array journal. Native result writers
  preserve logits, points, scores and maps exactly; all new chunks are validated.
  Completed/no-fire regions never run CPU construction or GPU inference again.
- Input stores use new date-specific identities. Compressed day chunks are
  hard-linked (copied if across filesystems); original seven-tensor checksums and
  receipt provenance are retained. No values are re-normalized. The ordinary
  CPU reader and wave validator must decode and verify every reused tensor
  before GPU inference; a link alone is not recorded as verified input completion.
- If trimming changes a chunk's first day and the saved input lacks its frozen
  first-day checks, that boundary day is rebuilt/checked by the original builder.
- Copied predictions have no new input directory. Their source input journals
  remain durable, and cleanup explicitly verifies this case before skipping it.
- Source caches remain preserved during migration. They are not deleted merely
  because new links exist. New input cleanup retains the normal 600-second
  delay, no-active-writer checks and registered-path restrictions.

All new final files describe the actual asymmetric rule. Unselected dates are
absent, not predicted zero. This remains a retrospective, observation-conditioned
experiment; it does not evaluate full-year false alarms.
