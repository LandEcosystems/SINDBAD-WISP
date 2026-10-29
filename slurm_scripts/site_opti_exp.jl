ENV["GKSwstype"] = "100"
using Sindbad
using CMAEvolutionStrategy
using Plots
using Sindbad.DataLoaders.YAXArrays

# Site level optimization of the experiment in SINDBAD_EXPERIMENT, one site per call.
#
# Usage:
#   "$JULIA" --project=. --sysimage="$SINDBAD_SYSIMAGE" \
#       slurm_scripts/site_opti_exp.jl <domain_index>
#   "$JULIA" --project=. --sysimage="$SINDBAD_SYSIMAGE" \
#       slurm_scripts/site_opti_exp.jl --list-domains
#
# Each site is a domain, and the domain index is 1-based along the site dimension of
# the input zarr. Inside a SLURM job array it is taken from SLURM_ARRAY_TASK_ID.
# `--list-domains` writes domains.txt to the output directory, so that line i is the
# site of array task i.
#
# The experiment, its name, input, output and overwrite flag come from
# slurm_scripts/env.sh, so source it first when running by hand.
include(joinpath(@__DIR__, "common.jl"))

site_names = open_dataset(path_input).site.val

if "--list-domains" in ARGS
    writeDomains(site_names, ones(Int, length(site_names)))
    exit(0)
end

site_index = domainIndex()
site = site_names[site_index]

if isDone(site)
    println("site $(site_index) $(site) is already done, skipping")
    exit(0)
end

println("optimizing site $(site_index) of $(length(site_names)): $(site)")
# output to a slurm log file is buffered, so flush to see the progress while running
flush(stdout)

replace_info = Dict(
    "experiment.basics.name" => ENV["SINDBAD_EXP_NAME"],
    "experiment.basics.domain" => site,
    "experiment.model_output.path" => path_output,
    "forcing.default_forcing.data_path" => path_input,
    "forcing.subset.site" => [site],
    "optimization.observations.default_observation.data_path" => path_input,
)

@time out_opti = runExperimentOpti(experiment_json; replace_info=replace_info, log_level=:info)

markDone(site, site_index)
