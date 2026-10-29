# Combines the restart files of the runs of one experiment, the pools of every site
# after its spinup, into one file along the site dimension. See README.md.
#
# Usage:
#   ./combine_outputs/run_combine.sh combine_restarts [path_output] [exp_name] \
#       [format]
#
# - After the site runs: the combined file is the SINDBAD_SITE_RESTART of the PFT
#   runs in slurm_scripts/stage_pft.sh, so that every site of a PFT starts from its
#   own pools. slurm_scripts/prepare_stage.sh runs this before the PFT stage.
# - After the PFT runs: the combined file has the pools of every site spun up with
#   the parameters of its PFT, e.g. as the start of a forward run with the PFT map.
#
# path_output and exp_name default to SINDBAD_OUTPUT and SINDBAD_EXP_NAME, format,
# zarr or nc, to zarr. Writes <path_output>/restart_concatenated_<exp_name>.<format>.
include(joinpath(@__DIR__, "common.jl"))

path_output, exp_name = outputArgs()
path_combined = joinpath(path_output, "restart_concatenated_$(exp_name).$(formatArg(3))")

found = findRunFiles(path_output, exp_name, "data", "restart")
isempty(found.paths) || concatRestartFiles(found.paths, path_combined)
