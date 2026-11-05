# Sujan follow-up: references, pre-fire carbon and negative net contributions

The frozen 293-region cohort is used, including the documented near-zero water/ET
flags. No source model output or production controller is modified.

## Reproduce

```bash
python -m result_analysis.sujan_followup references "$ANALYSIS_ROOT"
python -m result_analysis.sujan_followup extract "$ANALYSIS_ROOT"
python -m result_analysis.sujan_followup merge "$ANALYSIS_ROOT"
python -m result_analysis.sujan_followup negative "$ANALYSIS_ROOT"
python -m result_analysis.sujan_followup_delivery "$ANALYSIS_ROOT"
```

Extraction can run as the 32-task `slurm_prefire.sh` array. Each task requests one
CPU and 4 GiB and processes disjoint region IDs. Checkpoints are per region and
source files are read by native longitude chunk. No full daily output copy is made.
Deployments supply `ANALYSIS_ROOT`, `ANALYSIS_CODE` and `ANALYSIS_PYTHON`.

Outputs are under `report_findings_v3_all293/sujan_followup_v1/`. The three-page PDF
contains reference comparisons, pre-fire stock by plant form and negative-net
decomposition. Model-PFT comparisons are a separate figure. `CSV_INDEX_EN.csv`
records the grain and interpretation of every data table.

## Reference comparison

Existing per-region reference pairs are pooled with stable, coverage-based
deduplication and identical two-scenario support. GPP comparisons use common
MODIS/FLUXCOM geographic cells and complete years 2015–2021. ET products have
their own valid supports. Annual region distributions and native reference-cell
scatter data are both exported. ET is not relabelled as LE. No MODIS NEE is
invented. Reference products are estimates, not ground truth.

## First recorded fire

For each owned, runnable natural-vegetation pixel with first positive VIIRS
forcing at day t > 0, extract all eight carbon pools at t−1 and pool-specific
combustion at t. The previous saved daily state is a temporal proxy for the
within-day pre-combustion state. It is neither restart nor initial-year stock.
The event is first within the saved simulation, not the pixel's first-ever fire.

The cohort yields 72,102 paired pixels in 277 cubes; six first-day pixels have no
previous saved stock. Group means use affected owned class area, not all cube
area or independently measured burned area. Original 2019 IGBP classes and
parameter-compatible model PFTs are exported separately. The fixed 2019 map does
not establish the pixel's actual land-cover class in every event year.

The stock–emission relationship partly follows directly from model mortality,
combustion and stock dependence. It is not independent empirical validation.
Stocks and emissions are paired by source pool: vegetation with vegetation,
litter with litter, and whole ecosystem with all eight emission components.

## Negative annual net effects

`net = direct_fire + ΔReco − ΔGPP`, where Δ is VIIRS_Fire minus No_Fire. Existing
2,637 region-years are rechecked; all direct emissions are nonnegative, while
353 net values are below −0.01 g C m−2 yr−1. This descriptive threshold is not a
significance test. Selected negative cases show respiration reductions larger
than direct combustion plus lost uptake. The arithmetic does not establish the
process cause or a cumulative benefit from fire.

All paired differences reconcile within the saved-output rounding envelope.
All 586 raw extraction source identities match the frozen inventory. Independent
scalar reads and original forcing-date checks were performed for two regions;
this is a sampled orientation/time check, not an independent reread of all values.
