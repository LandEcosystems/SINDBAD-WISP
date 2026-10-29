# WISP experiments on SLURM

A WISP experiment runs in three stages, each on its own jobs, all with the Sindbad in
`dev/` and all output under one root:

| Stage | Domain of one job | Setup | Writes, under `SINDBAD_OUTPUT_ROOT` |
|---|---|---|---|
| `site` | a FLUXNET site, CMA-ES on the threads of the job | `setups/Site/` | `site/output_<site>_<exp>/`, with the pools after spinup in `data/restart.zarr` |
| `pft` | a PFT, all its sites at once, starting from their site pools | `setups/PFT/` | `pft/output_<wisp_experiment>_PFT_<k>_<exp>/` |
| `entity` | a WISP entity, the whole 350 m grid, parameters by PFT, the fire experiments | `setups/WISP/` | `entity/<entity>/`, see `README_SINDBAD_WISP_OUTPUT.md` |

Between the stages the outputs are combined (`combine_outputs/`):

- **Before `pft`:** the site pools go into `site/restart_concatenated_<site exp>.zarr`.
- **Before `entity`:** the PFT parameters go into the lookup map `pft/params_by_pft_<pft exp>.json` and `.zarr`, which gives each pixel the parameters of its `f_pft`.

The handoffs are built when the next stage starts.

`SINDBAD_OUTPUT_ROOT` is `/Net/Groups/BGI/tscratch/skoirala/<SINDBAD_WISP_EXPERIMENT>`.
The experiment names of the stages are `<SINDBAD_WISP_EXPERIMENT>_site`, `_pft` and
`_entity`.

## Files

| File | Role |
|---|---|
| `env.sh` | **Shared settings**: WISP experiment, stage, paths, partition, share, chain jobs |
| `stage_site.sh`, `stage_pft.sh`, `stage_entity.sh` | **Settings of a stage**: experiment, scripts, input from the stage before, job size |
| `run_all.sh` | Runs the stages one after another as dependent jobs |
| `run.sh` | Runs one stage. Submits in the `SINDBAD_SUBMIT_MODE` way |
| `prepare_stage.sh` | Checks the stage before and combines its output, checks the system image |
| `build_image.sh`, `build_sysimage.jl` | Builds the system image of a stage, with its warmup run compiled in |
| `site_opti_warmup.jl`, `pft_opti_warmup.jl`, `entity_forward_warmup.jl` | The warmup runs |
| `submit_as_jobs.sh` | `jobs` mode: one job per domain, `--watch` keeps filling the share |
| `submit_as_array.sh` | `array` mode: all domains as one job array, for stages with a fixed job size |
| `slurm_job_limit.sh` | Job size and the resource share |
| `run.sbatch` | A job of a stage: runs one domain, retries Julia start-up failures |
| `chain.sbatch` | A job of `run_all.sh`: the prep or watch step of a stage |
| `common.jl` | Paths, `domains.txt` and done markers, shared by the Julia run scripts |
| `site_opti_exp.jl`, `pft_opti_exp.jl`, `entity_forward_exp.jl` | The Julia run of one domain |

## Before the first run

- `PackageCompiler` must be in your default Julia environment on the cluster.
- Check `env.sh` and the three stage files. The entity stage file has the entities, the forcing period and the fire switches.
- The `dev/` checkouts and `Manifest.toml` are shared by all stages. After changing them, rebuild the images.

## All stages as dependent jobs

From the repository root on a login node:

```bash
./slurm_scripts/run_all.sh --dry-run          # prints the sbatch commands
./slurm_scripts/run_all.sh                    # site, pft and entity
./slurm_scripts/run_all.sh --from pft         # reuse the site stage that is there
./slurm_scripts/run_all.sh --to pft           # stop after the PFT stage
```

Each stage gets two jobs, and each job waits for the one before with `afterok`:

| Job | Resources | Does |
|---|---|---|
| `<wisp_experiment>_prep_<stage>` | `SINDBAD_BUILD_CPUS`, `SINDBAD_BUILD_MEM`, `SINDBAD_BUILD_TIME` | `build_image.sh --if-needed`: combines the output of the stage before, then builds the system image if it is missing or older than `Manifest.toml` or a commit in `dev/` |
| `<wisp_experiment>_watch_<stage>` | 1 cpu, `SINDBAD_WATCH_MEM`, `SINDBAD_WATCH_TIME` | `submit_as_jobs.sh --until-done`: submits the jobs of the stage as the watcher of `run.sh` does, and waits until none is left in the queue |

How the watch job ends:
- **All domains done:** it succeeds and the next stage starts.
- **Some domains failed:** it lists them and fails. SLURM then cancels the rest of the chain.
  - To continue anyway, rerun the failed domains with `SINDBAD_STAGE=<stage> ./slurm_scripts/run.sh` and start the chain again with `--from <next stage>`.
  - Or set `SINDBAD_ALLOW_PARTIAL=1` before starting, to go on with the done domains.
- **Time limit:** the watch job runs as long as its stage, so `SINDBAD_WATCH_TIME` must fit the time limit of the partition.

Logs of the chain are in `$SINDBAD_OUTPUT_ROOT/logs/chain/`. Follow them with `squeue -u $USER -n <job names>`, which `run_all.sh` prints.

The watch job submits jobs from a compute node. If the cluster does not allow that,
`run_all.sh --tmux` runs the same steps one after another in a tmux session on the
login node.

## One stage at a time

```bash
SINDBAD_STAGE=pft ./slurm_scripts/build_image.sh       # once, see below
SINDBAD_STAGE=pft ./slurm_scripts/run.sh --dry-run
SINDBAD_STAGE=pft ./slurm_scripts/run.sh               # all domains that are not done
SINDBAD_STAGE=pft ./slurm_scripts/run.sh 5,17,30-40    # or lines of domains.txt
```

`SINDBAD_STAGE` can also be set in `env.sh`.

Both `run.sh` and `build_image.sh` first prepare the input of the stage from the stage before:
- **New input:** if the input does not exist yet, it is combined, once every domain of the stage before has a done marker.
- **Stage before not finished:** they stop and print how to finish it, or, if it never ran, how to run it.
- **Existing input:** it is used as it is. Delete it to combine again.
- **Input from elsewhere:** set `SINDBAD_SITE_RESTART` in `stage_pft.sh`, or `SINDBAD_PFT_PARAMETERS` and `SINDBAD_PFT_PARAMETER_MAP` in `stage_entity.sh`, e.g. to files of a TRENDY run. Those are used as they are, without checking a stage.
- **Dry run:** `run.sh --dry-run` only says what it would combine.

**System images.** Each stage has its own image,
`$SINDBAD_SYSIMAGE_DIR/sindbad_<stage exp>.so`, with its warmup run compiled in.
- **When to build:** once before the first run of a stage, and again after `dev/` or `Manifest.toml` changes. Changing the setup jsons or the settings needs no rebuild.
- **No image:** `run.sh` stops with the build command.
- **Old image:** if it is older than `Manifest.toml` or the last commit in a `dev/` checkout, `run.sh` warns.
- **Where it is built:** the jobs never compile. The build runs on the login node, or in the prep job of `run_all.sh`.

| Action | Command |
|---|---|
| Follow the watcher | `tail -f $SINDBAD_OUTPUT_ROOT/<stage>/logs/submit_watch.log` |
| Attach / detach | `tmux attach -t <stage exp>`, then `Ctrl-b d` |
| Stop submitting | `tmux kill-session -t <stage exp>` (running jobs continue) |
| See jobs | `squeue -u $USER` |
| Follow a domain | `tail -f $SINDBAD_OUTPUT_ROOT/<stage>/logs/<stage>_<index>_<domain>_<jobid>.log` |

## How it works

- **Domains.** `<run script> --list-domains` writes `<stage>/domains.txt` with one
  `<domain> <n_sites>` line per domain. Line i is the domain of job or array task i.
  - **Site stage:** the sites of the input zarr.
  - **PFT stage:** the PFTs with at least `SINDBAD_MIN_SITES` sites in the site restart file. The job of a PFT grows with its sites, see `stage_pft.sh`.
  - **Entity stage:** every folder `<entity><SINDBAD_FORCING_SUFFIX>` in `SINDBAD_ENTITIES` with a forcing file `<SINDBAD_FORCING_PREFIX><entity><SINDBAD_FORCING_SUFFIX>.nc`, e.g. `MED_ES_01_frozen_v1/daily_master_dev_v5_MED_ES_01_frozen_v1.nc`. The entity is named without the suffix, `MED_ES_01`.
- **Experiment name.** The site and PFT runs take `experiment.basics.name` from
  `SINDBAD_EXP_NAME`, so the output folders, the combine scripts and the jobs always
  agree, whatever name the json has.
- **Resource share.** Each round, `slurm_job_limit.sh` works out a budget:
  - It adds the idle cpus and unallocated memory of the partition, on nodes in `idle` or `mix` state, to what the running jobs of the stage hold.
  - It takes `SINDBAD_USE_PERCENT` % of both.
  - Domains are submitted, the largest first, while the cpus and memory of the running and pending jobs of the stage stay within the budget. One job is always allowed.
- **Watching.** The watcher skips:
  - domains with a done marker;
  - domains whose job `<stage exp>_<domain>` is already in the queue;
  - domains it submitted before, so a failing domain is not retried in a loop.

  With `--watch` it exits once every domain is done or submitted. With `--until-done` it also waits for the queue to empty and reports the failed domains. To retry the failed ones, start it again after it has finished.
- **Done markers.** A finished domain writes `<stage>/done/<domain>`, and a job of a
  done domain exits at once. `SINDBAD_OVERWRITE=1` runs them again.
- **Start-up retries.** Julia sometimes fails to start on a node.
  - A failure within 2 minutes is retried up to `SINDBAD_START_ATTEMPTS` times.
  - After that the domain is resubmitted as a job of its own, named `<stage exp>_<domain>`, away from that node, up to `SINDBAD_START_MOVES` times.
  - The watcher counts it like its own jobs.

### Entity stage

- **Fire experiments.** Each job runs the fire experiments switched on in
  `stage_entity.sh` one after another, as in `run_forward_spatial_WISP_DD_fire.jl`.
  They differ only in the source variable of `f_burnt_area` and its scalar
  `source_to_sindbad_unit`:

  | Experiment | Source variable | Scalar | Switch |
  |---|---|---|---|
  | `No_Fire` | `SINDBAD_VIIRS_FIRE_VAR` | 0.0 | `SINDBAD_DO_NO_FIRE` |
  | `VIIRS_Fire` | `SINDBAD_VIIRS_FIRE_VAR` | 0.99 | `SINDBAD_DO_VIIRS_FIRE` |
  | `WISP_Fire` | `SINDBAD_WISP_FIRE_VAR` | 0.99 | `SINDBAD_DO_WISP_FIRE` |

  A job of all three takes about three times as long as one run, so set `SINDBAD_TIME` for it.
- **Parameters.** By default these are the PFT lookup map of the PFT stage, and pixels of PFTs without parameters get the defaults.
  - `SINDBAD_DEFAULT_PFT` gives those pixels the parameters of another PFT instead.
  - The entity model has fire models and an LAI based fAPAR that the site and PFT model does not have. Their parameters keep their defaults.
- **Comparison.** With `SINDBAD_MAKE_COMPARISON=1` and two or more experiments switched on, the job compares them once all are done.
  - **Summaries:** each experiment saves the time-mean maps and space-mean series of its variables in `<entity>/comparison/summary_<experiment>.jls`. A rerun that skips the done experiments therefore still compares all of them.
  - **Maps:** per variable, `<entity>/comparison/` gets an n x n panel of the time-mean maps:
    - the maps on the diagonal;
    - the differences above it;
    - the histograms with r, r2, bias and rmse below it.
  - **Time series:** per variable, a plot with one line per experiment.
  - **Metrics:** for every pair, in `comparison_metrics_<domain>.csv`.
  - **Failures:** a failure of the comparison is reported in the log but does not fail the entity.
- **Output.**
  - Each fire experiment of an entity writes its output and figures to `entity/<entity>/output_WISP_<entity>-full_<experiment>/`.
  - The comparison goes to `entity/<entity>/comparison/`.
  - The figures of `plot_forward_WISP_DD.jl` are made when `SINDBAD_MAKE_PLOTS=1`. A failure in the plots is reported in the log but does not fail the entity.
- **Done markers per experiment.** A finished fire experiment writes `entity/done/<experiment>/<entity>`, so a rerun of the job skips it. `done/<entity>` follows once all switched-on experiments are done. When an experiment is switched on later, remove the `done/<entity>` markers, or the submitters skip those entities.
- **Memory.** `run_lazy` is false in the experiment json, so the output of the whole grid is kept in memory for the plots and the comparison.
  - A 128 x 128 grid over 10 years writes about 9 GB of compressed output, and `SINDBAD_MEM=64G` leaves room for it.
  - Check `sacct -j <jobid> -o MaxRSS` after the first entities and lower the memory, so more entities fit in the share.
