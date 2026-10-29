#!/bin/bash
# PFT level optimization, one PFT per job. All sites of the PFT run in parallel on
# the threads of the job, so the cpus and memory of a job grow with its number of
# sites. Sourced by env.sh when SINDBAD_STAGE is pft.

# the name sets the image, jobs and tmux session and the output folders of the runs;
# the experiment is in setups/, the scripts in slurm_scripts/ and the warmup
# optimizer next to the experiment
export SINDBAD_EXP_NAME="$SINDBAD_PFT_NAME"
export SINDBAD_EXPERIMENT="PFT/experiment_refactored_WRDY_SoilDepth_PFT.json"
export SINDBAD_RUN_SCRIPT="pft_opti_exp.jl"
export SINDBAD_WARMUP_SCRIPT="pft_opti_warmup.jl"
export SINDBAD_WARMUP_OPTIMIZER="CMAEvolutionStrategy_CMAES_warmup.json"

# pools of the sites after spinup. prepare_stage.sh combines them from the site stage
# of this WISP experiment. Another restart zarr, e.g.
# /Net/Groups/BGI/tscratch/skoirala/WR_DD_SI_RS/restart_concatenated_WR_DD_SI_RS.zarr,
# is used as it is.
export SINDBAD_SITE_RESTART="$SINDBAD_SITE_RESTART_OWN"
export SINDBAD_MIN_SITES=1                  # PFTs with fewer sites are not optimized

# one job, sized by the number of sites n of its PFT: cpus = n up to
# SINDBAD_ONE_THREAD_SITES sites, else ceil(2n / 3), at most SINDBAD_MAX_THREADS, and
# memory = SINDBAD_MEM_BASE + n * SINDBAD_MEM_PER_SITE
export SINDBAD_ONE_THREAD_SITES=16
export SINDBAD_MAX_THREADS=32               # at most the cpus of one node
export SINDBAD_MEM_BASE="8G"
export SINDBAD_MEM_PER_SITE="256M"
export SINDBAD_TIME="2-00:00:00"

# the job sizes differ, so only jobs, no array
export SINDBAD_SUBMIT_MODE="jobs"
