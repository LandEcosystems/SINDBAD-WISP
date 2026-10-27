# WISP forward runs on SLURM

Runs the forward model for many entities on the cluster, one entity per job, with the
setup of `run_forward_spatial_WISP_DD.jl`: the whole lat/lon grid of the entity's
forcing file, and parameters per pixel from the PFT class map in
`setups/WROASTED_WISP_DD/params/`. Each entity gets `SINDBAD_THREADS` (P) cpus.

`SINDBAD_SUBMIT_MODE` in `env.sh` sets how many run at once:

- **jobs** (default): one job per entity, submitted by a watcher in tmux. Every
  `SINDBAD_WATCH_MINUTES` it submits as many entities as fit in
  `SINDBAD_USE_PERCENT` (75) % of the free cpus and memory of the partition.
- **array**: one job array, with at most `SINDBAD_MAX_JOBS` (n) entities running, or
  as many as SLURM finds room for when n is empty.

## Files

| File | Role |
|---|---|
| `slurm_scripts/env.sh` | **All settings**: paths, entities, parameters, P, share, memory, time |
| `slurm_scripts/build_image.sh` | Builds the system image on the login node |
| `slurm_scripts/build_sysimage.jl` | The build, with `forward_entity_warmup.jl` compiled in |
| `slurm_scripts/forward_entity_warmup.jl` | 4 x 4 pixel run of the first entity during the build |
| `slurm_scripts/run_entities.sh` | Entry point. Submits in the `SINDBAD_SUBMIT_MODE` way |
| `slurm_scripts/submit_entities_as_jobs.sh` | `jobs` mode: one job per entity, `--watch` keeps filling the share |
| `slurm_scripts/submit_entities.sh` | `array` mode: all entities as one job array |
| `slurm_scripts/slurm_job_limit.sh` | Job size and the resource share |
| `slurm_scripts/forward_entity.sbatch` | The job: runs one entity, retries Julia start-up failures |
| `slurm_scripts/forward_entity_exp.jl` | The Julia run of one entity |

## Before the first run

- The parameters are the PFT class map of the TRENDY PFT optimization, from
  `concat_pft_parameters.jl`, in `/Net/Groups/BGI/tscratch/skoirala/WR_DD_PFT/`. Set
  `SINDBAD_PARAMETERS` and `SINDBAD_PARAMETER_MAP` in `env.sh` to use another one. The
  run stops at the start when the json or the zarr does not exist.
- `PackageCompiler` must be in your default Julia environment on the cluster.

## Running

From the repository root on a login node:

1. Check the settings in `slurm_scripts/env.sh`.
2. Build the system image. Rebuild after any change in `dev/` or `Manifest.toml`.
   Changing the setup jsons needs no rebuild.

   ```bash
   ./slurm_scripts/build_image.sh
   ```

3. Dry run, in the foreground. It writes `$SINDBAD_OUTPUT/entities.txt`, prints the
   settings, the resource budget and the `sbatch` commands of the first round, and
   lets SLURM validate the first one with `--test-only`.

   ```bash
   ./slurm_scripts/run_entities.sh --dry-run
   ```

4. Start it. In jobs mode the watcher runs in a tmux session on this login node and
   prints the host name. In array mode it submits the array and exits.

   ```bash
   ./slurm_scripts/run_entities.sh            # all entities that are not done
   ./slurm_scripts/run_entities.sh 5,17,30-40 # or lines of entities.txt
   ```

| Action | Command |
|---|---|
| Follow the watcher | `tail -f $SINDBAD_OUTPUT/logs/submit_watch.log` |
| Attach / detach | `tmux attach -t $SINDBAD_EXP_NAME`, then `Ctrl-b d` |
| Stop submitting | `tmux kill-session -t $SINDBAD_EXP_NAME` (running jobs continue) |
| See jobs | `squeue -u $USER` |
| Follow an entity | `tail -f $SINDBAD_OUTPUT/logs/fwd_<index>_<entity>_<jobid>.log` |

## How it works

- **Entities.** Every folder `<entity><SINDBAD_FORCING_SUFFIX>` in `SINDBAD_ENTITIES`
  with a forcing file `<SINDBAD_FORCING_PREFIX><entity><SINDBAD_FORCING_SUFFIX>.nc` is
  an entity, e.g. `MED_ES_01_frozen_v1/daily_master_dev_v5_MED_ES_01_frozen_v1.nc`. The
  entity is named without the suffix, `MED_ES_01`, in `entities.txt` and the output.
  Line i of `entities.txt` is the entity of job or array task i.
- **Resource share.** Each round, `slurm_job_limit.sh` adds the idle cpus and
  unallocated memory of the partition, on nodes in `idle` or `mix` state, to what the
  running jobs of the experiment hold, and takes `SINDBAD_USE_PERCENT` % of both as the
  budget. Entities are submitted while the cpus and memory of the running and pending
  jobs of the experiment stay within it. One job is always allowed.
- **Watching.** The watcher skips entities with a done marker, entities with a job
  `<SINDBAD_EXP_NAME>_<entity>` already in the queue, and entities it submitted
  before, so a failing entity is not retried in a loop. It exits once every entity is
  done or submitted. To retry the failed ones, start it again after it has finished.
- **Fire experiments.** Each job runs the fire experiments switched on in `env.sh`
  one after another, as in `run_forward_spatial_WISP_DD_fire.jl`. They differ only
  in the source variable of `f_burnt_area` and its scalar `source_to_sindbad_unit`:
  `No_Fire` scales `SINDBAD_VIIRS_FIRE_VAR` by 0.0, `VIIRS_Fire` scales it by 0.99, and
  `WISP_Fire` scales `SINDBAD_WISP_FIRE_VAR` by 0.99. Switch them with
  `SINDBAD_DO_NO_FIRE`, `SINDBAD_DO_VIIRS_FIRE` and `SINDBAD_DO_WISP_FIRE`. A job of
  all three takes about three times as long as one run, so set `SINDBAD_TIME` for it.
- **Comparison.** With `SINDBAD_MAKE_COMPARISON=1` and two or more experiments
  switched on, the job compares them once all are done. Each experiment saves the
  time-mean maps and space-mean series of its variables in
  `<entity>/comparison/summary_<experiment>.jls`, so a rerun that skips the done
  experiments still compares all of them. `<entity>/comparison/` gets, per variable,
  an n x n panel of the time-mean maps with the maps on the diagonal, the differences
  above and the histograms with r, r2, bias and rmse below, and a time series plot with
  one line per experiment. The metrics of every pair are saved in
  `comparison_metrics_<domain>.csv`. A failure of the comparison is reported in the
  log but does not fail the entity.
- **Output.** The output is organized as in `run_forward_spatial_WISP_DD_fire.jl`.
  Each fire experiment of an entity writes its output and figures to
  `$SINDBAD_OUTPUT/<entity>/output_WISP_<entity>-full_<experiment>/`, e.g.
  `MED_FR_01/output_WISP_MED_FR_01-full_No_Fire/`, and the comparison goes to
  `$SINDBAD_OUTPUT/<entity>/comparison/`.
  The figures of `plot_forward_WISP_DD.jl` are made when `SINDBAD_MAKE_PLOTS=1`. A
  failure in the plots is reported in the log but does not fail the entity.
- **Done markers.** A finished fire experiment writes
  `$SINDBAD_OUTPUT/done/<experiment>/<entity>`, so a rerun of the job skips it. Once
  all switched-on experiments are done, the entity writes
  `$SINDBAD_OUTPUT/done/<entity>`, and a job of a done entity exits at once.
  `SINDBAD_OVERWRITE=1` runs them again. When an experiment is switched on later,
  remove the `done/<entity>` markers, or the submit scripts skip those entities. Runs
  made before the fire experiments have only `done/<entity>`, so use a new
  `SINDBAD_EXP_NAME` for the fire runs.
- **Memory.** `run_lazy` is false in the experiment json, so the output of the whole
  grid is kept in memory for the plots and the comparison. A 128 x 128 grid over 10
  years writes about 9 GB of compressed output, and `SINDBAD_MEM=64G` leaves room for
  it. Check `sacct -j <jobid> -o MaxRSS` after the first entities and lower it, so
  more entities fit in the share.
- **Start-up retries.** Julia sometimes fails to start on a node. A failure within 2
  minutes is retried up to `SINDBAD_START_ATTEMPTS` times. Then the entity is
  resubmitted as a job of its own named `<SINDBAD_EXP_NAME>_<entity>` away from that
  node, up to `SINDBAD_START_MOVES` times. The watcher counts it like its own jobs.
