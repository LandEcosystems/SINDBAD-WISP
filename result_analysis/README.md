# SINDBAD–WISP result analysis

An independent, read-only analysis of Sujan's `WISP_V1_202610` entity runs.
This package does **not** run SINDBAD, run WISP, edit forcings, or resume model controllers.

## Questions and scope

1. How do productivity, water fluxes and woody carbon agree with reference estimates?
2. What changes between paired No_Fire and VIIRS_Fire simulations?
3. How do responses vary across original vegetation classes, formal pyromes, carbon stocks and climate?

The first immutable snapshot contains 293 completed regions from a planned 400.
It is an **interim completed subset**, not a design-unbiased Mediterranean total.
The current WISP forcing is a copy placeholder and is not analysed as a prediction.
LAI is not saved or uniquely reconstructable; no supplementary model run is performed.

## Run

Use the existing scientific environment, or install `requirements.txt` in an isolated environment:

```bash
export PYTHONPATH=/path/to/this/checkout
python -m result_analysis --config result_analysis/config.json inventory
python -m result_analysis --config result_analysis/config.json audit
python -m result_analysis --config result_analysis/config.json region --index 0
python -m result_analysis --config result_analysis/config.json references --region REGION_ID
python -m result_analysis --config result_analysis/config.json report
python -m result_analysis --config result_analysis/config.json status
python -m unittest discover -s tests -v
```

`inventory` freezes source IDs and stat fingerprints, hashes saved code/settings,
and never interprets a done marker as scientific acceptance. `audit` independently
extracts effective JLD2 forcing, PFT parameters, source masks and statistical ownership.
`region` checks those dependencies before and after computation; incompatible
versions fail rather than silently mix. The engine reads native NetCDF chunks with
the full time axis, saving compact summaries instead of a second daily dataset.

For Slurm, set `ANALYSIS_CODE`, `ANALYSIS_CONFIG`, `ANALYSIS_IDS`, `ANALYSIS_PYTHON` and submit
`slurm_region.sh` with `--array=0-292%8`. A line in the IDs file is one region.
The first trial uses the first, middle and last sorted completed IDs; selection does
not depend on output quality or model effect. Each job requests 4 CPUs, 32 GiB, 24 hours.
The CPU-only analysis requests no GPU devices. Keep code deployments immutable.

The MOD16 adapter also requires GDAL with HDF4 support and a working PROJ database
in the audited environment. GDAL is not installed implicitly from pip. Reference
preparation is a separate array stage; `slurm_finish.sh` only renders the report.

For component-parallel plotting and a concise main report, use
[Parallel report instructions](../docs/PARALLEL_REPORT_EN.md). This route reuses
the completed numerical/reference stages and validates existing figure reuse.
It creates composite component pages and independent group atlases, then merges
them only after every required worker has succeeded.

Do not overwrite `inventory.json` for an enlarged or changed source snapshot.
Create a versioned output root, freeze the new inventory, and reuse a region only
after its source, dependency and scientific-code fingerprints match. This prevents
newly completed regions from silently changing the population of an older report.

## Data contracts

* Source: `SINDBAD_RESULTS_ROOT`, the approved entity-output directory.
* Analysis root: `SINDBAD_ANALYSIS_ROOT`, a separate user-owned scratch directory.
* Private deployment configuration supplies absolute paths; it is never committed.
* Per region: `annual.csv`, `monthly.csv`, `spatial.npz`, `compact.nc`, profiles,
  existing-CSV recomputation, event-aligned paired changes, QA, fingerprints and logs.
* Reports: English Markdown/PDF plus independent PNG/PDF and source tables.
* Every one of the 13 saved variables / 50 components is registered; derived stocks and fire sums
  are additional, not counted as independent saved variables.

Annual fluxes are sums; annual stocks are means. The first saved and final stocks
are separately retained in `spatial.npz` for stock-change accounting. 2014 is
partial. Missing hours/days are never implicitly converted to shorter integrals.
`compact.nc` contains native-MODIS-composite GPP sums, complete-month flux inputs
and annual wood-carbon means for area-overlap reference comparisons.

Model grid data use explicit accepted-run masks. Water can have finite zeros.
Regional groups use unique owned original-2019 natural-vegetation area. Reference
comparisons use their own matched spatial support, not a misleading replication of
coarse pixels. Ownership arrays are `(lat,lon)`; saved simulation arrays are
`(lon,lat,[layer],time)`. Both dimensions and coordinates are checked.

## Scientific checks

The pipeline profiles NaN, Inf, negative values, zeros, constants and temporal jumps;
checks NEE/respiration identities, zero No_Fire diagnostics, no-fire-pixel agreement;
and evaluates carbon closure from the second saved day onward. The actual
four-digit quantization envelope is propagated for each stock and flux. A separate
0.01 g C/m²/day numerical model tolerance is declared, never tuned from outcomes.
Large jumps are profiled without automatically rejecting genuine fire disturbance.

`completion.json` means processing completed, not that every scientific check passed.
`qa.json` holds the independent numerical status. Failed regions retain their data
and evidence and are quarantined from primary validated comparisons. Reference
agreement is another stage; it does not certify a model as correct.

See [output semantics](../docs/OUTPUT_SEMANTICS_EN.md),
[grouping](../docs/GROUPING_EN.md), and [references](../docs/REFERENCES_EN.md).

## Interpretation safeguards

* NEE excludes direct fire emission. Mortality, transfer and emission are not additive.
* Restart is saved before age regrowth and is not the first forward inventory.
* fAPAR uses the actual PFT parameter map, not an obsolete default description.
* Native per-m² units are retained. Neither blanket vegetation-fraction scaling nor
  an unverified FLUXCOM land-fraction conversion is applied.
* VIIRS active fire scaled by 0.99 is a forcing proxy, not measured 99% burned area.
* Repeated positive fire days reapply the forcing; they are not necessarily new fires.
* Shared FLUXNET/MODIS lineage limits claims of independent reference validation.
* 2,000-replicate, seed-42 spatial-block/year bootstrap intervals do not include all model/product
  uncertainty. Carbon-factor sensitivity is not a confidence interval.
* Hypotheses from simulated paired experiments are not observational causal proof.

No source result or user model configuration is changed by this analysis.

## Email figures: all 293 regions, overall and grouped comparisons

After generating the all-293 findings tables, run:

```bash
python -m result_analysis.email_grouped_comparisons "$SINDBAD_ANALYSIS_ROOT"
```

This produces a two-page overview and eight detailed comparisons for GPP, cVeg,
ET and direct fire carbon emission, each shown overall and by pyrome or original
vegetation class. Both scenarios appear side by side; the detailed figures also
show paired differences. Outputs are under
`report_findings_v3_all293/email_section2_grouped_v2/`, with plotted values,
recomputed group statistics, source hashes, English email text and a ZIP package.

Each point is one region/group's 2015–2023 mean annual value. Fluxes use annual
totals and cVeg uses annual mean stocks. Distributions weight regions equally;
diamonds separately show owned-area-weighted means. Mixed regions contribute
only their corresponding class pixels to each group. No-Fire direct combustion
is exactly zero; negative net carbon contributions in the case studies describe
a different quantity. These are descriptive results for the completed cohort,
not design-weighted Mediterranean totals.

This explicitly requested all-293 view includes the 46 regions with near-zero
water/ET numerical flags, without altering their original QA or model outputs.
The figure builder reconciles the summaries, area partitions and paired values
against the prior all-293 tables before exporting the figures.
