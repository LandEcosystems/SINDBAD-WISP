ENV["GKSwstype"] = "100"
using Sindbad
using CMAEvolutionStrategy
using Plots
using Sindbad.DataLoaders.YAXArrays

# PFT level optimization of the experiment in SINDBAD_EXPERIMENT, one PFT per call.
# One parameter vector is optimized for all sites of the PFT, which run in parallel
# on the threads of the job. The pools of each site come from the restart file in
# SINDBAD_SITE_RESTART.
#
# Usage:
#   "$JULIA" --project=. --sysimage="$SINDBAD_SYSIMAGE" \
#       slurm_scripts/pft_opti_exp.jl <domain_index>
#   "$JULIA" --project=. --sysimage="$SINDBAD_SYSIMAGE" \
#       slurm_scripts/pft_opti_exp.jl --list-domains
#
# Each PFT is a domain named <wisp_experiment>_PFT_<k>, after
# SINDBAD_WISP_EXPERIMENT. `--list-domains` writes domains.txt to the output
# directory with the PFTs that have at least SINDBAD_MIN_SITES sites in the restart
# file, and their number of sites, which the submitters use to size the job.
#
# The settings come from slurm_scripts/env.sh, so source it first when running by
# hand.
include(joinpath(@__DIR__, "common.jl"))

path_restart = ENV["SINDBAD_SITE_RESTART"]

pftDomain(pft_class) = "$(ENV["SINDBAD_WISP_EXPERIMENT"])_PFT_$(pft_class)"

"""
    pftSites()

Returns a sorted vector of `pft_class => site names` pairs for the PFTs in the input,
keeping only the sites that have pools in the restart file.
"""
function pftSites()
    ds = open_dataset(path_input)
    site_names = ds.site.val
    site_pft = ds.f_pft.data[:]
    restart_sites = Set(open_dataset(path_restart).site.val)
    valid = [i for i ∈ eachindex(site_pft) if isfinite(site_pft[i]) && site_names[i] ∈ restart_sites]
    classes = sort(unique(round.(Int, site_pft[valid])))
    return [k => site_names[filter(i -> round(Int, site_pft[i]) == k, valid)] for k ∈ classes]
end

"""
    optimizePFT(pft_class, sites; replace_extra=Dict(), output_path=path_output)

Optimizes one parameter vector for `sites`, the sites of `pft_class`. `replace_extra`
is merged into the replace_info, e.g., to set a short optimizer for the warmup.
"""
function optimizePFT(pft_class, sites; replace_extra=Dict(), output_path=path_output)
    replace_info = Dict(
        "experiment.basics.name" => ENV["SINDBAD_EXP_NAME"],
        "experiment.basics.domain" => pftDomain(pft_class),
        "experiment.model_output.path" => output_path,
        "experiment.flags.run_optimization" => true,
        "forcing.default_forcing.data_path" => path_input,
        "forcing.subset.site" => sites,
        "forcing.variables.cEco.data_path" => path_restart,
        "forcing.variables.TWS.data_path" => path_restart,
        "optimization.observations.default_observation.data_path" => path_input,
        "optimization.subset_model_output" => true,
    )
    merge!(replace_info, replace_extra)
    return runExperimentOpti(experiment_json; replace_info=replace_info, log_level=:info)
end

if abspath(PROGRAM_FILE) == @__FILE__
    if "--list-domains" in ARGS
        min_sites = parse(Int, ENV["SINDBAD_MIN_SITES"])
        pft_sites = filter(p -> length(last(p)) >= min_sites, pftSites())
        writeDomains(pftDomain.(first.(pft_sites)), length.(last.(pft_sites)))
        exit(0)
    end

    domain_index = domainIndex()
    # the domain is read from domains.txt, so the job runs the PFT the submitter
    # sized it for
    domain = first(split(readlines(path_domains)[domain_index]))
    pft_class = parse(Int, last(split(domain, "_PFT_")))

    if isDone(domain)
        println("domain $(domain_index) $(domain) is already done, skipping")
        exit(0)
    end

    sites = Dict(pftSites())[pft_class]
    println("optimizing domain $(domain_index): $(domain) with $(length(sites)) sites on $(Threads.nthreads()) threads")
    # output to a slurm log file is buffered, so flush to see the progress while
    # running
    flush(stdout)

    @time out_opti = optimizePFT(pft_class, sites)

    markDone(domain, domain_index)
end
