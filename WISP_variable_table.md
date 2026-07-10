## WROASTED_Xu forcing (spatial setup)

| Variable | Standard Name | Bounds | Unit | Source Variable | Space-Time Type | in WISP | Source (Xu) |
|----------|---------------|--------|------|-----------------|-----------------|--------|-------------|
| f_ambient_CO2 | ambient_CO2 | [200, 500] | ppm | atmCO2_SCRIPPS_global | spatiotemporal | | |
| f_clay | CLAY | [0.0, 100.0] | - | CLYPPT_SoilGrids | spatiovertical | | |
| f_dist_intensity | isDisturbed flag for disturbance | [-0.001, 1.1] | null | dist_frac_sb2018 | spatiotemporal | | |
| f_burnt_area | fire fraction area per area pixel | [0.0, 1.0] | null | fire_frac | spatiotemporal | | |
| f_tree_frac | tree fraction | [0.0, 1.0] | null | tree_frac | spatiovertical | | |
| f_frac_vegetation | tree fraction | [0.0, 1.0] | null | veg_frac | spatiovertical | | |
| f_pft | pft index | [1, 17] | null | f_pft | spatial | | |
| f_orgm | CLAY | [0.0, 100.0] | - | OCSTHA_SoilGrids | spatiovertical | | |
| f_PAR | Photosynthetically active radiation (=Rg*.5) | [0, 50] | MJ m-2 d-1 | SW_IN_ERAIv2_gfld | spatiotemporal | | |
| f_rain | Rain | [0, 300] | mm d-1 | P_ERAIv2_gfld | spatiotemporal | | |
| f_rg | Global Radiation | [0.0, 100.0] | MJ m-2 d-1 | SW_IN_ERAIv2_gfld | spatiotemporal | | |
| f_rg_pot | Potential Global Radiation | [0.0, 100.0] | MJ m-2 d-1 | SW_IN_POT_ONEFlux | spatiotemporal | | |
| f_rn | Net radiation | [0.0, 100.0] | MJ m-2 d-1 | NETRAD_ERAIv2_gfld | spatiotemporal | | |
| f_sand | SAND | [0.0, 100.0] | - | SNDPPT_SoilGrids | spatiovertical | | |
| f_silt | CLAY | [0.0, 100.0] | - | SLTPPT_SoilGrids | spatiovertical | | |
| f_airT | Tair | [-80.0, 60.0] | °C | TA_ERAIv2_gfld | spatiotemporal | | |
| f_airT_day | TairDay | [-80.0, 60.0] | °C | TA_DayTime_ERAIv2_gfld | spatiotemporal | | |
| f_VPD | Vapor pressure deficit | [0.01, 100.0] | kPa | VPD_ERAIv2_gfld | spatiotemporal | | |
| f_VPD_day | Vapor pressure deficit Day | [0.01, 100.0] | kPa | VPD_DayTime_ERAIv2_gfld | spatiotemporal | | |

## Hourly forcing (examples/exp_WROASTED settings)

| Variable | Standard Name | Bounds | Unit | Source Variable | Space-Time Type | in WISP | Source (Xu) |
|----------|---------------|--------|------|-----------------|-----------------|--------|-------------|
| f_ambient_CO2 | ambient_CO2 | [200, 500] | ppm | CO2_GF | spatiotemporal | | |
| f_clay | CLAY | [0.0, 100.0] | - | CLYPPT_SoilGrids | spatiovertical | | |
| f_dist_intensity | isDisturbed flag for disturbance | [-0.001, 1.1] | null | dist_frac_sb2018 | spatiotemporal | | |
| f_orgm | CLAY | [0.0, 100.0] | - | OCSTHA_SoilGrids | spatiovertical | | |
| f_PAR | Photosynthetically active radiation (=Rg*.5 * 0.0036) | [0, 50] | MJ m-2 day-1 | SW_IN_GF | spatiotemporal | | |
| f_rain | Rain | [0, 300] | mm hr-1 | P_GF | spatiotemporal | | |
| f_rg | Global Radiation | [0.0, 100.0] | MJ m-2 day-1 | SW_IN_GF | spatiotemporal | | |
| f_rg_pot | Potential Global Radiation | [0.0, 100.0] | MJ m-2 day-1 | SW_IN_POT_ONEFlux | spatiotemporal | | |
| f_rn | Net radiation | [0.0, 100.0] | MJ m-2 d-1 | SW_IN_POT_ONEFlux | spatiotemporal | | |
| f_sand | SAND | [0.0, 100.0] | - | SNDPPT_SoilGrids | spatiovertical | | |
| f_silt | CLAY | [0.0, 100.0] | - | SLTPPT_SoilGrids | spatiovertical | | |
| f_airT | Tair | [-80.0, 60.0] | °C | TA_GF | spatiotemporal | | |
| f_VPD | Vapor pressure deficit | [0.01, 100.0] | kPa | VPD_GF | spatiotemporal | | |
