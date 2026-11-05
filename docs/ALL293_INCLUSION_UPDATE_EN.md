# All-293-region inclusion update

The user requested inclusion of the 46 regions excluded by the original strict
nonnegativity gate. The revised report includes all 293 completed regions from
the frozen inventory. Original simulation outputs, QA files, prior reports and
production jobs are unchanged. This is a new analysis version.

The 46 regions have very small negative **soil-water storage** values, not
negative soil carbon. Among them, 27 also have negative ET. Observed minima are
−3.8146973 × 10⁻⁶ mm for soil water and −5.6339559 × 10⁻⁸ mm/day for ET.
There are no negative carbon stocks or direct combustion values in the checked
outputs. The tiny water residuals are consistent with floating-point effects;
the first timestep producing them has not been traced to prove a root cause.

## Inclusion policy

- Preserve all raw values, including the tiny negatives; do not clip or impute.
- Retain the strict QA failures as numerical notes in an explicit inclusion ledger.
- Only soil-water nonnegativity failures no smaller than −10⁻⁵ mm and ET failures
  no smaller than −10⁻⁶ mm/day are eligible for this dataset-specific allowance.
- Any other failed check, nonfinite value or larger negative stops this report.
  These guardrails are not proposed as universal physical tolerances.
- Recompute the 58 stored/derived component summaries, all grouping families and
  annual net atmospheric fire contributions for all 293 regions.
- Recompute the former 247-region cohort separately and reconcile it to the
  previous report. Save a 247-versus-293 sensitivity table.

Only complete years 2015–2023 enter annual comparisons. A distribution point is
one cube's nine-year mean, not an independently sampled pixel or year. Fluxes use
annual totals; stocks use annual means unless explicitly labelled final-day.
Area-weighted summaries use unique owned natural-vegetation support. They remain
descriptions of this interim cohort, not unbiased Mediterranean-wide estimates.

## Compact delivery

`report_findings_v3_all293/` contains:

- `THREE_RESEARCH_FINDINGS_ALL293_EN.pdf`: four pages, comprising a new all-293
  overview and the three previously verified case-study pages.
- `GROUPED_GPP_ET_CVEG_293_EN.pdf`: six companion pages, with paired distributions
  for overall/pyrome and overall/vegetation comparisons for GPP, ET and cVeg.
- `SHORT_EMAIL_EN.md`, `.txt`, `.html` and `SHORT_EMAIL_DRAFT.eml`: a substantially
  shortened email, with the main PDF attached to the unsent draft. No email is sent.
- Inclusion ledger, full component/group summaries, region-level means, annual
  carbon contributions, source checksums and validation evidence.

The three individual case-study figures are unchanged: those cubes were already
included in the previous cohort. The new overview and grouped summaries genuinely
include the additional 46. Case studies were selected exploratorily and do not
establish a typical effect. No new all-region reference comparison is claimed;
the two Nile Delta reference comparisons retain their original matched support.

Use the frozen analysis Python environment, with pypdf on `PYTHONPATH`:

```sh
python -m result_analysis.inclusive_report ROOT prepare
python -m result_analysis.inclusive_report ROOT plots
python -m result_analysis.inclusive_report ROOT email
python -m result_analysis.inclusive_report ROOT package
```

`ROOT` is `/Net/Groups/BGI/tscratch/ybai/sindbad_wisp_result_analyse_v1`.
The `email` stage refreshes the count of completed cubes. Its conditional
2–4-day forecast is attributed to the prior production timing audit; it is not
a guaranteed completion date.
