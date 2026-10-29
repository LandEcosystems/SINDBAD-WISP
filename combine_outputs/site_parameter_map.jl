# Builds the site map of the parameters after the site runs: the optimized
# parameters of every site along the site dimension, and its json. See README.md.
#
# Usage:
#   ./combine_outputs/run_combine.sh site_parameter_map [path_output] [exp_name] \
#       [format]
#
# path_output and exp_name default to SINDBAD_OUTPUT and SINDBAD_EXP_NAME, format,
# zarr or nc, to zarr. Writes <path_output>/params_concatenated_<exp_name>.<format>
# and its json. Given as the `parameters` config file of a multi-site forward run,
# each site runs with its own parameters; `forcing.subset.site` selects the sites.
include(joinpath(@__DIR__, "common.jl"))

path_output, exp_name = outputArgs()
path_combined = joinpath(path_output, "params_concatenated_$(exp_name).$(formatArg(3))")

found = findRunFiles(path_output, exp_name, "optimization", "params")
isempty(found.paths) || concatParameterFiles(found.paths, path_combined)
