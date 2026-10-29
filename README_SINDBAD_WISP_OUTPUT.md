# SINDBAD WISP experiment: output

This document describes the output of a WISP experiment: where it is written, how
folders and files are named, which variables are saved, and which figures and
comparison files are made.

A WISP experiment runs in three stages, each on SLURM jobs of its own, all with the
Sindbad in `dev/` and all output under one root:

| Stage | Domain of one job | Run script | Setup | Output |
|---|---|---|---|---|
| `site` | one FLUXNET site, optimized with CMA-ES | `slurm_scripts/site_opti_exp.jl` | `setups/Site/` | `site/`, section 3 |
| `pft` | one PFT, all its sites at once, optimized with CMA-ES | `slurm_scripts/pft_opti_exp.jl` | `setups/PFT/` | `pft/`, section 4 |
| `entity` | one WISP entity, forward runs of the fire experiments on its whole grid | `slurm_scripts/entity_forward_exp.jl` | `setups/WISP/` | `entity/`, section 6 |

Between the stages, the output of one stage is combined into the input of the next,
see section 5. How to set up and submit the stages is described in
`slurm_scripts/README_SLURM.md`, and the combine scripts in `combine_outputs/README.md`.
All settings are in `slurm_scripts/env.sh` and the stage files
`slurm_scripts/stage_site.sh`, `stage_pft.sh` and `stage_entity.sh`.

## Locations on the cluster

| What | Path | Setting |
|---|---|---|
| Output root | `/Net/Groups/BGI/tscratch/skoirala/WISP_V1_202610` | `SINDBAD_OUTPUT_ROOT` in `env.sh`, named after `SINDBAD_WISP_EXPERIMENT` |
| Output of a stage | `<output root>/site`, `/pft` and `/entity` | `SINDBAD_OUTPUT` of the stage |
| Site data of the site and PFT stages | `/Net/Groups/BGI/work_3/sindbad/data/WR_DD_202610/FLUXNET_v2023_12_1D_202610_skoirala.zarr` | `SINDBAD_DATA_DEPOT` and `SINDBAD_INPUT` in `env.sh` |
| Forcing of the entities | `/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/wisp_eos_q10_med10_v1/outputs/base_v5` | `SINDBAD_ENTITIES` in `stage_entity.sh` |
| System images | `/Net/Groups/BGI/scratch/skoirala/sysimages/sindbad_<stage exp>.so` | `SINDBAD_SYSIMAGE_DIR` in `env.sh` |

In this document, `$SINDBAD_OUTPUT_ROOT` stands for the output root above, and the
experiment names of the stages are:

| Stage | Experiment name, `<stage exp>` |
|---|---|
| `site` | `WISP_V1_202610_site` |
| `pft` | `WISP_V1_202610_pft` |
| `entity` | `WISP_V1_202610_entity` |

## 1. Layout of the output root

```
$SINDBAD_OUTPUT_ROOT/
├── site/
│   ├── output_<site>_WISP_V1_202610_site/          one run folder per site, section 3
│   ├── restart_concatenated_WISP_V1_202610_site.zarr   input of the PFT stage, section 5
│   ├── domains.txt, logs/, done/, sysimage_warmup/     job management, section 7
├── pft/
│   ├── output_WISP_V1_202610_PFT_<k>_WISP_V1_202610_pft/   one run folder per PFT, section 4
│   ├── params_by_pft_WISP_V1_202610_pft.zarr       input of the entity stage, section 5
│   ├── params_by_pft_WISP_V1_202610_pft.json
│   ├── domains.txt, logs/, done/, sysimage_warmup/
├── entity/
│   ├── <entity>/                                   one folder per entity, section 6
│   │   ├── output_WISP_<entity>-full_<experiment>/ one run folder per fire experiment
│   │   └── comparison/                             comparison of the fire experiments
│   ├── domains.txt, logs/, done/, sysimage_warmup/
└── logs/chain/                                     logs of the prep and watch jobs of run_all.sh
```

## 2. The run folder

Every run of every stage writes one run folder, named by Sindbad
`output_<domain>_<name>`, with the `domain` and `name` of the experiment json as the job
script sets them:

| Stage | Domain | Name | Run folder |
|---|---|---|---|
| `site` | the site, e.g. `DE-Hai` | `<stage exp>` | `output_DE-Hai_WISP_V1_202610_site` |
| `pft` | `WISP_V1_202610_PFT_<k>` | `<stage exp>` | `output_WISP_V1_202610_PFT_4_WISP_V1_202610_pft` |
| `entity` | `WISP`, from the experiment json | `<entity>-full_<experiment>` | `output_WISP_MED_ES_01-full_No_Fire` |

A run folder has these subfolders:

| Folder | Content |
|---|---|
| `data/` | Model output and the restart file, NetCDF |
| `optimization/` | Optimized parameters and performance. Only in the site and PFT stages. |
| `figure/` | Figures of the run |
| `settings/` | Copies of the setup jsons and the full Sindbad `info` of the run, `info.jld2`, since `save_info = true` in all stages |
| `code/` | `model_definitions.jl`, `model_functions.jl` and `model_structs.jl`: the code of the model as it was run |

The jsons in `settings/` are unchanged copies of the setup files. They do not include
the changes the job script makes through `replace_info`, e.g. the name, the domain, the
output path, the input paths and, in the entity stage, the fire forcing. The settings
as they were used are in `settings/info.jld2`, under `info.experiment`:

```julia
using Sindbad
using Sindbad.Setup: load        # JLD2's load, as Sindbad provides it
info = load(joinpath(run, "settings", "info.jld2"))["info"]
info.experiment.basics           # name, domain and period of the run
```

### `data/restart.nc`

All stages write `restart.nc`: the pools `cEco` and `TWS` of every location after the
spinup, i.e. at the start of the simulation period, not at its end. It has no `time`
dimension. Its global attributes add to those of the output files:

| Attribute | Meaning |
|---|---|
| `restart_date` | The start date of the simulation, the date of the state |
| `spinup_sequence_method` | The spinup sequence, e.g. `sequence_with_age` |
| `spinup_mode` | The spinup mode of the step that saved the restart, e.g. `LoopTEM` |

Spinup states of every step are not saved (`store_spinup = false` in all experiment
jsons), although the model is spun up before every run (`spinup_TEM = true`).

### Global attributes of the output files

Each NetCDF file records the run: `experiment` is the run name. The other attributes
are `domain`, `date`, `simulation_by`, `host`, `machine`, `os` and `julia`.

## 3. Site stage: `site/`

One CMA-ES optimization per site, on the 8 threads of the job. Each site runs alone,
with its own spinup.

| Setting | Value |
|---|---|
| Experiment json | `setups/Site/experiment_refactored_WRDY_SoilDepth_MT.json` |
| Forcing and observations | The site data zarr of the data depot, subset to the site |
| Period | 1979-01-01 to 2017-12-31, from the experiment json |
| Model structure | `setups/Site/model_structure_WRDY_SoilDepth.json`, without the fire models and the LAI based fAPAR of the entity model |
| Spinup | `sequence_with_age` |
| Optimizer | CMA-ES, population 16, `CMAEvolutionStrategy_CMAES_SoilDepth_MT.json` |
| Parameters | 41, listed in `optimization_refactored_WRDY_SoilDepth_MT.json` |
| Constraints | `agb`, `evapotranspiration`, `gpp`, `gpp_iav`, `ndvi`, `nee`, `nirv`, `reco`, `transpiration`, summed (`metric_sum`), mostly `NSE_inv` |

After the optimization, Sindbad runs the model once with the default and once with the
optimized parameters, and saves the optimized run.

### `data/`

| File | Content |
|---|---|
| `all_variables.nc` | All output variables of the experiment json in one file (`save_single_file = true`), from the run with the optimized parameters. The space dimension is `site`, of length 1. |
| `restart.nc` | The pools after the spinup with the optimized parameters, see section 2 |

The output variables are the fluxes, pools and diagnostics listed under
`model_output.variables` of the experiment json, e.g. `gpp`, `nee`,
`evapotranspiration`, `cEco` and `TWS`, plus diagnostics of the carbon allocation,
flows and turnover. Layered variables use `d_cEco` (8 carbon pools), `d_soil` (soil
layers), `d_tws` (water storages) and `d_cFlow` (11 carbon flows).

### `optimization/`

| File | Content |
|---|---|
| `model_parameters_optimized.csv` | The parameter table of the optimization, one row per optimized parameter, with its optimized value in the `optimized` column |
| `params.zarr` and `params.json` | The optimized parameters as a parameter file, one variable `<model>__<parameter>` per parameter on the `site` dimension. The json can be given as the `parameters` config file of another run. |
| `performance.csv` | One row per constraint: its cost metric and weight, the number of points, the means of the observation and of the default and optimized output, `loss_def` and `loss_opt`, and a `<metric>_def` and `<metric>_opt` column for every metric of ErrorMetrics |

### `figure/`

| File | Content |
|---|---|
| `comparison_time_series.png` | One row per constraint: the observation, the default and the optimized output, and their mean seasonal cycle |
| `comparison_histograms.png` | One panel per constraint: the metric of the default and the optimized output |
| `debug_<variable>[_<layer>].png` | Time series of each output variable, default and optimized |
| `forcing_<variable>.png` | Time series of each forcing variable |

Sindbad also draws the carbon flow matrix of the `cCycleBase` model in `figure/`.

## 4. PFT stage: `pft/`

One CMA-ES optimization per PFT: one parameter vector for all sites of the PFT, whose
sites run in parallel on the threads of the job. The cpus and memory of a job grow
with the number of sites of its PFT, see `stage_pft.sh`.

| Setting | Value |
|---|---|
| Experiment json | `setups/PFT/experiment_refactored_WRDY_SoilDepth_PFT.json` |
| Domain | `WISP_V1_202610_PFT_<k>`, with `<k>` the `f_pft` class of the sites |
| Sites | The sites of PFT `<k>` in the site data that have pools in the site restart file. PFTs with fewer than `SINDBAD_MIN_SITES` sites are not run. |
| Period | 1979-01-01 to 2017-12-31, from the experiment json |
| Model structure | The model structure of the site stage |
| Spinup | `sequence_with_spinup_input`: every site starts from its own pools in `site/restart_concatenated_WISP_V1_202610_site.zarr`, see section 5 |
| Optimizer | CMA-ES, `CMAEvolutionStrategy_CMAES_SoilDepth_PFT.json` |
| Parameters | The 40 parameters of the site stage without `cCycleBase,ηH` |
| Constraints | As in the site stage, with the cost over all sites of the PFT |

The files of a run folder are those of the site stage, see section 3, with these
differences:

- **`data/all_variables.nc` and `data/restart.nc`** have the `site` dimension with all
  sites of the PFT.
- **`optimization/params.zarr`** has the same value at every site, since the PFT has
  one parameter vector.
- **`figure/`** has the forms for many locations:

| File | Content |
|---|---|
| `comparison_<constraint>_timeseries.png` | Mean over all sites and up to five random sites, of the optimized output and the observation |
| `comparison_<constraint>_spacetime.png` | Optimized output and observation of every site over time |
| `comparison_histograms.png` | One panel per constraint: histograms of the metric per site of the default and the optimized output |
| `debug_<variable>_timeseries[_<layer>].png` and `debug_<variable>_spacetime[_<layer>].png` | Each output variable of the optimized run, as series and as site by time |
| `forcing_<variable>_timeseries[_<layer>].png` and `forcing_<variable>_spacetime[_<layer>].png` | Each forcing variable with time. A forcing variable without time has one `forcing_<variable>_spacetime.png` with a column per layer. |

## 5. Files between the stages

The combined files are written next to the run folders of the stage they combine. The
chain builds them when the next stage starts, once every domain of the stage before is
done, by `slurm_scripts/prepare_stage.sh` with the scripts in `combine_outputs/`. An
existing file is used as it is, so delete it to combine again.

| File | Built from | Content | Used by |
|---|---|---|---|
| `site/restart_concatenated_WISP_V1_202610_site.zarr` | `data/restart.nc` of every site run | The pools `cEco` and `TWS` of every site after its spinup, on the `site` dimension | `SINDBAD_SITE_RESTART` of the PFT stage |
| `pft/params_by_pft_WISP_V1_202610_pft.zarr` and `.json` | `optimization/params.zarr` of every PFT run | One value per parameter and PFT, on the `f_pft` dimension. PFTs without parameters get the default of each parameter, or those of `SINDBAD_DEFAULT_PFT` when it is set. | `SINDBAD_PFT_PARAMETERS` and `SINDBAD_PFT_PARAMETER_MAP` of the entity stage: each pixel takes the parameters of its `f_pft` |

Two optional combines are made only by hand, with
`combine_outputs/run_combine.sh`:

| File | Content |
|---|---|
| `site/params_concatenated_WISP_V1_202610_site.zarr` and `.json` | The optimized parameters of every site, on the `site` dimension |
| `pft/restart_concatenated_WISP_V1_202610_pft.zarr` | The pools of every site after the spinup with the parameters of its PFT |

A PFT whose run failed has no parameters in the map. With `SINDBAD_ALLOW_PARTIAL=1` the
next stage starts from the done domains only, so check the log of the combine for the
runs without a file.

## 6. Entity stage: `entity/`

Forward runs of the WISP entities with the PFT parameters of section 5, one entity per
job, on the whole lat/lon grid of its forcing file.

The forcing of an entity is in a folder of its own, e.g.
`base_v5/MED_ES_01_frozen_v1/daily_master_dev_v5_MED_ES_01_frozen_v1.nc`. The entity is
named without the `_frozen_v1` suffix in the output, e.g. `MED_ES_01`. The runs cover
2014-01-12 to 2023-12-31, the period of the forcing files, set by `SINDBAD_DATE_BEGIN`
and `SINDBAD_DATE_END` in `stage_entity.sh`.

The model has the fire models and an LAI based fAPAR that the site and PFT model does
not have. Their parameters are not in the PFT map and keep their defaults.

### 6.1 Fire experiments

Every entity is run up to three times. The runs differ only in the forcing of
`f_burnt_area`, the burnt area fraction that drives the fire module:

| Experiment | Source variable of `f_burnt_area` | Scalar | Meaning |
|---|---|---|---|
| `No_Fire` | `viirs_fire_binary` | 0.0 | Fire turned off |
| `VIIRS_Fire` | `viirs_fire_binary` | 0.99 | Fire from VIIRS active-fire detections |
| `WISP_Fire` | `WISP_fire_binary` | 0.99 | Fire from the WISP prediction |

The scalar is `source_to_sindbad_unit` of `f_burnt_area`: the forcing is multiplied by
it. Each experiment is switched on with `SINDBAD_DO_NO_FIRE`, `SINDBAD_DO_VIIRS_FIRE` and
`SINDBAD_DO_WISP_FIRE` in `stage_entity.sh`. The source variables are set by
`SINDBAD_VIIRS_FIRE_VAR` and `SINDBAD_WISP_FIRE_VAR`. In the current run,
`No_Fire` and `VIIRS_Fire` are switched on and `WISP_Fire` is off.

> **Note.** In the current forcing files (`*_frozen_v1.nc`), `WISP_fire_binary` is a
> placeholder that is an exact copy of `viirs_fire_binary`. Until real WISP predictions
> are in the files, `VIIRS_Fire` and `WISP_Fire` give identical results. Where a pixel
> has no fire days, all three experiments give identical results.

### 6.2 Folder layout

```
entity/
└── <entity>/                                   e.g. MED_ES_01
    ├── output_WISP_<domain>_No_Fire/           one run folder per fire experiment
    │   ├── data/                               model output, NetCDF
    │   ├── figure/                             maps and time series of this run
    │   ├── settings/                           setup of this run
    │   └── code/                               model code of this run
    ├── output_WISP_<domain>_VIIRS_Fire/
    ├── output_WISP_<domain>_WISP_Fire/
    └── comparison/                             comparison of the fire experiments
```

| Name | Pattern | Example |
|---|---|---|
| Domain | `<entity>-<subset>` | `MED_ES_01-full` |
| Run name | `<domain>_<experiment>` | `MED_ES_01-full_No_Fire` |
| Run folder | `output_WISP_<run name>` | `output_WISP_MED_ES_01-full_No_Fire` |
| Figure tag of a run | `<domain>-<experiment>` | `MED_ES_01-full-No_Fire` |

`WISP` in the run folder is the `domain` of the experiment json, which Sindbad puts in
front of the run name.

The subset part of the domain is `full`: every run covers the whole lat/lon grid of the
forcing file of its entity. Only the short warmup run of the system image build in
`entity/sysimage_warmup/` uses a 4 x 4 block of pixels, tagged `px4x4`.

### 6.3 Model output: `data/`

One NetCDF file per output variable (`save_single_file = false`), named after the
variable, plus `restart.nc`.

#### Dimensions and coordinates

| Dimension | Meaning |
|---|---|
| `time` | Daily time steps, `days since 1980-01-01`, standard calendar. The forcing files run from 2014-01-12 to 2023-12-31, which is 3641 days, with time stamps at 00:00. |
| `lat`, `lon` | Latitude and longitude of the pixel centers in degrees, as in the forcing file |
| `d_cEco` | The 8 carbon pools, see below |
| `d_tws` | The 10 water storage layers, see below |

Variables are stored as `float` with the dimension order `(lon, lat, [layer], time)` as
written in the file. xarray and netCDF4 in Python show this order. Julia tools show
the reverse order.

The files are compressed (`data_compression` in the experiment json: `level = 4`,
`digits = 4`). The model runs in `Float32`.

#### Variables

| File | Variable | Group | Layers | Units | Description |
|---|---|---|---|---|---|
| `gpp.nc` | `gpp` | fluxes | none | gC m-2 day-1 | Gross primary productivity |
| `auto_respiration.nc` | `auto_respiration` | fluxes | none | gC m-2 day-1 | Autotrophic respiration |
| `hetero_respiration.nc` | `hetero_respiration` | fluxes | none | gC m-2 day-1 | Heterotrophic respiration |
| `eco_respiration.nc` | `eco_respiration` | fluxes | none | gC m-2 day-1 | Total ecosystem respiration, autotrophic plus heterotrophic |
| `nee.nc` | `nee` | fluxes | none | gC m-2 day-1 | Net ecosystem exchange. Negative values are a carbon sink. |
| `evapotranspiration.nc` | `evapotranspiration` | fluxes | none | mm day-1 | Total evaporation: soil evaporation, transpiration, snow sublimation and interception loss |
| `transpiration.nc` | `transpiration` | fluxes | none | mm day-1 | Transpiration |
| `runoff.nc` | `runoff` | fluxes | none | mm day-1 | Total runoff |
| `cEco.nc` | `cEco` | pools | `d_cEco` (8) | gC m-2 | Carbon content of each ecosystem carbon pool |
| `TWS.nc` | `TWS` | pools | `d_tws` (10) | mm | Water content of each water storage layer |
| `c_fire_efflux.nc` | `c_fire_efflux` | diagnostics | `d_cEco` (8) | gC m-2 day-1 | Carbon of each pool combusted and emitted to the atmosphere |
| `c_fire_flow.nc` | `c_fire_flow` | diagnostics | `d_cEco` (8) | gC m-2 day-1 | Carbon killed but not combusted, received by each litter or soil pool |
| `c_fire_mortality.nc` | `c_fire_mortality` | diagnostics | `d_cEco` (8) | gC m-2 day-1 | Total carbon lost by each vegetation pool: its combusted part plus its flow to litter and soil |

The fire diagnostics come from the `cFire_simple` model. The model can also add
vegetation die-off to them, but die-off is turned off in these runs
(`cVegetationDieOff` is `none` in the model structure json), so they show the effect of
fire alone. Litter and soil pools have no mortality: they lose carbon to fire only
through `c_fire_efflux`.

#### Carbon pools, `d_cEco`

The model uses the `GSI` carbon pool configuration, the same as the site and PFT
stages:

| Index | Pool | Description |
|---|---|---|
| 1 | `cVegRoot` | Root carbon |
| 2 | `cVegWood` | Wood carbon |
| 3 | `cVegLeaf` | Leaf carbon |
| 4 | `cVegReserve` | Vegetation reserve carbon |
| 5 | `cLitFast` | Fast litter |
| 6 | `cLitSlow` | Slow litter |
| 7 | `cSoilSlow` | Slow soil carbon |
| 8 | `cSoilOld` | Old soil carbon |

#### Water storage layers, `d_tws`

From the `pools.water` block of the model structure json:

| Index | Layer | Description |
|---|---|---|
| 1 to 7 | `soilW` | Soil water of the 7 soil layers, from top to bottom, with layer depths of 50, 200, 750, 1000, 3000, 5000 and 40000 mm in the model structure json |
| 8 | `groundW` | Groundwater |
| 9 | `snowW` | Snow water |
| 10 | `surfaceW` | Surface water |

#### `restart.nc`

`cEco` and `TWS` of every pixel after the spinup, see section 2. The entity runs spin
up with `sequence_with_age` and with the fire models off, as `use_in_spinup` is false
for them, so the restart files of the fire experiments of an entity are the same.

#### Reading the output

Python:

```python
import xarray as xr

run = "/Net/Groups/BGI/tscratch/skoirala/WISP_V1_202610/entity/MED_ES_01/output_WISP_MED_ES_01-full_No_Fire"
gpp = xr.open_dataset(f"{run}/data/gpp.nc").gpp
gpp_map = gpp.mean("time")                    # time-mean map
gpp_series = gpp.mean(["lat", "lon"])         # mean over all pixels per day

ceco = xr.open_dataset(f"{run}/data/cEco.nc").cEco
c_veg = ceco.isel(d_cEco=slice(0, 4)).sum("d_cEco")   # vegetation carbon, pools 1 to 4
```

Julia:

```julia
using YAXArrays, NetCDF
run = "/Net/Groups/BGI/tscratch/skoirala/WISP_V1_202610/entity/MED_ES_01/output_WISP_MED_ES_01-full_No_Fire"
gpp = open_dataset(joinpath(run, "data", "gpp.nc")).gpp
```

### 6.4 Setup of a run: `settings/` and `code/`

| File | Content |
|---|---|
| `settings/experiment_WISP_DD.json` | Copy of the experiment json in `setups/WISP/` |
| `settings/forcing_WISP_DD.json` | Copy of the forcing json |
| `settings/model_structure_WISP_DD.json` | Copy of the model structure json: the selected model of each process and the pools |
| `settings/params_by_pft_WISP_V1_202610_pft.json` | Copy of the parameter json, the PFT map of section 5 |
| `settings/info.jld2` | The full Sindbad `info` of the run, with the settings as they were used |
| `code/model_definitions.jl`, `code/model_functions.jl`, `code/model_structs.jl` | The code of the model as it was run |

The jsons in `settings/` are the same for all fire experiments. The copy of the
forcing json shows the `f_burnt_area` entry of the setup file in every run folder, also
in the `No_Fire` folder, whose run used the scalar 0.0. To see which fire forcing a run
used:

```julia
using Sindbad
using Sindbad.Setup: load
run = "/Net/Groups/BGI/tscratch/skoirala/WISP_V1_202610/entity/MED_ES_01/output_WISP_MED_ES_01-full_No_Fire"
info = load(joinpath(run, "settings", "info.jld2"))["info"]
fba = info.experiment.data_settings.forcing.variables.f_burnt_area
fba.source_variable, fba.source_to_sindbad_unit   # e.g. ("viirs_fire_binary", 0.0)
```

The run name is also in the `experiment` global attribute of every NetCDF file.

### 6.5 Figures of a run: `figure/`

Made when `SINDBAD_MAKE_PLOTS=1` in `stage_entity.sh`, by `plot_forward_run` in
`plot_forward_WISP_DD.jl`. Sindbad's own figures of sections 3 and 4 are not drawn,
since the entity experiment json sets no `visualization_backend`. All figures are PNG
files. Layered variables get one figure per layer, with the layer index after the
variable name. The tag at the end is the figure tag of the run, e.g.
`MED_ES_01-full-No_Fire`.

| File | Content |
|---|---|
| `<group>_<variable>[_<layer>]_<tag>.png` | Map of the time mean of an output variable. The title gives the spatial mean and the number of pixels without a valid value. |
| `<group>_<variable>[_<layer>]_timeseries_<tag>.png` | The mean over all pixels of an output variable, as daily values and as monthly and annual means, in three panels |
| `forc_<forcing variable>[_<layer>]_<tag>.png` | Map of a forcing variable, see below |

Examples: `fluxes_gpp_MED_ES_01-full-No_Fire.png`,
`pools_cEco_3_timeseries_MED_ES_01-full-No_Fire.png`,
`forc_f_burnt_area_MED_ES_01-full-No_Fire.png`.

All means are NaN-aware, so pixels without data do not hide the others. The maps are
drawn by array index: lat index on the y axis and lon index on the x axis, with the
first row of the array at the bottom. Whether north is up depends on the order of lat
in the forcing file.

How a forcing variable is shown depends on its dimensions:

| Type | Variables | Figure |
|---|---|---|
| Space and time | `f_airT`, `f_airT_day`, `f_burnt_area`, `f_frac_vegetation`, `f_PAR`, `f_rain`, `f_rg`, `f_rg_pot`, `f_rn`, `f_tree_frac`, `f_VPD_day` | Map of the time mean |
| Space and soil layer | `f_clay`, `f_sand`, `f_silt`, `f_orgm` | One map per soil layer |
| Space only | `f_dist_year`, `f_pft`, `f_soil_depth` | Map |
| Time only | `f_ambient_CO2` | Time series over the time index |

The forcing variables, their sources in the forcing file and their units are listed in
`forcing_WISP_DD.json`. The `f_burnt_area` figure shows the forcing after the scalar of
the experiment, so it is zero everywhere for `No_Fire`.

### 6.6 Comparison of the fire experiments: `comparison/`

Made when two or more fire experiments run and `SINDBAD_MAKE_COMPARISON=1` in
`stage_entity.sh`, by `plot_runs_comparison` in `plot_forward_WISP_DD.jl`, once all
experiments of the entity are done. With only one experiment, nothing is compared. The
tag of the comparison files is the domain, e.g. `MED_ES_01-full`.

| File | Content |
|---|---|
| `<group>_<variable>[_<layer>]_mean_comparison_<domain>.png` | Comparison matrix of the time-mean maps, see below |
| `<group>_<variable>[_<layer>]_timeseries_comparison_<domain>.png` | Time series of all experiments, see below |
| `comparison_metrics_<domain>.csv` | Metrics of every pair of experiments, for every variable and layer |
| `summary_<experiment>.jls` | The time-mean maps and mean series of an experiment, saved so that a rerun of the job can compare experiments it skipped. Read with `Serialization.deserialize` in Julia. |

#### Comparison matrix

An n x n panel figure for n experiments, in the order No_Fire, VIIRS_Fire, WISP_Fire.
The experiment names are the column titles of the top row and the row labels of the
first column.

| Position | Panel |
|---|---|
| Diagonal | Time-mean map of each experiment, with its spatial mean in the title. All diagonal maps share the colorbar at the top left. |
| Above the diagonal, row i, column j | Difference map, column minus row: experiment j minus experiment i. All difference maps share the colorbar at the top right: blue is negative, red is positive, and differences within 5% of the largest one are grey. |
| Below the diagonal, row i, column j | Histogram of the pixel differences, experiment i minus experiment j, as named on the x axis. This is the distribution of the difference map mirrored across the diagonal, with the bars in the same colors. The title gives `r`, `r2`, `bias` and `rmse` of experiment i against experiment j. |

All difference maps and histograms of one figure share one symmetric range, set by the
largest difference of any pair. Small differences of one pair can therefore look faint
next to a pair that differs a lot.

#### Time series comparison

The mean over all pixels of each experiment in three panels: daily values, monthly
means and annual means. Each experiment has its own color, line style and marker. The
legend of the daily panel gives `r2`, `bias` and `rmse` of each experiment against the
first one.

#### Metrics file

One row per variable or layer, scale and pair of experiments:

| Column | Meaning |
|---|---|
| `variable` | Variable and layer, e.g. `fluxes_gpp` or `pools_cEco_3` |
| `scale` | `spatial`: computed over the pixels of the time-mean maps. `temporal`: computed over the days of the series of means over all pixels. |
| `reference` | The first experiment of the pair |
| `other` | The second experiment of the pair |
| `n` | Number of values that are valid in both experiments |
| `r` | Pearson correlation |
| `r2` | Square of `r` |
| `bias` | Mean of other minus reference |
| `rmse` | Root mean square of other minus reference |

`r` and `r2` are `NaN` when a variable has no variance, e.g. a layer with the same
constant value in every pixel. When two experiments are identical, `bias` and `rmse`
are 0.

### 6.7 Sizes and memory

The runs are not lazy (`run_lazy = false` in the experiment json), so the full output
of a run is kept in memory for the figures and the comparison. The comparison keeps
only the time-mean maps and the mean series of each experiment, which are small.

A 128 x 128 grid over the 10 years writes about 9 GB of compressed output per
experiment, so an entity with three experiments takes about three times that. The
NetCDF files are compressed with zlib at level 4, with shuffle, so the size depends on
the data. The fire diagnostics are mostly zeros where there is no fire and take little
space there.

## 7. Files of the job management

Each stage folder, `site/`, `pft/` and `entity/`, holds the files of the job management
of its stage next to its run folders:

| Path | Content |
|---|---|
| `domains.txt` | One `<domain> <n_sites>` line per domain: a site, a PFT or an entity. Line i is the domain of job or array task i. `n_sites` sizes the jobs of the PFT stage and is 1 in the others. |
| `logs/<stage>_<index>_<domain>_<jobid>.log` | Log of the job of one domain, in `jobs` mode |
| `logs/<stage>_<arrayid>_<task>.log` | Log of one array task, in `array` mode |
| `logs/submit_watch.log` | Log of the watcher that submits the jobs, when the stage is run with `run.sh` |
| `done/<domain>` | Marker of a finished domain. The submit scripts and a rerun of the job skip it. |
| `done/<experiment>/<entity>` | Entity stage only: marker of a finished fire experiment of an entity. A rerun of the job skips it. `done/<entity>` follows once all switched-on experiments are done. |
| `sysimage_warmup/` | Output of the short warmup run made while building the system image of the stage. It can be deleted. |

The jobs of `run_all.sh`, which links the stages, log to `$SINDBAD_OUTPUT_ROOT/logs/chain/`:

| Path | Content |
|---|---|
| `prep_<stage>_<jobid>.log` | The prep job of a stage: the combine of the stage before and the build of the system image |
| `watch_<stage>_<jobid>.log` | The watch job of a stage: the submit rounds and the list of failed domains |
| `chain_tmux.log` | The same steps, when the chain runs in tmux with `run_all.sh --tmux` |

`SINDBAD_OVERWRITE=1` in `env.sh` runs domains and fire experiments again even when they
have done markers.
