#!/bin/bash
# All settings of the builds and the jobs of the three stages of a WISP experiment:
# site optimization, PFT optimization and the entity forward runs. Every script in
# slurm_scripts/ and combine_outputs/ reads this file. See README_SLURM.md.

# the WISP experiment names the output root of all its stages
export SINDBAD_WISP_EXPERIMENT="WISP_V1_202610"
# the stage of the scripts: site, pft or entity. A value set before, e.g.
# SINDBAD_STAGE=pft ./slurm_scripts/run.sh, is kept.
export SINDBAD_STAGE="${SINDBAD_STAGE:-site}"

# paths
# zsh has no BASH_SOURCE, but sets $0 to the sourced file
slurm_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export SINDBAD_REPO="$(dirname "$slurm_dir")"
export JULIA="/Net/Groups/Services/HPC_22/apps/julia/julia-1.13.1/bin/julia"
export SINDBAD_OUTPUT_ROOT="/Net/Groups/BGI/tscratch/skoirala/${SINDBAD_WISP_EXPERIMENT}"
export SINDBAD_SYSIMAGE_DIR="/Net/Groups/BGI/scratch/skoirala/sysimages"
# site data of the site and PFT stages, in the data depot
export SINDBAD_DATA_DEPOT="/Net/Groups/BGI/work_3/sindbad/data/WR_DD_202610/"
export SINDBAD_INPUT="FLUXNET_v2023_12_1D_202610_skoirala.zarr"

# the experiment names and output folders of the stages. The stage files take the
# input of a stage from the output of the one before it.
export SINDBAD_SITE_NAME="${SINDBAD_WISP_EXPERIMENT}_site"
export SINDBAD_PFT_NAME="${SINDBAD_WISP_EXPERIMENT}_pft"
export SINDBAD_ENTITY_NAME="${SINDBAD_WISP_EXPERIMENT}_entity"
export SINDBAD_SITE_OUTPUT="${SINDBAD_OUTPUT_ROOT}/site"
export SINDBAD_PFT_OUTPUT="${SINDBAD_OUTPUT_ROOT}/pft"
export SINDBAD_ENTITY_OUTPUT="${SINDBAD_OUTPUT_ROOT}/entity"
# the files that pass from one stage to the next, built by prepare_stage.sh
export SINDBAD_SITE_RESTART_OWN="${SINDBAD_SITE_OUTPUT}/restart_concatenated_${SINDBAD_SITE_NAME}.zarr"
export SINDBAD_PFT_PARAMETERS_OWN="${SINDBAD_PFT_OUTPUT}/params_by_pft_${SINDBAD_PFT_NAME}.json"
export SINDBAD_PFT_PARAMETER_MAP_OWN="${SINDBAD_PFT_OUTPUT}/params_by_pft_${SINDBAD_PFT_NAME}.zarr"

# system image: use "generic;cascadelake,clone_all" for older nodes; parallel
# precompilation fails on the cluster
export SINDBAD_CPU_TARGET="cascadelake"
export JULIA_NUM_PRECOMPILE_TASKS=1

# every job
export SINDBAD_PARTITION="work"
export SINDBAD_START_ATTEMPTS=20            # Julia start-up attempts on a node
export SINDBAD_START_MOVES=5                # moves to another node if Julia fails
export SINDBAD_EXCLUDE=""                   # nodes to avoid, e.g. node-r5-he34
export SINDBAD_OVERWRITE=0                  # 1 reruns domains that already finished
# 1 lets the next stage start from the domains that finished when some failed. The
# failed ones are listed. With 0 the next stage stops until all are done.
export SINDBAD_ALLOW_PARTIAL=0

# submitting: the share of the available resources for all jobs, and the time between
# submit rounds of the watcher, which reads this file at start, so restart the
# watcher after edits
export SINDBAD_USE_PERCENT=75
export SINDBAD_WATCH_MINUTES=10

# the jobs of run_all.sh that link the stages. A prep job combines the output of the
# stage before and builds the system image when needed. A watch job submits the jobs
# of the stage and waits until none is left in the queue, so its time covers the
# whole stage and must fit the time limit of the partition.
export SINDBAD_BUILD_CPUS=4
export SINDBAD_BUILD_MEM="32G"
export SINDBAD_BUILD_TIME="06:00:00"
export SINDBAD_WATCH_MEM="2G"
export SINDBAD_WATCH_TIME="7-00:00:00"

# settings that only some stage files set, cleared so that they do not leak from a
# stage sourced before in the same shell
unset SINDBAD_MAX_THREADS SINDBAD_ONE_THREAD_SITES SINDBAD_MEM_BASE SINDBAD_MEM_PER_SITE

# the experiment of the stage, its run script and the resources of one job
case "$SINDBAD_STAGE" in
    site|pft|entity) source "${slurm_dir}/stage_${SINDBAD_STAGE}.sh" ;;
    *) echo "SINDBAD_STAGE must be site, pft or entity, not ${SINDBAD_STAGE}" >&2; return 1 2> /dev/null || exit 1 ;;
esac

export SINDBAD_OUTPUT="${SINDBAD_OUTPUT_ROOT}/${SINDBAD_STAGE}"
export SINDBAD_SYSIMAGE="${SINDBAD_SYSIMAGE_DIR}/sindbad_${SINDBAD_EXP_NAME}.so"
