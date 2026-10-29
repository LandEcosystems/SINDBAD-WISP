# Combining the outputs of the runs

The scripts here combine the files that each run of a stage writes, one per output
folder, into one file for the next stage. They read files only and run no model, so
they take minutes on the login node.

The two combines the chain needs run on their own: `slurm_scripts/prepare_stage.sh`
runs `combine_restarts` before the PFT stage and `pft_parameter_map` before the entity
stage, once the stage before is done. See `slurm_scripts/README_SLURM.md`. The commands
below are for running them by hand, e.g. again after deleting the combined file, or
for the optional ones.

## The workflow

```
site stage ─┬─ combine_restarts ───► site/restart_concatenated_<site exp>.zarr
            │                           │  SINDBAD_SITE_RESTART of the PFT stage
            │                           ▼
            └─ site_parameter_map ─► site/params_concatenated_<site exp>.zarr/.json
                                        (optional) parameters per site
                                     PFT stage ─┬─ pft_parameter_map ─► pft/params_by_pft_<pft exp>.zarr/.json
                                                │                          SINDBAD_PFT_PARAMETERS of the entity stage
                                                └─ combine_restarts ──► pft/restart_concatenated_<pft exp>.zarr
                                                                           (optional) pools with the PFT parameters
```

All paths are in the output root of the WISP experiment,
`SINDBAD_OUTPUT_ROOT`, and the experiment names are `<wisp_experiment>_site` and
`<wisp_experiment>_pft`.

| Script | Writes | Used for |
|---|---|---|
| `combine_restarts` on the site stage | `restart_concatenated_<exp>.zarr`: the pools of every site after its spinup | `SINDBAD_SITE_RESTART` of the PFT stage, so every site of a PFT starts from its own pools |
| `site_parameter_map` on the site stage | `params_concatenated_<exp>.zarr` and `.json`: the parameters of every site, on the dimension `site` | optional, the `parameters` config file of a forward run on those sites |
| `pft_parameter_map` on the PFT stage | `params_by_pft_<exp>.zarr` and `.json`: one value per parameter and PFT, on the dimension `f_pft` | `SINDBAD_PFT_PARAMETERS` of the entity stage: each pixel takes the parameters of its `f_pft` |
| `combine_restarts` on the PFT stage | `restart_concatenated_<exp>.zarr`: the pools of every site spun up with its PFT parameters | optional, e.g. the start of a forward run on the sites |

## Running

From the repository root, with the stage whose output is combined:

```bash
SINDBAD_STAGE=site ./combine_outputs/run_combine.sh combine_restarts
SINDBAD_STAGE=site ./combine_outputs/run_combine.sh site_parameter_map
SINDBAD_STAGE=pft  ./combine_outputs/run_combine.sh pft_parameter_map
SINDBAD_STAGE=pft  ./combine_outputs/run_combine.sh combine_restarts
```

`run_combine.sh` runs the script with `JULIA` and `SINDBAD_SYSIMAGE` of
`slurm_scripts/env.sh`, the Julia and the Sindbad of the runs, and not with whatever
`julia` is on the PATH of the session. Without more arguments the scripts take
`SINDBAD_OUTPUT` and `SINDBAD_EXP_NAME` of the stage. Other runs are combined by
giving their output folder and experiment name, e.g.
`run_combine.sh combine_restarts /Net/Groups/BGI/tscratch/skoirala/WR_DD_SI_RS WR_DD_SI_RS`.
The experiment name is the one that ends the names of the output folders,
`output_<site>_<exp>` for site runs and `output_<wisp_experiment>_PFT_<k>_<exp>` for
PFT runs. `combine_restarts.jl` and `site_parameter_map.jl` take the format, `zarr`
(default) or `nc`, as a third argument. `pft_parameter_map.jl` takes a PFT as the third
argument, `SINDBAD_DEFAULT_PFT` in the chain, whose parameters the locations of PFTs
without parameters then use; without it they use the default of each parameter. A PFT
whose run failed has no parameters.

## File names

Until October 2026 Sindbad named the files of a run after the experiment and domain,
e.g. `data/restart_<exp>_<site>.zarr` and `optimization/params_<exp>_<site>.zarr`.
Since then they are `data/restart.zarr` and `optimization/params.zarr`. The scripts
find both, so runs from before and after the change can be combined. The log lists
the runs without a file, e.g. those that failed or are still running.
