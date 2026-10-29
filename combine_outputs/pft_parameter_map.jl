# Builds the PFT lookup map of the parameters after the PFT runs: one value per
# parameter and PFT on the dimension f_pft, and its json. See README.md.
#
# Usage:
#   ./combine_outputs/run_combine.sh pft_parameter_map [path_output] [exp_name] \
#       [default_pft]
#
# path_output and exp_name default to SINDBAD_OUTPUT and SINDBAD_EXP_NAME. Writes
# <path_output>/params_by_pft_<exp_name>.zarr and .json. Given as the `parameters`
# config file of a forward run, each location runs with the parameters of its f_pft
# in the forcing. With default_pft, the locations of PFTs without parameters use
# those of that PFT, else the default of each parameter.
include(joinpath(@__DIR__, "common.jl"))

"""
    pftParameterMap(path_output, exp_name; default_pft=nothing)

Writes the PFT lookup map of the parameters of the PFT runs of `exp_name` under
`path_output` and its json, and returns their paths.
"""
function pftParameterMap(path_output, exp_name; default_pft=nothing)
    found = findRunFiles(path_output, exp_name, "optimization", "params")
    # the domain of a PFT run is <wisp_experiment>_PFT_<k>
    paths_by_pft = Pair{Int,String}[]
    for (domain, path) ∈ zip(found.domains, found.paths)
        m = match(r"_PFT_(\d+)$", domain)
        isnothing(m) || push!(paths_by_pft, parse(Int, m.captures[1]) => path)
    end
    println("parameters of the PFTs $(first.(paths_by_pft))")
    out_path = joinpath(path_output, "params_by_pft_$(exp_name).zarr")
    return concatLookupParameterFiles(paths_by_pft, out_path; lookup_variable="f_pft", default_value=default_pft)
end

if abspath(PROGRAM_FILE) == @__FILE__
    path_output, exp_name = outputArgs()
    default_pft = length(ARGS) >= 3 ? parse(Int, ARGS[3]) : nothing
    pftParameterMap(path_output, exp_name; default_pft)
end
