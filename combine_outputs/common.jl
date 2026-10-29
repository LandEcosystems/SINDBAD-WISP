using Sindbad

# Shared by the scripts in combine_outputs/. See README.md there.
include(joinpath(pkgdir(Sindbad), "tools", "data_io", "concat_site_files.jl"))

"""
    findRunFiles(path_output, exp_name, sub_dir, file_name)

Returns the `file_name` files of the runs of `exp_name` under `path_output`, one per
output folder `output_<domain>_<exp_name>`, with their domains and the domains without
one. A domain is a site for site runs and `<wisp_experiment>_PFT_<k>` for PFT runs.

Sindbad writes `<sub_dir>/<file_name>.zarr` or `.nc`, e.g. `data/restart.zarr`, and
until October 2026 wrote `<sub_dir>/<file_name>_<exp_name>_<domain>.zarr` or `.nc`.
Both are found.
"""
function findRunFiles(path_output, exp_name, sub_dir, file_name)
    folder_pattern = Regex("^output_(.+)_\\Q$(exp_name)\\E\$")
    domains = String[]
    paths = String[]
    domains_without = String[]
    for folder ∈ sort(readdir(path_output))
        m = match(folder_pattern, folder)
        (isnothing(m) || !isdir(joinpath(path_output, folder))) && continue
        domain = m.captures[1]
        names = ("$(file_name)", "$(file_name)_$(exp_name)_$(domain)")
        candidates = [joinpath(path_output, folder, sub_dir, "$(n).$(ext)") for n ∈ names for ext ∈ ("zarr", "nc")]
        i_found = findfirst(ispath, candidates)
        if isnothing(i_found)
            push!(domains_without, domain)
        else
            push!(domains, domain)
            push!(paths, candidates[i_found])
        end
    end
    println("found $(length(paths)) $(file_name) files of $(exp_name) in $(path_output)")
    isempty(domains_without) || println("no $(file_name) file for $(length(domains_without)) runs: $(join(domains_without, ", "))")
    return (; domains, paths, domains_without)
end

# the output folder and experiment name from the arguments, else from
# slurm_scripts/env.sh
outputArgs() = (length(ARGS) >= 1 ? ARGS[1] : ENV["SINDBAD_OUTPUT"],
    length(ARGS) >= 2 ? ARGS[2] : ENV["SINDBAD_EXP_NAME"])

function formatArg(i)
    out_format = length(ARGS) >= i ? lstrip(ARGS[i], '.') : "zarr"
    out_format ∈ ("zarr", "nc") || error("the format must be zarr or nc, not $(out_format)")
    return out_format
end
