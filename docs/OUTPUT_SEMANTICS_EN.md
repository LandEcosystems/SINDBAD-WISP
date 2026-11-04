# SINDBAD–WISP output interpretation

This guide applies to Sujan's **WISP_V1_202610 entity runs**, currently the
`No_Fire` and `VIIRS_Fire` scenarios, from 12 January 2014 to 31 December 2023
(3,641 daily states/fluxes). It describes the saved outputs, not the behaviour of
every SINDBAD configuration. The actual saved model code, `info.jld2`, parameter
map, NetCDF metadata and input masks take precedence over generic documentation.

## 1. What the scenarios mean

Both scenarios use the same weather, initialization, parameters and static
vegetation/tree fractions. The fire module is driven by

\[
f_{\rm burnt\_area}=s\,\mathrm{VIIRS}_{\rm binary},\qquad
s=0\;\text{(No_Fire)},\quad s=0.99\;\text{(VIIRS_Fire)}.
\]

VIIRS is an **active-fire detection proxy**. A value of 0.99 is a modelling
choice, not an observation that 99% of that grid cell burned. Repeated positive
days can disturb the same cell repeatedly. FRP does not scale this forcing in
the present configuration. `cVegetationDieOff` is disabled, so the saved fire
diagnostics do not include an additional non-fire die-off process.

The real `WISP_Fire` scenario has not been run here. In the current input cubes,
the WISP binary fields are copies used as placeholders; they are not predictions
and must not be evaluated as a third independent experiment.

## 2. Saved scientific outputs: 13 variables, 50 components

Native dimensions are `(lon, lat, time)` or `(lon, lat, layer, time)` in
Python/NetCDF. Coordinates, rather than array indices, determine orientation.
Carbon fluxes are nominally `g C m-2 day-1`, carbon pools `g C m-2`, water
fluxes `mm day-1` and water stores `mm`.

| File / variable | Components | Physical meaning | Valid temporal summary |
|---|---:|---|---|
| `gpp` | 1 | Gross photosynthetic carbon uptake | Daily rate; sum complete days for accumulated uptake |
| `auto_respiration` | 1 | Respiration by vegetation | Daily rate or accumulated release |
| `hetero_respiration` | 1 | Respiration associated with litter/soil decomposition | Daily rate or accumulated release |
| `eco_respiration` | 1 | Autotrophic plus heterotrophic respiration | Daily rate or accumulated release |
| `nee` | 1 | Ecosystem respiration minus GPP; negative means net uptake | Daily rate or accumulated exchange; **excludes direct fire efflux** |
| `evapotranspiration` | 1 | Total modelled evaporation, including transpiration, soil evaporation, interception and snow sublimation | Daily rate or accumulated water loss |
| `transpiration` | 1 | Vegetation transpiration | Daily rate or accumulated water loss |
| `runoff` | 1 | Total modelled runoff | Daily rate or accumulated water export |
| `cEco` | 8 | Ecosystem carbon stocks in the pools below | Mean, beginning/end, change or trend; never sum stocks and call the result emissions |
| `TWS` | 10 | Water stored in the layers below | Mean, beginning/end, change or trend |
| `c_fire_efflux` | 8 | Carbon combusted and emitted from each pool | Sum over pools and days for direct fire carbon emissions |
| `c_fire_flow` | 8 | Non-combusted killed carbon received by litter/soil pools | Internal transfer, **not another atmospheric emission** |
| `c_fire_mortality` | 8 | Vegetation carbon lost through combustion plus transfer | Mortality diagnostic; **do not add it to efflux and flow** |

All individual layer components remain in the analysis inventory, including
constant or identically zero layers. A zero layer is a result, not missing data.

### Carbon-pool indices

Indices below are the **one-based model indices**; Python array indices are one
less.

| Index | Name | Interpretation |
|---:|---|---|
| 1 | `cVegRoot` | Root carbon |
| 2 | `cVegWood` | Woody vegetation carbon |
| 3 | `cVegLeaf` | Leaf carbon |
| 4 | `cVegReserve` | Vegetation reserve carbon |
| 5 | `cLitFast` | Fast litter pool |
| 6 | `cLitSlow` | Slow litter pool |
| 7 | `cSoilSlow` | Slow soil-carbon pool |
| 8 | `cSoilOld` | Old soil-carbon pool |

Derived stocks are `cVeg = sum(cEco[1:4])`, `cLitter = sum(cEco[5:6])`,
`cSoil = sum(cEco[7:8])`, and `cEcosystem = sum(cEco[1:8])` using inclusive
one-based ranges. These aggregates supplement, rather than replace, all eight
saved components.

### Water-store indices

| Index | Name | Interpretation |
|---:|---|---|
| 1–7 | `soilW_1` … `soilW_7` | Soil-water storage from top to bottom |
| 8 | `groundW` | Groundwater storage |
| 9 | `snowW` | Snow water storage |
| 10 | `surfaceW` | Surface water storage |

Configured soil-layer thicknesses are 50, 200, 750, 1,000, 3,000, 5,000 and
40,000 mm. Actual pixel soil depth changes the active column: these configuration
values do not establish that every simulated pixel has 50 m of soil. `sum(TWS)`
is total stored water, not evapotranspiration.

## 3. Identities, budgets and numerical precision

\[
R_{\rm eco}=R_{\rm auto}+R_{\rm hetero},\qquad
NEE=R_{\rm eco}-GPP,\qquad
F_{\rm fire}=\sum_k c_{{\rm fire\,efflux},k}.
\]

For consecutive saved daily states, the carbon-budget residual is

\[
\epsilon_t=C_{\rm eco}(t)-C_{\rm eco}(t-1)+NEE(t)+F_{\rm fire}(t).
\]

The model uses Float32 and output rounding with four significant decimal digits
(implemented through retained binary precision). Each term is rounded separately.
The test propagates the rounding bounds of both large stocks and the small fluxes.
A fixed tiny absolute tolerance would incorrectly flag well-balanced rounded
outputs. The budget begins with the **first saved forward state** and uses the
subsequent days' fluxes.

`restart.nc` is not that state. In the audited age-spinup sequence, restart is
saved at `EtaScaleH`, before age-dependent regrowth and the forward run. Its date
attribute does not make it the actual first forward inventory. Using it as the
forward budget baseline can create a large artificial imbalance.

Converting fire carbon by `44/12` expresses it as the mass of CO2 if all emitted
carbon were assigned to CO2. The present output does not independently resolve
the atmospheric partition into CO2, CO and CH4.

## 4. Area, masks and grouping

Finite output is not sufficient to identify land: some water cells contain finite
zeros. Use the recorded run mask first. Preserve reasons for every excluded cell;
do not silently replace failed values or shrink the support of one scenario.
Paired scenario analysis uses common valid support.

The output unit labels are per square metre. Some internal GPP/fAPAR calculations
refer to vegetated-area quantities, while saving applies no blanket vegetation
fraction correction. Therefore retain native values and explicitly audit the
area convention before interpreting reference-area totals. Do **not** multiply
or divide all variables by vegetation fraction merely because the field exists.

Use the unique statistical-ownership areas when cubes overlap. For scientific
grouping, use the original 2019 IGBP map and original pyrome geography; use the
FLUXNET-compatible model PFT only for parameter diagnostics. A mixed cube is not
assigned wholly to its dominant pyrome. The primary vegetation groups are forest
(IGBP 1–5), shrubland (6–7), savanna (8–9), grassland (10) and wetland (11).

The 293 completed regions are a non-random completion subset of the planned 400.
Their results are interim, descriptive results for analysed support, not an
unbiased Mediterranean total. In particular, applying the full-design inclusion
weights to only the completed subset does not fix completion bias.

## 5. LAI, fAPAR and biomass are different quantities

LAI is not saved and its model module is not enabled in this experiment. It cannot
be uniquely recovered from the current outputs. The active fAPAR formulation is

\[
fAPAR=1-\exp(-k_{\rm extinction} C_{\rm leaf}),\qquad
fAPAR_{\rm grid}=fAPAR\,f_{\rm vegetation}.
\]

It uses a leaf-carbon state at a specific point in the model update, which is not
necessarily the saved end-of-day leaf state. This equation is not an LAI
observation or a unique SLA-based inversion. The actual PFT parameter map
contains optimized `fAPAR__k_extinction` values; the old README statement that
this parameter always retains its default is stale. The model also applies a
0.99 multiplier to the vegetation-fraction forcing in the audited run.

ESA CCI v6 AGB is aboveground **woody dry biomass**, excluding roots and foliage.
The primary matching model component is woody carbon within original forest
pixels, not total cVeg. Convert the reference as

\[
C_{\rm woody,reference}=AGB\,[Mg\,ha^{-1}]\times100\times0.47
\quad[gC\,m^{-2}].
\]

The 0.51 alternative is a carbon-fraction sensitivity, not a confidence interval.
Do not multiply CCI biomass by tree fraction a second time. The model's own AGB
diagnostic includes different pools and must be labelled separately.

## 6. Meaning of existing PNG, CSV and serialized outputs

| Existing artifact | Meaning and caution |
|---|---|
| `<group>_<variable>[_layer]_<tag>.png` | Time-mean map; original plots use array indices, not a projected geographic map |
| `*_timeseries_<tag>.png` | Spatial means at daily resolution, plus monthly and annual **means**; these are not accumulated emissions |
| `forc_*` figures | Forcing summaries; dynamic fields are usually time means, static fields maps and time-only fields series |
| `*_mean_comparison_*` | Diagonal: scenario mean maps. Upper panels: column minus row. Lower panels: corresponding difference histograms |
| `*_timeseries_comparison_*` | Scenario spatial-mean time series; differences need their displayed sign convention |
| `comparison_metrics_*.csv` | Scenario comparisons, not model-versus-observation validation. Bias is `other − reference`; spatial metrics use time-mean maps, temporal metrics use daily spatial means |
| `r2` column | Pearson `r` squared, not NSE and not predictive `1 − SSE/SST` |
| `summary_*.jls` | Julia-serialized aggregated maps/series, potentially made before output rounding; not raw pixel-day simulations |
| `settings/*.json` | Saved templates; some runtime overrides are only represented in `info.jld2` |
| `settings/info.jld2` | Actual run information, including overrides and parameter-map identity |
| `code/*.jl` | Saved model equations and definitions used for that run |
| `done/*` | Job-stage completion marker; it does not itself prove every figure was produced or every physical check passed |

Original means are not ownership-area/design-weighted regional totals. A missing
correlation for a constant series is expected and should remain undefined.
Optimization-site/PFT figures concern the calibration stage, not independent
regional EO validation.

## 7. New figure families and their interpretation

Every figure links to its source summary or paired table and identifies units,
time support, grouping, region count and area/mask convention.

| New figure family | Observation represented | What can be concluded |
|---|---|---|
| Scenario scatter / density hexbin | Paired region-year or explicitly labelled pixel values | Scenario agreement and departures from the 1:1 line; no observational validation |
| Reference scatter / density hexbin | A reference-resolution cell and matching time interval | Agreement with that product on common support, not truth at 375 m |
| Reference-only model-grid atlas | Archived reference means copied to model centres, alongside original source-cell indices | Display only; the source grid remains coarse and no new fine-resolution pairs or metrics are created |
| Violin + box + region points | Region-year values or paired differences | Heterogeneity among analysed regions; not an independent-pixel confidence interval |
| Geographic value/difference map | Real coordinates and their analysed support | Where responses occur; interpolation does not add evidence |
| Seasonal / annual series | Area-aware means or complete-period flux integrals, explicitly labelled | Seasonal structure and interannual change; 2014 is incomplete |
| Annual anomaly heatmap | Defined anomalies relative to a specified analysed baseline | Relative anomalies, not direct emissions or trends by themselves |
| Group effect + interval | Paired response by original pyrome/vegetation group | Between-group model responses; uncertainty method must be stated |
| Fire-aligned recovery | Response relative to first fire, separating repeated fire | Modelled recovery pattern, not an identified real-world causal effect |

Zero or constant components are shown as point masses and zero proportions;
they do not receive a fabricated smooth density or correlation. Overall, pyrome,
vegetation, original IGBP and model-PFT outputs are kept separate, with additional
combined panels. Reference products share some training/remote-sensing lineage
with SINDBAD, so agreement is not fully independent validation.

The reference-only atlas uses the first three accepted regions in stable ID
order. Each map states the actual archived period and source comparison-grid
resolution; repeated scenario-identical reference maps are shown once. Actual
coarse source-cell IDs remain in a companion CSV. The atlas includes available
finite reference values even when formal coverage thresholds reject a pair,
and is kept separate from the 90% coverage-gated comparison metrics. It does
not compare partial-period reference means with unmatched ten-year model means.

Exploratory response tests use an approximate centered two-way bootstrap of
2-degree spatial blocks and complete years (2,000 resamples, seed 42). A
two-sided p-value is reported only with at least five blocks, three years and
nonconstant bootstrap draws. Benjamini–Hochberg q-values are computed together
for valid tests across the seven predefined response variables and all overall,
pyrome and vegetation groups; nonsignificant and undefined tests are retained.
These are exploratory model-response tests, not observational causal evidence.
Percentile intervals are not multiplicity-adjusted. Environmental relationship
plots remain descriptive, without pixel-independent significance tests.

Recovery plots retain all detected-fire pixels, repeated-positive-day pixels and
single-positive-day pixels as separate curves. Their displayed pixel counts use
positive uniquely owned area, runnable status and valid lag support. Original
upstream runnable-pixel counts are retained separately for audit.

## 8. Reference and visual-method sources

- [SINDBAD–WISP source repository](https://github.com/LandEcosystems/SINDBAD-WISP),
  particularly the saved run code and the local audited output guide.
- [MOD17A2HGF C6.1](https://doi.org/10.5067/MODIS/MOD17A2HGF.061): 8-day accumulated
  GPP. The local 0.05-degree product is already scaled; annual terminal composites
  contain 5 or 6 days, not always 8.
- [FLUXCOM-X product collection](https://www.icos-cp.eu/data-products/fluxcom-x-global-fluxes-collection)
  and [Nelson et al., 2024](https://bg.copernicus.org/articles/21/5079/2024/):
  reference flux estimates, common land support and product uncertainty.
- [ESA CCI Biomass v6 product guide](https://climate.esa.int/media/documents/D4.3_CCI_PUG_V6.0_20250606.pdf):
  woody biomass definition and product support.
- [Archibald et al., 2013](https://doi.org/10.1073/pnas.1211466110): published pyromes;
  unknown classifications remain explicit.
- [Carbon emissions from the 2023 Canadian wildfires](https://www.nature.com/articles/s41586-024-07878-z):
  useful map/time-series and common-support comparison design.
- [Global patterns and drivers of tropical aboveground carbon changes](https://www.nature.com/articles/s41558-024-02115-x):
  useful separation of carbon changes, geography and explanatory conditions.

These papers inform figure selection and interpretation. Their findings are not
substitutes for results from the present experiments.
