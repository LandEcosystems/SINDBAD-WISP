# Parallel figures and a concise scientific report

This rendering workflow reuses the completed numerical analysis and reference
comparisons. It does not rerun SINDBAD, WISP or reference harmonization.

The previous serial workflow rendered about 21 separate figures for every
component. The replacement preserves the analytical scope but organizes it as:

- A concise main report selected by predefined scientific topics, never by
  significance or effect size: GPP, NEE, ET, vegetation carbon, ecosystem carbon,
  direct fire emissions, reference agreement and fire-response mechanisms.
- An all-component appendix: three composite pages per component (58 components
  in the current snapshot). These contain the overview, combined overall/pyrome/
  vegetation distributions and the pixel-wise scenario comparison.
- Separate pyrome and vegetation atlases. Three components share each page;
  each retains its own scatter, violin/box, unit and numerical data.
- Reference and scientific appendices, plus all original-class/PFT metric tables.

The main report need not reproduce every technical diagnostic. Failed QA,
missing LAI, partial 2014, unavailable reference years, reference uncertainty
and the limits of the completed subset remain explicit.

## Execution

Use the audited scientific environment with `pyarrow` and a separate pinned
`pypdf==6.1.1` installation. Do not modify a shared model environment.

```bash
python -m result_analysis.parallel_report "$ANALYSIS_ROOT" prepare
python -m result_analysis.parallel_report "$ANALYSIS_ROOT" worker --index 0
python -m result_analysis.parallel_report "$ANALYSIS_ROOT" merge
```

For automatic Slurm dispatch, set `ANALYSIS_ROOT`, `ANALYSIS_CODE`,
`ANALYSIS_PYTHON` and `ANALYSIS_PDF_LIBRARY`, then submit
`slurm_plot_prepare.sh`. Its `launch` stage checkpoints the array job IDs and
submits an `afterok` merge dependency. It never restarts model controllers.

Preparation reads the accepted regional summaries once and partitions annual
and monthly tables by component. The prepared manifest records input identities,
checksums, code, QA membership, task ordering and the frozen region inventory.
Component and group workers use separate directories and atomic completion
records. The suggested Slurm array runs up to 32 component/group tasks at once,
one CPU and 4 GiB per task. Science and pooled-reference rendering use separate
two-CPU, 16-GiB workers. PDF merging runs only after all required tasks succeed.

The original serial report and its completed files remain untouched. Overview
and combined figures can be reused only after their paired values exactly match
the new task's values and their PNG/PDF exports can be decoded. Existing pixel
figures and their exact-metric records are retained under the same frozen input
identity. Reuse is recorded in each task manifest.

Each worker publishes its own PDF, source tables and checksums. The merger
verifies all required tasks and input identities, checks PDF page counts, and
publishes the main and component reports atomically. A missing or corrupt task
prevents final publication. No worker writes into another worker's PDF or cache.
Interrupted locks are not stolen automatically; verify the owner has stopped
before restoring that task.

Outputs are in `report_parallel_v1/` below the analysis output root. Start with
`REPORT_EN.md` or `REPORT_EN.pdf`; use `component_index.csv` for the full appendix.
The figures remain evidence, not automatic scientific discoveries.
