# Reference products, support matching, and interpretation

This pipeline compares SINDBAD estimates with other estimates. Neither MODIS nor FLUXCOM-X is a direct measurement of every simulated pixel. Shared FLUXNET calibration and remote-sensing inputs limit statistical independence. The word **reference**, rather than **truth**, is used throughout.

## Executable stages

```bash
python -m result_analysis.references compare \
  --summary /analysis/regions/REGION/compact.nc \
  --output /analysis/regions/REGION/references
```

Deployment paths are supplied through private JSON configuration (`mod17_template`, `cci_path`, `fluxcom_manifest`, `qa_root`) or environment variables `SINDBAD_MOD17_TEMPLATE`, `SINDBAD_CCI_BIOMASS_PATH`, `SINDBAD_ANALYSIS_ROOT`, `SINDBAD_BATCH_QA_ROOT`. The optional MOD16 2015–2021 adapter uses `WISP_ANALYSIS_MOD16_ARCHIVE` and a shared, locked scratch tile cache. Site paths and analysis data are not committed.

The compact input contains unchanged model-grid coordinates, scenarios, the accepted run mask, original-2019 forest fractions, monthly flux sums (with the initial partial month excluded during comparison), native-MODIS-period GPP sums, and annual mean `cVegWood`. Source results remain read-only. Files or units that fail verification are recorded in `statuses.json`; the adapter never silently substitutes an undocumented local product.

## Source register

| Reference | Model quantity | Native/source support | Conversion and caveat |
|---|---|---|---|
| MOD17A2HGF C6.1, processed local Gpp | GPP | 0.05°, eight-day composites, local 2014–2021 | Already decoded **kg C m⁻² per composite**; multiply by 1,000 only. Do not apply the original 0.0001 scale a second time. Upstream native QA is not retained in this processed file, which limits reference-quality claims. |
| FLUXCOM-X X-BASE v1.0 | GPP, NEE, ET, transpiration | Official ICOS 0.05° monthly files, 2014–2021 | Official GPP/NEE monthly rates are `gC m-2 d-1`; multiply by actual calendar days for the monthly sum. Published water rates are `mm hr-1` (also confirmed by ICOS variable metadata), so multiply by 24 to obtain a daily rate, then by calendar days. This conversion is recorded per source file. Negative NEE is uptake. No automatic multiplication or division by `land_fraction`. |
| ESA CCI Biomass v6 coarse regional product | `cVegWood` in original forest support | Approximately 0.08186°, available annual epochs | CCI AGB is aboveground **woody dry mass**, excluding roots and foliage. Convert Mg ha⁻¹ × 100 × carbon fraction. Main fraction 0.47; 0.51 is a conversion sensitivity, not a confidence interval. No extra tree-fraction multiplication. |
| MOD16A2GF C6.1 | ET | Raw 500 m sinusoidal HDF archives available locally | The adapter uses audited unique 2015–2021 granules, decodes valid-range ET, requires all 46 composites in each year, retains inherited input QC, and aggregates to an explicit 0.05° processed reference grid. The checked 2014-01-01 archive contains ambiguous reprocessing versions and is explicitly excluded until its source identity is resolved. |
| MOD15A2H C6.1 | LAI | Local 0.05° processed product available | Model LAI is absent and not uniquely reconstructible from the saved leaf-carbon state. No synthetic LAI error is calculated. |

Sources:

- [NASA MOD17A2HGF.061](https://doi.org/10.5067/MODIS/MOD17A2HGF.061)
- [FLUXCOM-X product and publication](https://www.icos-cp.eu/data-products/fluxcom-x-global-fluxes-collection), [Nelson et al. (2024)](https://bg.copernicus.org/articles/21/5079/2024/)
- [Official ICOS monthly GPP collection](https://meta.icos-cp.eu/collections/AYj7-lwcdCLnBXJDoscxQZou)
- [CCI Biomass v6 user guide](https://climate.esa.int/media/documents/D4.3_CCI_PUG_V6.0_20250606.pdf)
- [IPCC forest carbon fractions, Table 4.3](https://www.ipcc-nggip.iges.or.jp/public/2006gl/pdf/4_Volume4/V4_04_Ch4_Forest_Land.pdf)

The official FLUXCOM files are distributed by ICOS RI under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Derived caches retain their object URL, publisher's source hash, size, ETag, source units, licence and geographic crop. A local SHA256 verifies each cropped cache; it is **not** represented as a recomputed hash of the un-downloaded global file.

## Official FLUXCOM cache

Standard local X-BASE links were broken during preflight. `old_aggs` exists but lacks a sufficiently proven unit/provenance chain. The primary preparation route therefore discovers official annual collection members and reads regional portions of the published 0.05° monthly NetCDF through HTTP ranges:

```bash
python -m result_analysis.references prepare-fluxcom-monthly \
  --variables GPP NEE ET ET_T --years 2014 2015 2016 2017 2018 2019 2020 2021 \
  --bbox -12 28 40 46 --workers 4 \
  --output ${ANALYSIS_ROOT}/reference_cache/fluxcom_official_monthly
```

This is one shared Mediterranean crop, not a separate download for every cube. Range responses are checked for exact byte offsets and stable object identity; HTML licence pages, full-file responses and short reads are rejected. The official complete calendar-month bounds must match. Source missing data remain missing. On this cluster the networked login host can fetch these caches while compute workers read the local cache. A separate explicit hourly-Zarr route remains available, but it is not the default because its four-year chunks create unnecessary I/O for monthly comparisons.

## Spatial comparison

For a native reference pixel `j`, compute intersection areas `A_ij` between its geographic cell and model cells, then:

\[
M_j=\frac{\sum_i A_{ij}M_i}{\sum_i A_{ij}}.
\]

The overlap operator integrates the WGS84 ellipsoid between parallels and meridians. Ascending and descending axes are supported and tested. Missing model values reduce valid support; they are never set to zero.

The initial conservative support screen uses the **full reference-pixel area** when the reference's corresponding land-area denominator has not been independently established. Main pairs require at least 90% support; 80% results are retained as a sensitivity. This can exclude coastal or incomplete pixels more strictly than an audited land-only denominator. The report names that limitation. `land_fraction` is retained for future denominator auditing, not used to rescale fluxes speculatively.

CCI comparisons require original forest support. A mixed coarse pixel with less than the required supported forest area is excluded from the formal forest-wood comparison. A forest-only model mean is not presented as if the entire coarse observation were pure forest.

`model_grid_reference_display.npz` provides a second, visual product: each model-cell centre receives the value of the **containing** native reference cell. Gaps are not filled from a neighboring reference cell. Native source-cell IDs are saved and coarse-pixel values copied to the model grid do not count as independent observations.

## Temporal comparison

- MODIS intervals start on days 1, 9, 17, …; the final interval contains five or six days. Full model daily flux sums are paired with composite totals. The partly available initial January 2014 interval is not reconstructed by dividing or extrapolating.
- FLUXCOM monthly rates become totals using the actual number of days, including February 29. The core summary contains a 20-day January 2014 sum; the comparison adapter explicitly excludes that partial month rather than presenting it as a complete monthly sum.
- CCI annual woody biomass is compared with the model's annual mean woody-carbon state. Its acquisition/annual-epoch timing remains a limitation; it is not a precisely dated instantaneous observation.
- `annual_reference_pairs.csv.gz` only sums complete, finite, contiguous calendar-year flux periods. Stocks are not summed. Multi-product rankings should use the intersection of common coordinates and complete years 2015–2021, not different valid supports.

## Metric and grouping semantics

`reference_pairs.csv.gz` retains accepted and excluded pairs and reasons. Formal metrics use effective overlap-area weights and report Bias, MAE, RMSE, Pearson r, r² and NSE separately. Correlation and NSE are undefined when their required variance is zero. One independent support unit is a native reference pixel and matched interval, **not** every fine pixel-day.

Across overlapping cubes, duplicate native supports must be resolved before pooled metrics using a fixed rule: retain the highest-coverage pair, then the lexicographically first region ID for ties. Never select the smaller-error pair. Product, scenario, source-cell ID, period and carbon-fraction sensitivity identify a duplicate. All regional rows remain available for audit.

`reference_group_weights.csv.gz` derives the original pyrome/vegetation/IGBP overlap from the frozen source crosswalk. A mixed native observation remains the same coarse observation. Group statistics weight that native-support agreement by the group's intersection area; they do **not** claim to observe a separate class-specific flux inside the coarse pixel. Model PFT is a parameter diagnostic, distinct from original vegetation. CCI groups are further restricted to forest support.

Current completed regions are a non-random subset of the planned probability sample. This adapter emits paired reference agreement, not a claimed unbiased Mediterranean total. It does not apply inclusion weights or invent area semantics. Regional absolute model/reference totals remain gated pending denominator verification.

## Outputs and reproducibility

Each region receives:

- `reference_pairs.csv.gz`: all native-support pairs, source-cell IDs, units, support, exclusion reason and 90/80% acceptance.
- `reference_metrics.csv`: period-level metrics by actual year and all available years.
- `annual_reference_pairs.csv.gz` and `annual_reference_metrics.csv`: complete annual sums or annual stock states.
- `reference_group_weights.csv.gz` and `reference_group_metrics.csv`: actual class-area contributions and grouped agreement.
- `model_grid_reference_display.npz` plus JSON: explicitly visual-only coarse values on model coordinates.
- `statuses.json`: completed, missing or scientifically blocked products.
- `reference_complete.json`: schema, code/config/source fingerprint, output identities and timestamp. Relevant MOD16 granule identities are rechecked before accepting an existing completion marker. This stage marker means the attempted comparisons finished; individual blockers remain visible and do not become successful validations.

Tests cover interval lengths, leap years, partial periods, missing-value propagation, axis reversal, no spatial overlap, forest support, additive ellipsoid areas, carbon/water conversions, constant-series metrics, r² versus NSE, actual NetCDF decoding, prevention of double MODIS scaling and resumable execution.
