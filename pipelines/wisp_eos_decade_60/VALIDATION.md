# Validation record — 4 November 2026

- Archived engine: every file matches the deployed `engine/release.json` identity.
- Engine regression suite: **56 passed** after copying into this repository.
- Bounded campaign regression suite: **10 passed**, including a controller-hook
  simulation through all 16 authorized cubes and no admission of a 17th cube.
- Source cubes: original acceptance records, size/mtime and complete-file SHA256
  verified for Granada and all 15 additional cubes before manifest publication.
- Existing pilot: six jobs, two producers/job, at least three new dates/producer;
  seven input arrays exactly match fresh frozen references. Pilot passed at
  2026-11-04 18:30:53 UTC (19:30:53 CET).
- Live 60-job expansion has run successfully. The first decade is still building
  at publication time. These checks do **not** label its future GPU output or
  cleanup as already validated. See the live per-cube acceptance reports.
- Selection rule: preserve the active cube, then take the next 15 already enrolled
  accepted cubes in the original frozen order. This changes execution scope only;
  the 400-cube population, statistical weights and original catalog stay intact.
- Controller handover preserves the tested engine release, current worker jobs,
  month leases, temporary inputs and frozen science. Only the controller entry
  point changes to enforce finite campaign scope and bounded renewal.

Commands are listed in the README. The scientific dependency deployment is
external and is verified at worker/controller startup; it is not a vendored
standalone numerical model in this repository.
