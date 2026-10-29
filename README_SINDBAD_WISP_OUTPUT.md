# SINDBAD WISP forward runs: output

This document describes the output of the WISP fire forward runs: where it is written,
how folders and files are named, which variables are saved, and which figures and
comparison files are made.

The runs are made on SLURM, one job per entity, by `slurm_scripts/entity_forward_exp.jl`.
How to set up and submit them is described in `slurm_scripts/README_SLURM.md`. The
experiment setup is in `setups/WISP/`: the experiment, forcing, model
structure and parameter jsons.

## Locations on the cluster

| What | Path | Setting in `env.sh` |
|---|---|---|
| Output | `/Net/Groups/BGI/tscratch/skoirala/WISP_DD_fwd_PFT` | `SINDBAD_OUTPUT`, named after `SINDBAD_EXP_NAME` |
| Forcing of the entities | `/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/wisp_eos_q10_med10_v1/outputs/base_v5` | `SINDBAD_ENTITIES` |
| Parameters, PFT class map | `/Net/Groups/BGI/tscratch/skoirala/WR_DD_PFT/params_by_pft_WR_DD_PFT.json` and `.zarr` | `SINDBAD_PARAMETERS`, `SINDBAD_PARAMETER_MAP` |

The forcing of an entity is in a folder of its own, e.g.
`base_v5/MED_ES_01_frozen_v1/daily_master_dev_v5_MED_ES_01_frozen_v1.nc`. The entity is
named without the `_frozen_v1` suffix in the output, e.g. `MED_ES_01`. The runs cover
2014-01-12 to 2023-12-31, the period of the forcing files.

In this document, `$SINDBAD_OUTPUT` stands for the output path above.

## 1. Fire experiments

Every entity is run up to three times. The runs differ only in the forcing of
`f_burnt_area`, the burnt area fraction that drives the fire module:

| Experiment | Source variable of `f_burnt_area` | Scalar | Meaning |
|---|---|---|---|
| `No_Fire` | `viirs_fire_binary` | 0.0 | Fire turned off |
| `VIIRS_Fire` | `viirs_fire_binary` | 0.99 | Fire from VIIRS active-fire detections |
| `WISP_Fire` | `WISP_fire_binary` | 0.99 | Fire from the WISP prediction |

The scalar is `source_to_sindbad_unit` of `f_burnt_area`: the forcing is multiplied by
it. Each experiment is switched on with `SINDBAD_DO_NO_FIRE`, `SINDBAD_DO_VIIRS_FIRE` and
`SINDBAD_DO_WISP_FIRE` in `env.sh`. The source variables are set by
`SINDBAD_VIIRS_FIRE_VAR` and `SINDBAD_WISP_FIRE_VAR`.

> **Note.** In the current forcing files (`*_frozen_v1.nc`), `WISP_fire_binary` is a
> placeholder that is an exact copy of `viirs_fire_binary`. Until real WISP predictions
> are in the files, `VIIRS_Fire` and `WISP_Fire` give identical results. Where a pixel
> has no fire days, all three experiments give identical results.

## 2. Folder layout

```
$SINDBAD_OUTPUT/
├── <entity>/                                   e.g. MED_ES_01
│   ├── output_WISP_<domain>_No_Fire/           one folder per fire experiment
│   │   ├── data/                               model output, NetCDF
│   │   ├── figure/                             maps and time series of this run
│   │   ├── settings/                           setup of this run
│   │   └── code/                               model code of this run
│   ├── output_WISP_<domain>_VIIRS_Fire/
│   ├── output_WISP_<domain>_WISP_Fire/
│   └── comparison/                             comparison of the fire experiments
├── entities.txt                                files of the job management,
├── logs/                                       see section 8
├── done/
└── sysimage_warmup/
```

### Naming

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
`sysimage_warmup/` uses a 4 x 4 block of pixels, tagged `px4x4`.

## 3. Model output: `data/`

One NetCDF file per output variable, named after the variable, plus `restart.nc`.

### Dimensions and coordinates

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

### Variables

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

### Carbon pools, `d_cEco`

The model uses the `GSI` carbon pool configuration:

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

### Water storage layers, `d_tws`

From the `pools.water` block of the model structure json:

| Index | Layer | Description |
|---|---|---|
| 1 to 7 | `soilW` | Soil water of the 7 soil layers, from top to bottom, with layer depths of 50, 200, 750, 1000, 3000, 5000 and 40000 mm in the model structure json |
| 8 | `groundW` | Groundwater |
| 9 | `snowW` | Snow water |
| 10 | `surfaceW` | Surface water |

### `restart.nc`

The state of the model at the last time step, for restarting a run: `cEco` with
dimensions `(lon, lat, d_cEco)` and `TWS` with `(lon, lat, d_tws)`. It has no `time`
dimension.

Spinup states are not saved (`store_spinup = false` in the experiment json), although
the model is spun up before every run (`spinup_TEM = true`).

### Global attributes

Each file records the run: `experiment` is the run name, e.g.
`MED_ES_01-full_No_Fire`. The other attributes are `domain`, `date`,
`simulation_by`, `host`, `machine`, `os` and `julia`.

### Reading the output

Python:

```python
import xarray as xr

run = "/Net/Groups/BGI/tscratch/skoirala/WISP_DD_fwd_PFT/MED_ES_01/output_WISP_MED_ES_01-full_No_Fire"
gpp = xr.open_dataset(f"{run}/data/gpp.nc").gpp
gpp_map = gpp.mean("time")                    # time-mean map
gpp_series = gpp.mean(["lat", "lon"])         # mean over all pixels per day

ceco = xr.open_dataset(f"{run}/data/cEco.nc").cEco
c_veg = ceco.isel(d_cEco=slice(0, 4)).sum("d_cEco")   # vegetation carbon, pools 1 to 4
```

Julia:

```julia
using YAXArrays, NetCDF
run = "/Net/Groups/BGI/tscratch/skoirala/WISP_DD_fwd_PFT/MED_ES_01/output_WISP_MED_ES_01-full_No_Fire"
gpp = open_dataset(joinpath(run, "data", "gpp.nc")).gpp
```

## 4. Setup of a run: `settings/` and `code/`

| File | Content |
|---|---|
| `settings/experiment_WISP_DD.json` | Copy of the experiment json in `setups/WISP/` |
| `settings/forcing_WISP_DD.json` | Copy of the forcing json |
| `settings/model_structure_WISP_DD.json` | Copy of the model structure json: the selected model of each process and the pools |
| `settings/params_by_pft_*.json` | Copy of the parameter json: parameters per PFT class |
| `settings/info.jld2` | The full Sindbad `info` of the run, with the settings as they were used. Saved because `save_info = true`. |
| `code/model_definitions.jl`, `code/model_functions.jl`, `code/model_structs.jl` | The code of the model as it was run |

The jsons in `settings/` are unchanged copies of the setup files, so they are the same
for all three experiments. They do not include the changes the job script makes through
`replace_info`: the run name, the output path, the forcing file, the period, the
parameter files and the `f_burnt_area` source variable and scalar of the experiment.
For example, the copy of the forcing json shows the `f_burnt_area` entry of the setup
file in every run folder, also in the `No_Fire` folder, whose run used the scalar 0.0.

The settings as they were used are in `settings/info.jld2`, under `info.experiment`.
The forcing settings are in `info.experiment.data_settings.forcing`. To see which fire
forcing a run used:

```julia
using Sindbad
using Sindbad.Setup: load        # JLD2's load, as Sindbad provides it
run = "/Net/Groups/BGI/tscratch/skoirala/WISP_DD_fwd_PFT/MED_ES_01/output_WISP_MED_ES_01-full_No_Fire"
info = load(joinpath(run, "settings", "info.jld2"))["info"]
fba = info.experiment.data_settings.forcing.variables.f_burnt_area
fba.source_variable, fba.source_to_sindbad_unit   # e.g. ("viirs_fire_binary", 0.0)
```

The run name is also in the `experiment` global attribute of every NetCDF file.

## 5. Figures of a run: `figure/`

Made when `SINDBAD_MAKE_PLOTS=1` in `env.sh`, by `plot_forward_run` in
`plot_forward_WISP_DD.jl`. All figures are PNG files.
Layered variables get one figure per layer, with the layer index after the variable
name. The tag at the end is the figure tag of the run, e.g. `MED_ES_01-full-No_Fire`.

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

### Forcing figures

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

## 6. Comparison of the fire experiments: `comparison/`

Made when two or more fire experiments run and `SINDBAD_MAKE_COMPARISON=1` in
`env.sh`, by `plot_runs_comparison` in `plot_forward_WISP_DD.jl`, once all experiments
of the entity are done. With only one experiment, nothing is compared. The tag of the
comparison files is the domain, e.g. `MED_ES_01-full`.

| File | Content |
|---|---|
| `<group>_<variable>[_<layer>]_mean_comparison_<domain>.png` | Comparison matrix of the time-mean maps, see below |
| `<group>_<variable>[_<layer>]_timeseries_comparison_<domain>.png` | Time series of all experiments, see below |
| `comparison_metrics_<domain>.csv` | Metrics of every pair of experiments, for every variable and layer |
| `summary_<experiment>.jls` | The time-mean maps and mean series of an experiment, saved so that a rerun of the job can compare experiments it skipped. Read with `Serialization.deserialize` in Julia. |

### Comparison matrix

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

### Time series comparison

The mean over all pixels of each experiment in three panels: daily values, monthly
means and annual means. Each experiment has its own color, line style and marker. The
legend of the daily panel gives `r2`, `bias` and `rmse` of each experiment against the
first one.

### Metrics file

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

## 7. Sizes and memory

The runs are not lazy (`run_lazy = false` in the experiment json), so the full output
of a run is kept in memory for the figures and the comparison. The comparison keeps
only the time-mean maps and the mean series of each experiment, which are small.

A 128 x 128 grid over the 10 years writes about 9 GB of compressed output per
experiment, so an entity with three experiments takes about three times that. The
NetCDF files are compressed with zlib at level 4, with shuffle, so the size depends on
the data. The fire diagnostics are mostly zeros where there is no fire and take little
space there.

## 8. Files of the job management

Next to the entity folders, `$SINDBAD_OUTPUT` holds the files of the job management:

| Path | Content |
|---|---|
| `entities.txt` | The entities, one per line. Line i is the entity of job or array task i. |
| `logs/fwd_<index>_<entity>_<jobid>.log` | Log of the job of one entity, in `jobs` mode |
| `logs/fwd_<task>_<arrayid>.log` | Log of one array task, in `array` mode |
| `logs/submit_watch.log` | Log of the watcher that submits the jobs in `jobs` mode |
| `done/<experiment>/<entity>` | Marker of a finished fire experiment of an entity. A rerun of the job skips it. |
| `done/<entity>` | Marker of an entity whose switched-on experiments are all done. The submit scripts skip it. |
| `sysimage_warmup/` | Output of the short `px4x4` run made while building the system image. It can be deleted. |

`SINDBAD_OVERWRITE=1` in `env.sh` runs entities and experiments again even when they
have done markers.
