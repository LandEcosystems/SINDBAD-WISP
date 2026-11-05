# Absolute scenario distributions and the interim research brief

The report answers three different questions: how absolute values vary among
regions; how the same regions respond to a change in fire forcing; and how the
model agrees with reference estimates. They must not be conflated.

## Absolute distributions

For each saved/derived component, the vertical axis is its physical value. The
horizontal groups are **All cubes**, then original pyromes or vegetation types.
Each group contains separate No Fire and VIIRS Fire violin/box plots. Each dot
represents one region's mean of nine complete calendar years, 2015–2023. Fluxes
use annual integrals; stocks use annual means. The partial 2014 year is excluded.
All nine paired years and constant spatial support are required. Years are not
treated as additional independent regions. The paired-difference panel retains
small responses without changing the absolute-value axis.

Class values use uniquely owned class area within a cube. Mixed cubes therefore
contribute to more than one class, while the overall distribution counts each
cube once. Distributions weight cubes equally; summary tables separately report
area-weighted estimates. Neither is a design-weighted Mediterranean total.

All 58 components have overall, pyrome and vegetation figures and source tables.
Independent group atlases contain absolute scenario distributions, six
components per page. Existing pixel-wise and detailed reference figures remain
available in the earlier report.

## Research evidence

The discovery stage independently recomputes endpoints and spatial fire-exposure
cohorts from saved pixel summaries and reconciles endpoint values to the previous
tables. All-area and detection-positive-cohort comparisons use the full saved
period and clearly differ from the 2015–2023 complete-year distribution plots.
The detection cohort is fixed over the complete period; its time average includes
pre-fire days. It is not immediate fire severity.

Case studies are exploratory: select the three most negative final relative
vegetation-carbon responses among QA-passed regions with final no-fire vegetation
carbon at least 100 g C m−2 and owned area at least 100 km². Keep the ranking and
criteria in a CSV. These cases show strong responses, not typical effects or
independent evidence of a novel causal mechanism.

Carbon accounting separates direct combustion, reduced GPP and respiration
change. Internal transfers and mortality are not additional emissions. Pool
endpoints are checked separately from annual mean stocks.

## Annual reference support

Reuse audited complete-year reference pairs, accepting at least 90% spatial
support. Deduplicate overlapping cube pairs by product, variable, scenario,
native reference cell, year and carbon fraction, preferring higher coverage and
then stable region ID. The multi-product GPP comparison uses common coarse
geographic cells and years (2015–2021), preserving product-specific masks and
values. Its sample differs from each product's full available sample.

Grouped reference metrics weight mixed reference-cell values by original-class
overlap; they do not resolve class-specific observations inside coarse pixels.
CCI matches woody carbon rather than total vegetation carbon. The 0.51 carbon
fraction is sensitivity, not an uncertainty interval. LAI cannot be validated
because it was not saved/enabled in this experiment.

An additional exploratory screen identifies common reference cell-years where
both GPP references exceed 1,000 g C m−2 yr−1 while both model aggregates are
below 200. It checks the corresponding no-fire values and shows full common-cell
annual series for the two cubes with the most such pairs. These thresholds
identify diagnostic cases, not exclusion criteria or proof of an input error.
The two selected Nile Delta cubes warrant baseline checks, separately from the
three fire-response cases. Source hashes and snapshot identity bind this screen
to the rest of the report.

## Commands

Use the frozen analysis environment and set the analysis root externally:

```bash
python -m result_analysis.findings_report "$ANALYSIS_ROOT" component --index 0
python -m result_analysis.findings_report "$ANALYSIS_ROOT" discovery
python -m result_analysis.findings_report "$ANALYSIS_ROOT" reference
python -m result_analysis.findings_report "$ANALYSIS_ROOT" groups
python -m result_analysis.findings_report "$ANALYSIS_ROOT" anomalies
python -m result_analysis.findings_report "$ANALYSIS_ROOT" brief
python -m result_analysis.validate_findings_report "$ANALYSIS_ROOT"
```

Components can run as a 58-element Slurm array with 32 concurrent tasks. Each
owns its output directory. Group atlases and the brief run only after component
success. The brief additionally requires completed discovery and reference
records from the same snapshot. Results are written to `report_findings_v2`
inside the analysis root; original simulations and earlier reports remain intact.
