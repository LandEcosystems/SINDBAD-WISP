#!/bin/bash
# All settings of the build and the jobs of the WISP forward runs, one entity per job.
# Every script in slurm_scripts/ reads this file. See README_SLURM.md.

# paths
# zsh has no BASH_SOURCE, but sets $0 to the sourced file
slurm_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export SINDBAD_REPO="$(dirname "$slurm_dir")"
export JULIA="/Net/Groups/Services/HPC_22/apps/julia/julia-1.13.1/bin/julia"

# the name sets the image, output folder and job names, so experiments with different
# names run side by side without mixing
export SINDBAD_EXP_NAME="WISP_DD_fwd_PFT"
# the experiment in setups/
export SINDBAD_EXPERIMENT="WROASTED_WISP_DD/experiment_WISP_DD.json"

# entities: each has a folder in SINDBAD_ENTITIES with its forcing file named
# <SINDBAD_FORCING_PREFIX><entity>.nc, e.g., daily_master_dev_v5_MED_PT_01.nc
export SINDBAD_ENTITIES="/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/wisp_eos_q10_med10_v1/outputs/base_v5"
export SINDBAD_FORCING_PREFIX="daily_master_dev_v5_"
# period of the forcing files, which replaces the dates of the experiment json
export SINDBAD_DATE_BEGIN="2014-01-12T00:00:00"
export SINDBAD_DATE_END="2023-12-31T00:00:00"

# parameters, absolute or relative to the folder of the experiment json. An empty
# SINDBAD_PARAMETERS runs the default parameters. A csv gives one parameter vector for
# all pixels. A json gives a parameter map. A non-empty SINDBAD_PARAMETER_MAP is the
# zarr that replaces the data_path of the json, and an empty one keeps that data_path.
# These are the PFT class map of the TRENDY PFT optimization WR_DD_PFT.
export SINDBAD_PARAMETERS="/Net/Groups/BGI/tscratch/skoirala/WR_DD_PFT/params_by_pft_WR_DD_PFT.json"
export SINDBAD_PARAMETER_MAP="/Net/Groups/BGI/tscratch/skoirala/WR_DD_PFT/params_by_pft_WR_DD_PFT.zarr"

# run options: 1 for true, 0 for false. A lazy run uses less memory, but the system
# image is compiled for the run_lazy of the build, so rebuild after changing it.
export SINDBAD_RUN_LAZY=0
export SINDBAD_MAKE_PLOTS=1
export SINDBAD_OVERWRITE=0                  # 1 reruns entities that already finished

# one job per entity with P cpus
export SINDBAD_THREADS=48                   # P, the threads of the run of one entity

# submitting: jobs is one job per entity, refilled by a watcher while the experiment
# holds at most SINDBAD_USE_PERCENT % of the available cpus and memory of the
# partition. array is one job array with at most SINDBAD_MAX_JOBS entities running,
# or no limit when it is empty.
export SINDBAD_SUBMIT_MODE="jobs"
export SINDBAD_USE_PERCENT=75
# the time between submit rounds of the watcher, which reads this file at start, so
# restart the watcher after edits
export SINDBAD_WATCH_MINUTES=10
export SINDBAD_MAX_JOBS=""                  # n, array mode only
export SINDBAD_MEM="64G"
export SINDBAD_HEAP_HINT="48G"              # Julia heap size hint, below SINDBAD_MEM
export SINDBAD_TIME="1-00:00:00"
export SINDBAD_PARTITION="work"

# system image: use "generic;cascadelake,clone_all" for older nodes; parallel
# precompilation fails on the cluster
export SINDBAD_CPU_TARGET="cascadelake"
export JULIA_NUM_PRECOMPILE_TASKS=1

# Julia start-up failures on a node
export SINDBAD_START_ATTEMPTS=20            # Julia start-up attempts on a node
export SINDBAD_START_MOVES=5                # moves to another node if Julia fails
export SINDBAD_EXCLUDE=""                   # nodes to avoid, e.g. node-r5-he34

export SINDBAD_OUTPUT="/Net/Groups/BGI/tscratch/skoirala/${SINDBAD_EXP_NAME}"
export SINDBAD_SYSIMAGE="/Net/Groups/BGI/scratch/skoirala/sysimages/sindbad_${SINDBAD_EXP_NAME}.so"
