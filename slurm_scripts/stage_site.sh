#!/bin/bash
# Site level optimization, one site per job, with CMA-ES evaluating its population on
# the threads of the job. Sourced by env.sh when SINDBAD_STAGE is site.

# the name sets the image, jobs and tmux session and the output folders of the runs;
# the experiment is in setups/, the scripts in slurm_scripts/ and the warmup
# optimizer next to the experiment
export SINDBAD_EXP_NAME="$SINDBAD_SITE_NAME"
export SINDBAD_EXPERIMENT="Site/experiment_refactored_WRDY_SoilDepth_MT.json"
export SINDBAD_RUN_SCRIPT="site_opti_exp.jl"
export SINDBAD_WARMUP_SCRIPT="site_opti_warmup.jl"
export SINDBAD_WARMUP_OPTIMIZER="CMAEvolutionStrategy_CMAES_warmup_MT.json"

# one job, the same for every site
export SINDBAD_THREADS=8                    # threads and cpus, divides popsize of CMA-ES
export SINDBAD_MEM="12G"
export SINDBAD_HEAP_HINT="8G"               # Julia heap size hint, below SINDBAD_MEM
export SINDBAD_TIME="2-00:00:00"

# jobs: one job per site, refilled by a watcher; array: one job array, limit fixed at
# submit
export SINDBAD_SUBMIT_MODE="jobs"
