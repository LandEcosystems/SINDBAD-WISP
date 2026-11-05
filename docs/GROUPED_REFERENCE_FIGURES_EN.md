# MODIS / FLUXCOM-X comparisons by pyrome, vegetation and forest × pyrome

This extends the named-product, three-row reference figure: regional distributions,
No_Fire versus product, then VIIRS_Fire versus the same product. Each page contains
MODIS GPP, FLUXCOM-X GPP, FLUXCOM-X NEE, MODIS ET and FLUXCOM-X ET. The existing
overall figure, reference CSVs, simulations and production jobs are unchanged.

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python -m result_analysis.reference_grouped_data "$ANALYSIS_ROOT" --workers 4
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 python -m result_analysis.reference_grouped_figures "$ANALYSIS_ROOT" --workers 4
```

Results are in
`report_findings_v3_all293/sujan_followup_v1/grouped_references_v1/`.
Three PDF collections provide six pyrome pages (including unknown), five plant-form
pages, and six forest × pyrome pages. Per-group PNG/PDF files and exact plotted CSV
tables are retained alongside a detailed methods README and support coverage table.

## Spatial interpretation

The frozen 2015–2021 reference pairs retain their original 0.05-degree support,
90% model-coverage requirement and deterministic overlap deduplication. Both model
scenarios always use identical pairs. No high-resolution reference is manufactured.

Group areas come from the original 2019 classification, runtime masks and archived
source-to-model area crosswalk. Forest is IGBP 1–5, shrubland 6–7, savanna 8–9,
grassland 10 and wetland 11. Scientific classes are not the recoded model PFTs.
Forest × pyrome is computed from their joint source-cell membership; products of
marginal forest and pyrome fractions would be incorrect and are not used.

For reference cell j and class g:

    A[j,g] = sum_p intersection_area[j,p] * original_class_fraction[p,g]
    class_weighted_mean = sum_j A[j,g] * whole_cell_value[j] / sum_j A[j,g]

**These are class-area-weighted comparisons of whole reference-cell fluxes, not
unmixed class-specific fluxes.** A reference cell with 30% forest still carries a
mixed model/reference value. Its forest area supplies its weight; its value is not
relabelled as pure-forest GPP. The figure captions and output README state this.

Group means are computed within each cube/year, then averaged over seven complete
years. Each cube contributes one top-row point. Scatter RMSE/correlation and density
use the original-class area weights. The 293 completed cubes are descriptive;
sampling weights are not used to claim Mediterranean-wide totals.

## QA

- Each pyrome and vegetation partition sums to natural area; forest-pyrome groups
  sum to forest area. The forest intersections retain unknown pyromes.
- Broad-class areas are checked against independently archived reference weights
  for every contributing region. Coordinates, reference bounds and source identities
  are checked before joining the frozen pairs.
- Numerical boundary slivers no larger than 0.01 m² are excluded from grouped
  sample counts; raw area arrays remain unchanged and omitted areas are reported.
- Paired values are not changed by grouping. Mixed support and ≥90%-class support
  counts are reported, without an outcome-dependent purity filter.
- Common per-product axes and density scales apply across groups and scenarios.
  Positive bins below 0.01 km² yr remain visible at the lowest colour; a colourbar
  extension identifies this, without dropping observations or altering metrics.
- Sample sizes below ten use points/boxes without a smoothed violin; missing
  comparisons are explicit. Hexbin area weights must sum to their input weights.
- Per-region cached data and per-figure outputs are separate; four CPU processes
  can work independently, with PDF assembly only after all figures succeed.
