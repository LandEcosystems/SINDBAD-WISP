#!/bin/bash
# Forward runs of the WISP entities, one entity per job, on the whole lat/lon grid of
# its forcing file. Sourced by env.sh when SINDBAD_STAGE is entity.

# the name sets the image, jobs and tmux session; the experiment is in setups/ and
# the scripts in slurm_scripts/
export SINDBAD_EXP_NAME="$SINDBAD_ENTITY_NAME"
export SINDBAD_EXPERIMENT="WISP/experiment_WISP_DD.json"
export SINDBAD_RUN_SCRIPT="entity_forward_exp.jl"
export SINDBAD_WARMUP_SCRIPT="entity_forward_warmup.jl"

# entities: each has a folder <entity><SINDBAD_FORCING_SUFFIX> in SINDBAD_ENTITIES
# with its forcing file <SINDBAD_FORCING_PREFIX><entity><SINDBAD_FORCING_SUFFIX>.nc,
# e.g., MED_ES_01_frozen_v1/daily_master_dev_v5_MED_ES_01_frozen_v1.nc. The entity is
# named without the suffix in the output.
export SINDBAD_ENTITIES="/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/wisp_eos_q10_med10_v1/outputs/base_v5"
export SINDBAD_FORCING_PREFIX="daily_master_dev_v5_"
export SINDBAD_FORCING_SUFFIX="_frozen_v1"
# period of the forcing files, which replaces the dates of the experiment json
export SINDBAD_DATE_BEGIN="2014-01-12T00:00:00"
export SINDBAD_DATE_END="2023-12-31T00:00:00"

# parameters, absolute or relative to the folder of the experiment json. By default
# the PFT lookup map of the PFT stage of this WISP experiment, which
# prepare_stage.sh builds. Other files are used as they are: an empty
# SINDBAD_PFT_PARAMETERS runs the default parameters, a csv gives one parameter
# vector for all pixels, and a json gives a parameter map. A non-empty
# SINDBAD_PFT_PARAMETER_MAP is the zarr that replaces the data_path of the json, and
# an empty one keeps that data_path.
# The own map is "$SINDBAD_PFT_PARAMETERS_OWN" and "$SINDBAD_PFT_PARAMETER_MAP_OWN".
# This uses the PFT run made in SINDBAD-TRENDY.
export SINDBAD_PFT_PARAMETERS="/Net/Groups/BGI/tscratch/skoirala/WR_DD_PFT_RS/params_by_pft_WR_DD_PFT_RS.json"
export SINDBAD_PFT_PARAMETER_MAP="/Net/Groups/BGI/tscratch/skoirala/WR_DD_PFT_RS/params_by_pft_WR_DD_PFT_RS.zarr"
# the PFT whose parameters the pixels of PFTs without parameters get when the map is
# built. Empty gives them the default of each parameter.
export SINDBAD_DEFAULT_PFT=""

# run options: 1 for true, 0 for false. The runs are not lazy, as run_lazy is false
# in the experiment json, since the plots and the comparison need the output in
# memory.
export SINDBAD_MAKE_PLOTS=1

# fire experiments: 1 runs the experiment, 0 skips it. They differ in the source
# variable of f_burnt_area and its scalar. Each job runs the switched-on experiments
# of its entity one after another, with output in
# <SINDBAD_OUTPUT>/<entity>/output_WISP_<entity>-full_<experiment>.
#   No_Fire    : SINDBAD_VIIRS_FIRE_VAR scaled by 0.0, which turns the fire off
#   VIIRS_Fire : SINDBAD_VIIRS_FIRE_VAR scaled by 0.99
#   WISP_Fire  : SINDBAD_WISP_FIRE_VAR scaled by 0.99
export SINDBAD_DO_NO_FIRE=1
export SINDBAD_DO_VIIRS_FIRE=1
export SINDBAD_DO_WISP_FIRE=1
export SINDBAD_VIIRS_FIRE_VAR="viirs_fire_binary"
export SINDBAD_WISP_FIRE_VAR="WISP_fire_binary"
# 1 compares the switched-on experiments of an entity once all of them are done, with
# maps, time series and metrics in <SINDBAD_OUTPUT>/<entity>/comparison. Nothing is
# compared when only one experiment is switched on.
export SINDBAD_MAKE_COMPARISON=1

# one job, the same for every entity. A 128 x 128 grid holds about 12 GB of output
# plus the forcing in memory, and 32G ran out of memory.
export SINDBAD_THREADS=24
export SINDBAD_MEM="64G"
export SINDBAD_HEAP_HINT="48G"              # Julia heap size hint, below SINDBAD_MEM
export SINDBAD_TIME="1-00:00:00"

# jobs: one job per entity, refilled by a watcher; array: one job array, limit fixed
# at submit
export SINDBAD_SUBMIT_MODE="jobs"
