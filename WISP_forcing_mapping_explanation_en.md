# Mapping SINDBAD Forcing Variables to WISP Inputs: Complete Assessment

## 1. Purpose of This Document

This document explains why every SINDBAD forcing variable in [WISP_variable_table.md](WISP_variable_table.md) is labeled `Yes`, `Approximate`, or `No`. The question addressed here is: **Does the physical quantity required by SINDBAD exist in WISP's current official inputs, and if so, to what degree?**

This assessment does not automatically treat similar names as identical variables, nor does it treat a variable that indirectly reflects a process as an unconditional substitute. The classification considers all of the following:

1. Whether the physical definitions agree;
2. Whether the current WISP model actually uses the channel;
3. Whether the quantity is a direct input, a proxy, or requires additional derivation;
4. Whether units, accumulation conventions, and temporal scales are compatible;
5. Whether data sources, spatial resolutions, and preprocessing introduce important semantic differences.

## 2. Classification Labels

### 2.1 `Yes`

WISP's current 55-channel input schema contains a variable with a directly corresponding physical meaning. One example is SINDBAD air temperature `f_airT` and WISP ERA5 2 m air temperature `t2m`.

`Yes` **does not mean that values can be copied without conversion**. The data source, temporal resolution, spatial grid, units, accumulation convention, and normalization may still differ. These differences must be resolved before exchanging data.

### 2.2 `Approximate`

At least one of the following conditions applies:

- WISP contains only a related proxy rather than the same physical quantity;
- WISP has no independent channel, but the quantity can be derived from existing channels;
- The derivation requires an additional definition, such as a daylight mask, radiation sign convention, or temporal aggregation window;
- Even after derivation, the result cannot be guaranteed to be scientifically identical to the SINDBAD forcing.

An `Approximate` variable is not a drop-in replacement unless its method and limitations are explicitly documented and validated.

### 2.3 `No`

The physical quantity is absent from WISP's current official 55-channel schema, and no sufficiently direct and defensible substitute exists. A related WISP variable is still classified as `No` if it cannot recover the target quantity.

## 3. Assessment Scope and Evidence

### 3.1 SINDBAD Side

This document compares two WROASTED forcing configurations:

- Spatial setup: `dev/Sindbad.jl/examples/exp_WROASTED/settings_WROASTED/forcing_zarr.json`
- Hourly setup: `dev/Sindbad.jl/examples/exp_WROASTED/settings_WROASTED/forcing_hourly.json`

The spatial configuration contains 19 forcing variables. The hourly configuration contains 15. The original variable table omitted `f_airT_day` and `f_VPD_day` from the hourly section; both have now been added.

### 3.2 WISP Side

The current WISP training code hard-codes `FEATURE_NAMES_55` in `../WildfireIgnitionPred/src/utility.py`. This assessment counts only channels in that official schema as being present in WISP. Names that occur only in old scripts, comments, experimental notebooks, or artifacts outside the current training inputs are not considered affirmative evidence.

The WISP channels directly relevant to this mapping are:

| WISP channel | Meaning in WISP | Main source/processing |
|---|---|---|
| `t2m` | 2 m air temperature | ERA5; converted from K to °C before training |
| `d2m` | 2 m dew-point temperature | ERA5; used to calculate `vpd` |
| `vpd` | Vapor pressure deficit | Calculated from `t2m` and `d2m`, in kPa |
| `tp` | Total precipitation | ERA5 accumulated quantity; temporal differencing recovers interval increments |
| `ssrd` | Surface solar radiation downwards | ERA5 accumulated radiation; differenced and divided by 3600 to approximate W m⁻² |
| `ssr` | Surface net solar radiation | ERA5 accumulated net solar radiation; not complete all-wave net radiation |
| `avg_snswrf` | Mean surface net shortwave radiation flux | ERA5 mean flux |
| `avg_snlwrf` | Mean surface net longwave radiation flux | ERA5 mean flux |
| `fcover` | Fraction of vegetation cover | CLMS FCOVER; clipped to [0, 1] |
| `active_fire` | VIIRS active-fire confidence code | `-1` unobserved, `0` no fire, `1/2/3` low/nominal/high-confidence fire detection |
| `frp` | Fire Radiative Power | FRP associated with a VIIRS active-fire detection; not an area fraction |
| `swvl1`–`swvl4` | Volumetric soil water in four soil layers | ERA5 soil water state; not soil texture |

WISP loads ERA5 surface data hourly. The native ERA5 grid is cropped and regridded to the approximately 375 m VIIRS grid. CLMS `fcover` is a lower-frequency product that is forward-filled onto WISP's hourly time axis. Therefore, even a `Yes` label does not imply that WISP and SINDBAD use the same source product, native resolution, or temporal interpolation.

## 4. Complete Crosswalk

| SINDBAD forcing | Spatial source | Hourly source | Status | WISP counterpart |
|---|---|---|---|---|
| `f_ambient_CO2` | `atmCO2_SCRIPPS_global` | `CO2_GF` | `No` | none |
| `f_clay` | `CLYPPT_SoilGrids` | `CLYPPT_SoilGrids` | `No` | none |
| `f_dist_intensity` | `dist_frac_sb2018` | `dist_frac_sb2018` | `No` | none |
| `f_burnt_area` | `fire_frac` | not configured | `Approximate` | `active_fire`, `frp` |
| `f_tree_frac` | `tree_frac` | not configured | `Approximate` | `fcover` |
| `f_frac_vegetation` | `veg_frac` | not configured | `Yes` | `fcover` |
| `f_pft` | `f_pft` | not configured | `No` | none |
| `f_orgm` | `OCSTHA_SoilGrids` | `OCSTHA_SoilGrids` | `No` | none |
| `f_PAR` | `SW_IN_ERAIv2_gfld` | `SW_IN_GF` | `Approximate` | derived from `ssrd` |
| `f_rain` | `P_ERAIv2_gfld` | `P_GF` | `Yes` | `tp` |
| `f_rg` | `SW_IN_ERAIv2_gfld` | `SW_IN_GF` | `Yes` | `ssrd` |
| `f_rg_pot` | `SW_IN_POT_ONEFlux` | `SW_IN_POT_ONEFlux` | `No` | none |
| `f_rn` | `NETRAD_ERAIv2_gfld` | `SW_IN_POT_ONEFlux` | `Approximate` | derived from `avg_snswrf`, `avg_snlwrf` |
| `f_sand` | `SNDPPT_SoilGrids` | `SNDPPT_SoilGrids` | `No` | none |
| `f_silt` | `SLTPPT_SoilGrids` | `SLTPPT_SoilGrids` | `No` | none |
| `f_airT` | `TA_ERAIv2_gfld` | `TA_GF` | `Yes` | `t2m` |
| `f_airT_day` | `TA_DayTime_ERAIv2_gfld` | `TA_GF` | `Approximate` | daytime aggregation of `t2m` |
| `f_VPD` | `VPD_ERAIv2_gfld` | `VPD_GF` | `Yes` | `vpd` |
| `f_VPD_day` | `VPD_DayTime_ERAIv2_gfld` | `VPD_GF` | `Approximate` | daytime aggregation of `vpd` |

## 5. Detailed Variable-by-Variable Explanations

### 5.1 `f_ambient_CO2` — `No`

**SINDBAD definition.** Ambient atmospheric CO₂ concentration, bounded between 200 and 500 ppm. The spatial setup uses `atmCO2_SCRIPPS_global`; the hourly setup uses `CO2_GF`. Both represent atmospheric CO₂ concentration used to force vegetation physiology.

**WISP assessment.** `FEATURE_NAMES_55` contains no CO₂, atmospheric-composition, carbon-concentration, or other channel from which atmospheric CO₂ concentration can be recovered.

**Why no proxy is assigned.** Vegetation-state variables such as `gdmp`, `lai`, and `fapar` can be affected by CO₂, but they are also controlled by climate, nutrients, vegetation type, and management. They cannot be used to infer ambient CO₂ in ppm. Treating vegetation productivity as CO₂ would confuse a driver with a response.

**Data-exchange requirement.** A new forcing must be obtained from Scripps, an atmospheric composition product, station observations, or another independent CO₂ data source and converted to ppm. Current WISP inputs cannot provide it.

### 5.2 `f_clay` — `No`

**SINDBAD definition.** Soil clay fraction from SoilGrids `CLYPPT_SoilGrids`. The original unit is percent and `source_to_sindbad_unit = 0.01` converts it to a fraction. The `spatiovertical` type indicates a soil-depth dimension.

**WISP assessment.** WISP contains four soil-water channels, `swvl1`–`swvl4`, but no clay fraction or soil-texture class.

**Why soil water is not clay.** Clay fraction is a comparatively stable soil-texture property. Soil water is a state variable that changes with precipitation, evapotranspiration, and drainage. Clay affects the water-retention curve, but clay fraction cannot be uniquely inferred from soil-water values at one time or even from a limited time series. A similar four-layer structure does not make the physical quantities equivalent.

**Data-exchange requirement.** SoilGrids clay data must be loaded separately, with soil-depth layers, percent/fraction units, and target grids made consistent.

### 5.3 `f_dist_intensity` — `No`

**SINDBAD definition.** A disturbance fraction/flag from Besnard2018 `dist_frac_sb2018`, approximately bounded between 0 and 1 and configured as categorical. It indicates whether, or to what degree, an ecosystem is disturbed.

**WISP assessment.** WISP has no channel for disturbance type, disturbed fraction, harvest, storm damage, or land-cover transition.

**Why related variables are not substitutes.** `population_density` is only a broad proxy for human pressure; `active_fire` records fire detections; decreases in `fcover` or `lai` may result from fire, drought, harvest, seasonality, or missing data. None recover the definition of the Besnard2018 disturbance flag.

**Data-exchange requirement.** A disturbance dataset consistent with the SINDBAD definition must be introduced. Alternatively, a separately validated WISP-specific disturbance forcing could be defined, but it must not be claimed to be equivalent to the original variable.

### 5.4 `f_burnt_area` — `Approximate`

**SINDBAD definition.** `fire_frac` represents the burned/fire area fraction within a pixel and is bounded between 0 and 1. It is an area fraction.

**WISP candidates.** `active_fire` records the confidence level of a VIIRS active-fire detection; `frp` records the corresponding Fire Radiative Power. WISP also constructs a union target from strong fires detected over the future 24 hours, but this remains a binary indicator of whether fire was detected during the period, not a burned-area fraction product.

**Why the mapping is only `Approximate`.** Active-fire detection is an instantaneous or satellite-overpass thermal-anomaly observation. Burned area is the spatial extent of post-fire surface change. Fire missed between overpasses, cloud cover, subpixel fires, and repeated observations of the same location all cause the two concepts to differ. FRP is power rather than area, and a confidence code is not an area fraction.

**Possible proxy method.** The fraction of WISP subpixels with strong-fire detections inside a coarser target pixel and specified time window could be used as a proxy. Such a method must define:

- Which confidence classes are included;
- How `-1` unobserved values are handled;
- How the temporal window and repeat detections are aggregated;
- How the VIIRS 375 m detection footprint relates to target-pixel area;
- That the result is not a true burned fraction unless calibrated against a burned-area product.

**More reliable approach.** Introduce MCD64A1, FireCCI, or another formal burned-area product and align it with SINDBAD's time window and grid definition.

### 5.5 `f_tree_frac` — `Approximate`

**SINDBAD definition.** `tree_frac` is tree-cover fraction, bounded between 0 and 1. It requires trees to be distinguished from grasses, shrubs, crops, and other vegetation.

**WISP candidate.** `fcover` comes from CLMS FCOVER and represents total green vegetation-cover fraction. WISP also contains `lai`, `fapar`, and `gdmp`, which represent leaf area, absorbed photosynthetically active radiation fraction, and productivity, respectively, rather than tree fraction.

**Why the mapping is only `Approximate`.** Grassland and sparse woodland can have similar `fcover` while having very different tree fractions. WISP has no land-cover or PFT channel that can separate `fcover` into tree, grass, shrub, or crop fractions.

**Data-exchange requirement.** If only a vegetation-cover proxy is needed, `fcover` may be used while retaining the `Approximate` designation. If a SINDBAD component genuinely depends on woody/tree fraction, an independent tree-cover or land-cover dataset is required.

### 5.6 `f_frac_vegetation` — `Yes`

**SINDBAD definition.** The source variable is `veg_frac`, bounded between 0 and 1, and is intended to represent vegetation-covered fraction. Its configured `standard_name` is `tree fraction`, which conflicts with both the forcing key and source-variable name. This assessment follows the semantics of `f_frac_vegetation` and `veg_frac`.

**WISP counterpart.** `fcover` comes from CLMS FCOVER, also represents vegetation-cover fraction, and is clipped to [0, 1] during WISP preprocessing.

**Why the status is `Yes`.** Both quantities represent the fraction of a pixel covered by vegetation. WISP contains a directly corresponding channel, so no indirect inference from other variables is required.

**Remaining differences.** `Yes` does not guarantee identical vegetation definitions, observation dates, quality control, native resolution, or gap filling. WISP forward-fills lower-frequency CLMS data onto an hourly axis, while the SINDBAD spatial configuration labels the variable `spatiovertical`. Product definition, dimensions, time alignment, and regridding must therefore be verified before exchange.

### 5.7 `f_pft` — `No`

**SINDBAD definition.** A Plant Functional Type index with categorical values 1–17.

**WISP assessment.** WISP contains continuous vegetation-state channels such as `lai`, `fapar`, `fcover`, and `gdmp`, but no PFT index or land-cover class.

**Why vegetation state cannot recover PFT.** Multiple PFTs may have similar LAI, FAPAR, or FCOVER, and the same PFT can vary substantially with season and stress. The mapping from continuous state variables to discrete PFT is not one-to-one.

**Data-exchange requirement.** A PFT product consistent with SINDBAD's 17-class definition must be added. If another land-cover taxonomy is used, an explicit crosswalk and rules for unmappable classes are required.

### 5.8 `f_orgm` — `No`

**SINDBAD definition.** Source variable `OCSTHA_SoilGrids` points to a SoilGrids organic-carbon or organic-matter soil property. The configured range is 0–100 and the variable has a soil-depth dimension.

**Configuration warning.** Its `standard_name` is `CLAY`, which conflicts with `f_orgm` and `OCSTHA`. Its `source_to_sindbad_unit` is also set to `0.0`. If this factor is used as a multiplicative conversion, all input values may become zero; the SINDBAD configuration maintainer should confirm this setting.

**WISP assessment.** WISP has no soil organic carbon, organic matter, or carbon-stock channel. `gdmp` is vegetation productivity rather than soil carbon, and `swvl` is soil water.

**Data-exchange requirement.** SoilGrids organic-carbon or stock data must be read separately. The intended SINDBAD quantity—concentration, content, or stock—and the correct conversion factor must first be clarified.

### 5.9 `f_PAR` — `Approximate`

**SINDBAD definition.** Photosynthetically Active Radiation. The spatial configuration explicitly uses `PAR = Rg × 0.5`, sourced from `SW_IN_ERAIv2_gfld` in MJ m⁻² d⁻¹. The hourly configuration uses `SW_IN_GF` and a conversion factor of 0.0018, equivalent to multiplying shortwave radiation by 0.5 and converting W m⁻² to MJ m⁻² h⁻¹.

**WISP candidate.** `ssrd` is ERA5 surface solar radiation downwards. WISP has no independent PAR channel.

**Why the mapping is `Approximate`.** Following SINDBAD's own assumption, PAR can be estimated as `0.5 × ssrd`. However, 0.5 is an empirical conversion from broadband shortwave radiation to PAR rather than a direct spectral observation. The ratio can vary with solar angle, atmospheric conditions, and cloud cover.

**Effect of WISP preprocessing.** ERA5 `ssrd` is typically accumulated energy in J m⁻². WISP applies temporal differencing and divides by 3600 to approximate W m⁻². Therefore:

- To produce hourly PAR from raw WISP/ERA5 cubes, the accumulated quantity must be differenced correctly, multiplied by 0.5, and converted to the required energy unit;
- If a transformed WISP model input is used, verify whether scaling, `zscore`, or another transform has already been applied; normalized values cannot be treated directly as physical radiation;
- Daily PAR must be obtained by summing hourly energy, not by simply averaging W m⁻² values.

### 5.10 `f_rain` — `Yes`

**SINDBAD definition.** The spatial setup uses daily precipitation `P_ERAIv2_gfld` in mm d⁻¹; the hourly setup uses `P_GF` in mm h⁻¹.

**WISP counterpart.** `tp` is ERA5 total precipitation and is an official WISP surface-weather channel.

**Why the status is `Yes`.** WISP contains a direct precipitation-amount channel that physically corresponds to the SINDBAD precipitation/rain forcing.

**Important limitation.** ERA5 `tp` is total precipitation and may include both liquid and solid precipitation, while the SINDBAD field is named rain. If snow occurs in the study region and SINDBAD strictly requires liquid rain, phase separation is needed; all `tp` must not be treated unconditionally as rain.

**Units and accumulation.** WISP applies `diff_time` to accumulated `tp` to recover interval increments and may subsequently apply `log1p` and standardization. Exchange must use values in physical space:

- Hourly values require recovery of each hourly increment and conversion to mm h⁻¹;
- Daily values require summing hourly increments and conversion to mm d⁻¹;
- Accumulation resets and negative differences must follow ERA5 accumulation conventions;
- `log1p`- or z-score-transformed model inputs must not be passed directly to SINDBAD.

### 5.11 `f_rg` — `Yes`

**SINDBAD definition.** Global radiation. Its source variable is incoming shortwave radiation: `SW_IN_ERAIv2_gfld` for the spatial setup and `SW_IN_GF` for the hourly setup.

**WISP counterpart.** `ssrd` is surface solar radiation downwards, meaning incoming shortwave solar radiation at the surface.

**Why the status is `Yes`.** The core physical definitions directly correspond: both are surface incoming/downward shortwave radiation.

**Units and temporal processing.** SINDBAD requires MJ m⁻² per day or the corresponding hourly energy. WISP transforms accumulated `ssrd` in J m⁻² by differencing and dividing by 3600 to approximate W m⁻². The flux must be integrated or summed over the target time step; an instantaneous or mean flux cannot simply be labeled daily energy.

### 5.12 `f_rg_pot` — `No`

**SINDBAD definition.** Potential Global Radiation from `SW_IN_POT_ONEFlux`. This represents available global radiation under a potential/reference condition, rather than actual radiation affected by current clouds and atmospheric conditions.

**WISP assessment.** WISP contains actual ERA5 `ssrd`, but no `SW_IN_POT`, clear-sky radiation, or potential-radiation channel.

**Why `ssrd` is not a direct substitute.** Actual incoming radiation includes cloud, aerosol, and weather effects. Potential radiation is generally defined from solar geometry and a reference atmosphere. The two can differ substantially on cloudy days. Replacing potential radiation with actual `ssrd` would change the meaning of radiation-stress or cloud-related ratios such as `Rg/Rg_pot`.

**Data-exchange requirement.** ONEFlux potential radiation must be introduced, or the quantity must be calculated from latitude, longitude, date, solar geometry, and an explicit clear-sky assumption. The current WISP 55-channel inputs cannot provide it by themselves.

### 5.13 `f_rn` — `Approximate`

**SINDBAD definition.** Net radiation. The spatial source is `NETRAD_ERAIv2_gfld` in MJ m⁻² d⁻¹. Complete surface net radiation normally includes both net shortwave and net longwave components.

**WISP candidates.** WISP contains:

- `ssr`: surface net solar radiation, which is net shortwave only;
- `avg_snswrf`: mean surface net shortwave radiation flux;
- `avg_snlwrf`: mean surface net longwave radiation flux.

**Why the status is `Approximate`.** `ssr` alone lacks the longwave component, while `ssrd` is only incoming shortwave. Neither independently represents complete net radiation. In principle, net radiation can be estimated as:

```text
Rn ≈ avg_snswrf + avg_snlwrf
```

The sign direction, averaging period, timestamp semantics, and units of both source products must first be verified. WISP code comments describe these as mean fluxes, and the files used may be monthly means. They may therefore not be equivalent to synchronized hourly or daily net radiation.

**Hourly configuration anomaly.** In `forcing_hourly.json`, `f_rn.standard_name` is `Net radiation`, but its source variable is `SW_IN_POT_ONEFlux`, which is potential incoming shortwave radiation. These physical definitions conflict. This document preserves the configuration and does not assume which field is correct; the discrepancy must be resolved before use.

**Data-exchange requirement.** After validating the metadata and sign conventions, sum the WISP net-radiation components and integrate W m⁻² to MJ m⁻² over the target interval. If strict hourly `Rn` is required, synchronized ERA5 shortwave and longwave components are preferable to a monthly-mean proxy.

### 5.14 `f_sand` — `No`

**SINDBAD definition.** Soil sand fraction from `SNDPPT_SoilGrids`. Percent is multiplied by 0.01 to obtain a fraction, and the variable has a soil-depth dimension.

**WISP assessment.** WISP has no sand fraction. `swvl1`–`swvl4` are soil-water states, while `elevation`, `slope`, `hand`, and `geomorphon_*` cannot identify particle-size composition.

**Data-exchange requirement.** SoilGrids sand fraction must be added and aligned by soil layer, grid, and unit.

### 5.15 `f_silt` — `No`

**SINDBAD definition.** Source variable `SLTPPT_SoilGrids` represents soil silt fraction. Percent is multiplied by 0.01 to obtain a fraction, and the variable has a soil-depth dimension.

**Configuration warning.** Its `standard_name` is `CLAY`, which conflicts with `f_silt` and `SLTPPT`. This is a likely metadata error, but the configuration is not changed here.

**WISP assessment and limitation.** WISP has no silt fraction. Soil water and terrain cannot uniquely recover silt content.

**Data-exchange requirement.** SoilGrids silt fraction must be added, and the units and sum rule for clay+sand+silt must be verified.

### 5.16 `f_airT` — `Yes`

**SINDBAD definition.** Near-surface air temperature. The spatial setup uses `TA_ERAIv2_gfld`, the hourly setup uses `TA_GF`, and SINDBAD expects °C.

**WISP counterpart.** `t2m` is ERA5 2 m air temperature and an official WISP weather channel.

**Why the status is `Yes`.** Both quantities represent near-surface air temperature at approximately 2 m or station-screen height and directly correspond physically.

**Conversion requirement.** Raw ERA5 `t2m` is normally in K and WISP preprocessing converts it to °C. Data must be exported in physical space before normalization; a standardized model tensor requires inverse transformation. Spatial interpolation and differences between station and grid elevation should also be documented.

### 5.17 `f_airT_day` — `Approximate`

**SINDBAD definition.** Daytime air temperature. The spatial setup directly uses `TA_DayTime_ERAIv2_gfld`. The hourly setup still reads `TA_GF`, but exposes it through a separate forcing key, `f_airT_day`.

**WISP candidate.** WISP contains hourly `t2m`, but no independent `airT_day` channel.

**Why the status is `Approximate`.** A daytime statistic can be calculated from hourly `t2m`, but the result depends on rules not defined in the WISP schema:

- Whether daytime is a fixed local-hour interval or solar elevation greater than zero;
- Whether time is interpreted as UTC, local standard time, or true solar time;
- Whether the statistic is daytime mean, maximum, integral, or something else;
- How polar day, polar night, and missing hours are handled.

Until these rules exactly match the definition of the SINDBAD `TA_DayTime` product, the quantity is only an approximate derivation.

**Data-exchange requirement.** Read physical `t2m` in °C, create an explicit daylight mask from location and time, aggregate with the same statistic as SINDBAD, and record time-zone and missing-data rules.

### 5.18 `f_VPD` — `Yes`

**SINDBAD definition.** Vapor Pressure Deficit in kPa. The spatial source is `VPD_ERAIv2_gfld`; the hourly source is `VPD_GF`.

**WISP counterpart.** `vpd` is an official WISP weather channel. WISP does not read it directly from a file; it calculates VPD from `t2m` and `d2m`:

```text
T  = t2m - 273.15
Td = d2m - 273.15
es = 0.6108 × exp(17.27 × T  / (T  + 237.3))
ea = 0.6108 × exp(17.27 × Td / (Td + 237.3))
vpd = max(es - ea, 0)
```

The resulting unit is kPa.

**Why the status is `Yes`.** Although WISP's VPD is derived, it is already an independent `vpd` channel in the official 55-channel schema and physically corresponds to SINDBAD VPD.

**Conversion requirement.** WISP may subsequently apply `log1p` and standardization to `vpd`, so exchange should use pre-transform physical kPa values or correctly inverse-transformed values. SINDBAD has a lower bound of 0.01 kPa, whereas WISP's calculation permits zero; the alignment procedure must explicitly define whether values are clipped to 0.01.

### 5.19 `f_VPD_day` — `Approximate`

**SINDBAD definition.** Daytime Vapor Pressure Deficit. The spatial setup uses `VPD_DayTime_ERAIv2_gfld`; the hourly setup supplies this forcing from `VPD_GF`.

**WISP candidate.** WISP contains hourly `vpd`, but no independent `vpd_day` channel.

**Why the status is `Approximate`.** As with `f_airT_day`, a daylight mask and aggregation statistic must be defined. A simple daily mean includes nighttime and is not automatically equivalent to daytime VPD. Calculating VPD from daily-mean temperature and dew point would also differ from averaging hourly daytime VPD because saturation vapor pressure is nonlinear.

**Data-exchange requirement.** Calculate physical VPD in kPa for every hour from `t2m` and `d2m`, then apply a daylight mask and statistic consistent with SINDBAD. Do not average temperature first and calculate VPD afterward. Finally, apply the 0.01 kPa lower bound if required.

## 6. Key Differences Between the Spatial and Hourly Configurations

### 6.1 Variable Sets

The spatial setup contains 19 forcing variables and additionally includes the fire, vegetation, and PFT variables `f_burnt_area`, `f_tree_frac`, `f_frac_vegetation`, and `f_pft`. The hourly setup omits those four and therefore contains 15 variables.

### 6.2 Source Variables

The spatial setup mainly uses ERAIv2/global product names such as `TA_ERAIv2_gfld`, `P_ERAIv2_gfld`, and `SW_IN_ERAIv2_gfld`. The hourly setup mainly uses station/FLUXNET-style `*_GF` variables such as `TA_GF`, `P_GF`, `SW_IN_GF`, and `VPD_GF`.

WISP's candidate weather channels come from its own ERA5 preprocessing pipeline. Physical equivalence does not imply identical quality control, gap filling, or spatial representativeness across the three sources.

### 6.3 Temporal Scale and Units

- Spatial `f_rain`: mm d⁻¹; hourly `f_rain`: mm h⁻¹.
- Spatial radiation: MJ m⁻² d⁻¹; hourly sources are often W m⁻² and use a conversion factor to obtain interval energy.
- WISP `tp`, `ssr`, and `ssrd` may be accumulated quantities in the original ERA5 files; preprocessing uses `diff_time` to recover interval values.
- Daily amounts must be accumulated from hourly increments. Mean flux and accumulated energy must be converted explicitly using interval duration.

### 6.4 Spatial Representation

The SINDBAD spatial setup uses site as its space dimension and includes several `spatiovertical` soil or vegetation properties. WISP uses regional cubes in which ERA5, CLMS, and VIIRS data are aligned to an approximately 375 m grid. Exchange requires site extraction or grid aggregation, not merely renaming variables.

## 7. Configuration Metadata Anomalies and Items Requiring Confirmation

The following issues come from the existing SINDBAD configuration. They are documented here without modifying the source configuration:

1. `f_frac_vegetation.standard_name` is `tree fraction`, while the forcing key and source variable are `f_frac_vegetation` and `veg_frac`.
2. `f_orgm.standard_name` is `CLAY`, while its source variable is `OCSTHA_SoilGrids`.
3. `f_silt.standard_name` is `CLAY`, while its source variable is `SLTPPT_SoilGrids`.
4. Hourly `f_rn.standard_name` is `Net radiation`, but its source variable is `SW_IN_POT_ONEFlux`.
5. `f_orgm.source_to_sindbad_unit` is `0.0`. If this factor is applied multiplicatively, the forcing will become entirely zero.
6. Hourly `f_PAR.standard_name` includes `Rg*.5 * 0.0036`, while the configured conversion factor is `0.0018`. These are mathematically consistent, but it should be stated explicitly that this converts one hour of W m⁻² to MJ m⁻² h⁻¹ rather than producing a daily total.

These issues can affect scientific interpretation and numerical results. Each should be confirmed by the configuration maintainer before operational coupling.

## 8. Can the Variables Be Used Directly by SINDBAD?

### 8.1 Direct Physical Counterparts That Still Require Conversion

| SINDBAD | WISP | Required processing |
|---|---|---|
| `f_frac_vegetation` | `fcover` | Verify product definitions, align time, aggregate grid to site |
| `f_rain` | `tp` | Difference accumulations, confirm precipitation phase, convert hourly/daily units |
| `f_rg` | `ssrd` | Difference accumulations, convert flux to energy, aggregate hourly/daily |
| `f_airT` | `t2m` | K→°C for raw data, align grid to site |
| `f_VPD` | `vpd` | Use physical kPa values, undo model transforms, handle the lower bound |

### 8.2 Derivable Quantities or Proxies That Require Validation

| SINDBAD | WISP basis | Main risk |
|---|---|---|
| `f_burnt_area` | `active_fire`, `frp` | Active fire is not burned area; overpass and observation biases remain |
| `f_tree_frac` | `fcover` | Trees cannot be separated from other vegetation |
| `f_PAR` | `0.5 × ssrd` | Empirical spectral fraction plus accumulation and unit handling |
| `f_rn` | `avg_snswrf + avg_snlwrf` | Sign convention, averaging period, and anomalous configured source |
| `f_airT_day` | hourly `t2m` | Daylight definition and aggregation statistic are undefined |
| `f_VPD_day` | hourly `vpd` | Daylight definition, nonlinearity, and aggregation order |

### 8.3 Variables That Require Additional External Data

`f_ambient_CO2`, `f_clay`, `f_dist_intensity`, `f_pft`, `f_orgm`, `f_rg_pot`, `f_sand`, and `f_silt` cannot be reliably recovered from WISP's current inputs. If the SINDBAD model structure actually uses these forcing variables, appropriate external datasets must be added instead of filling them with weakly related WISP channels.

## 9. Recommended Data-Exchange Workflow

If WISP data are later converted into actual SINDBAD forcing, the recommended workflow is:

1. Read `t2m`, `tp`, `ssrd`, `vpd`, `fcover`, and radiation components from WISP's **physical-variable stage**, not from normalized model tensors.
2. Decide whether the target is spatial/daily or hourly forcing, and process accumulated quantities and fluxes for the target time step.
3. Extract values for each site or aggregate to the target pixel, explicitly recording whether mean, sum, area-weighted mean, or nearest neighbor is used.
4. Implement separate, testable derivation functions for `f_PAR`, `f_rn`, and daytime variables. Do not hide unit conversion inside unrelated processing.
5. Preserve proxy/derived metadata and method descriptions for all `Approximate` outputs.
6. Add external products for all `No` variables. Do not use zero as if it were a genuine forcing value while data are missing.
7. Validate bounds, units, time axes, missing values, and dimensions before passing the result to SINDBAD.

## 10. Evidence Files

### SINDBAD

- [Spatial forcing configuration](dev/Sindbad.jl/examples/exp_WROASTED/settings_WROASTED/forcing_zarr.json)
- [Hourly forcing configuration](dev/Sindbad.jl/examples/exp_WROASTED/settings_WROASTED/forcing_hourly.json)

### WISP

- [Current 55-channel schema](../WildfireIgnitionPred/src/utility.py)
- [ERA5/CLMS/VIIRS preprocessing implementation](../WildfireIgnitionPred/preprocessing_code/preprocess_utils.py)
- [Physical transforms and normalization](../WildfireIgnitionPred/preprocessing_code/normalize_data.py)
- [Current preprocessing protocol](../WildfireIgnitionPred/Data_Preprocessing_Protocol.md)
- [Current pipeline reference](../WildfireIgnitionPred/Method_Reference_CurrentPipeline.md)

## 11. Final Conclusion

WISP can directly provide counterparts for SINDBAD air temperature, VPD, total precipitation, downward shortwave radiation, and vegetation-cover fraction, although unit, temporal, and spatial alignment is still required. PAR, complete net radiation, daytime temperature, daytime VPD, tree fraction, and burned-area fraction can only be approximated, derived, or represented by proxies. CO₂, soil texture, soil organic matter, PFT, disturbance intensity, and potential radiation are absent from WISP's current official inputs and must be supplied from external data sources.
