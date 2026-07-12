# SINDBAD forcing availability in WISP

Status legend:

- **Yes**: WISP contains a directly corresponding physical variable in its current 55-channel input schema.
- **Approximate**: WISP contains only a proxy/related observation, or the quantity must be derived from one or more WISP channels.
- **No**: WISP does not contain the quantity or a sufficiently direct substitute in its current 55-channel input schema.

`Yes` indicates physical correspondence; it does not imply identical source products, units, spatial resolution, temporal resolution, or preprocessing. Detailed evidence and limitations are documented in [WISP_forcing_mapping_explanation.md](WISP_forcing_mapping_explanation.md).

## WROASTED_Xu forcing (spatial setup)

| Variable | Standard Name | Bounds | Unit | Source Variable | Space-Time Type | in WISP | WISP Variable / Notes | Source (Xu) |
|----------|---------------|--------|------|-----------------|-----------------|---------|-----------------------|-------------|
| f_ambient_CO2 | ambient_CO2 | [200, 500] | ppm | atmCO2_SCRIPPS_global | spatiotemporal | No | No atmospheric CO2 channel | |
| f_clay | CLAY | [0.0, 100.0] | - | CLYPPT_SoilGrids | spatiovertical | No | Soil moisture (`swvl1`–`swvl4`) is not clay fraction | |
| f_dist_intensity | isDisturbed flag for disturbance | [-0.001, 1.1] | null | dist_frac_sb2018 | spatiotemporal | No | No land-disturbance intensity/flag channel | |
| f_burnt_area | fire fraction area per area pixel | [0.0, 1.0] | null | fire_frac | spatiotemporal | Approximate | `active_fire` / `frp` describe active-fire detections, not burned-area fraction | |
| f_tree_frac | tree fraction | [0.0, 1.0] | null | tree_frac | spatiovertical | Approximate | `fcover` is total vegetation cover and does not isolate trees | |
| f_frac_vegetation | tree fraction | [0.0, 1.0] | null | veg_frac | spatiovertical | Yes | `fcover` (fraction of vegetation cover) | |
| f_pft | pft index | [1, 17] | null | f_pft | spatial | No | No plant functional type channel | |
| f_orgm | CLAY | [0.0, 100.0] | - | OCSTHA_SoilGrids | spatiovertical | No | No soil organic-matter/carbon channel | |
| f_PAR | Photosynthetically active radiation (=Rg*.5) | [0, 50] | MJ m-2 d-1 | SW_IN_ERAIv2_gfld | spatiotemporal | Approximate | Derive approximately as `0.5 × ssrd` after unit/time conversion | |
| f_rain | Rain | [0, 300] | mm d-1 | P_ERAIv2_gfld | spatiotemporal | Yes | `tp` (total precipitation), with unit/time conversion | |
| f_rg | Global Radiation | [0.0, 100.0] | MJ m-2 d-1 | SW_IN_ERAIv2_gfld | spatiotemporal | Yes | `ssrd` (surface solar radiation downwards), with unit/time conversion | |
| f_rg_pot | Potential Global Radiation | [0.0, 100.0] | MJ m-2 d-1 | SW_IN_POT_ONEFlux | spatiotemporal | No | `ssrd` is actual, not potential, incoming solar radiation | |
| f_rn | Net radiation | [0.0, 100.0] | MJ m-2 d-1 | NETRAD_ERAIv2_gfld | spatiotemporal | Approximate | Derive from `avg_snswrf + avg_snlwrf`, subject to sign/unit/time conventions | |
| f_sand | SAND | [0.0, 100.0] | - | SNDPPT_SoilGrids | spatiovertical | No | Soil moisture (`swvl1`–`swvl4`) is not sand fraction | |
| f_silt | CLAY | [0.0, 100.0] | - | SLTPPT_SoilGrids | spatiovertical | No | Soil moisture (`swvl1`–`swvl4`) is not silt fraction | |
| f_airT | Tair | [-80.0, 60.0] | °C | TA_ERAIv2_gfld | spatiotemporal | Yes | `t2m` (2 m air temperature) | |
| f_airT_day | TairDay | [-80.0, 60.0] | °C | TA_DayTime_ERAIv2_gfld | spatiotemporal | Approximate | Derive a daytime statistic from hourly `t2m` using an explicit daylight mask | |
| f_VPD | Vapor pressure deficit | [0.01, 100.0] | kPa | VPD_ERAIv2_gfld | spatiotemporal | Yes | `vpd` in kPa, derived in WISP from `t2m` and `d2m` | |
| f_VPD_day | Vapor pressure deficit Day | [0.01, 100.0] | kPa | VPD_DayTime_ERAIv2_gfld | spatiotemporal | Approximate | Derive a daytime statistic from hourly `vpd` using an explicit daylight mask | |

## Hourly forcing (examples/exp_WROASTED settings)

| Variable | Standard Name | Bounds | Unit | Source Variable | Space-Time Type | in WISP | WISP Variable / Notes | Source (Xu) |
|----------|---------------|--------|------|-----------------|-----------------|---------|-----------------------|-------------|
| f_ambient_CO2 | ambient_CO2 | [200, 500] | ppm | CO2_GF | spatiotemporal | No | No atmospheric CO2 channel | |
| f_clay | CLAY | [0.0, 100.0] | - | CLYPPT_SoilGrids | spatiovertical | No | Soil moisture (`swvl1`–`swvl4`) is not clay fraction | |
| f_dist_intensity | isDisturbed flag for disturbance | [-0.001, 1.1] | null | dist_frac_sb2018 | spatiotemporal | No | No land-disturbance intensity/flag channel | |
| f_orgm | CLAY | [0.0, 100.0] | - | OCSTHA_SoilGrids | spatiovertical | No | No soil organic-matter/carbon channel | |
| f_PAR | Photosynthetically active radiation (=Rg*.5 * 0.0036) | [0, 50] | MJ m-2 day-1 | SW_IN_GF | spatiotemporal | Approximate | Derive approximately as `0.5 × ssrd` after unit/time conversion | |
| f_rain | Rain | [0, 300] | mm hr-1 | P_GF | spatiotemporal | Yes | `tp` (total precipitation), after recovering hourly increments and converting units | |
| f_rg | Global Radiation | [0.0, 100.0] | MJ m-2 day-1 | SW_IN_GF | spatiotemporal | Yes | `ssrd` (surface solar radiation downwards), after recovering hourly flux | |
| f_rg_pot | Potential Global Radiation | [0.0, 100.0] | MJ m-2 day-1 | SW_IN_POT_ONEFlux | spatiotemporal | No | `ssrd` is actual, not potential, incoming solar radiation | |
| f_rn | Net radiation | [0.0, 100.0] | MJ m-2 d-1 | SW_IN_POT_ONEFlux | spatiotemporal | Approximate | Derive from `avg_snswrf + avg_snlwrf`; configured source name is inconsistent with net radiation | |
| f_sand | SAND | [0.0, 100.0] | - | SNDPPT_SoilGrids | spatiovertical | No | Soil moisture (`swvl1`–`swvl4`) is not sand fraction | |
| f_silt | CLAY | [0.0, 100.0] | - | SLTPPT_SoilGrids | spatiovertical | No | Soil moisture (`swvl1`–`swvl4`) is not silt fraction | |
| f_airT | Tair | [-80.0, 60.0] | °C | TA_GF | spatiotemporal | Yes | `t2m` (2 m air temperature) | |
| f_airT_day | TairDay | [-80.0, 60.0] | °C | TA_GF | spatiotemporal | Approximate | Derive a daytime statistic from hourly `t2m` using an explicit daylight mask | |
| f_VPD | Vapor pressure deficit | [0.01, 100.0] | kPa | VPD_GF | spatiotemporal | Yes | `vpd` in kPa, derived in WISP from `t2m` and `d2m` | |
| f_VPD_day | Vapor pressure deficit Day | [0.01, 100.0] | kPa | VPD_GF | spatiotemporal | Approximate | Derive a daytime statistic from hourly `vpd` using an explicit daylight mask | |
