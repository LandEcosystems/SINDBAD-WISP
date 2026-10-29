# Settings and helpers shared by the run scripts of the jobs, e.g., site_opti_exp.jl
# and pft_opti_exp.jl. Each run script lists its domains with `--list-domains` and
# runs one domain given by its index. See README_SLURM.md.
#
# The settings come from slurm_scripts/env.sh, so source it first when running by
# hand.

experiment_json = joinpath(ENV["SINDBAD_REPO"], "setups", ENV["SINDBAD_EXPERIMENT"])
path_input = joinpath(getSindbadDataDepot(), ENV["SINDBAD_INPUT"])
path_output = ENV["SINDBAD_OUTPUT"]
overwrite = ENV["SINDBAD_OVERWRITE"] == "1"
path_domains = joinpath(path_output, "domains.txt")
mkpath(path_output)

"""
    writeDomains(domains, n_sites)

Writes domains.txt to the output directory, one `<domain> <n_sites>` line per domain,
so that line i is the domain of job or array task i.
"""
function writeDomains(domains, n_sites)
    write(path_domains, join(string.(domains, " ", n_sites), "\n") * "\n")
    println("$(length(domains)) domains written to $(path_domains)")
    return path_domains
end

"""
    domainIndex()

Returns the 1-based index of the domain to run, from SLURM_ARRAY_TASK_ID inside a job
array, else from the first argument.
"""
function domainIndex()
    haskey(ENV, "SLURM_ARRAY_TASK_ID") && return parse(Int, ENV["SLURM_ARRAY_TASK_ID"])
    isempty(ARGS) && error("give the domain index as the first argument or through SLURM_ARRAY_TASK_ID")
    return parse(Int, ARGS[1])
end

# A marker per finished domain lets the submitters skip the domains that are done.
donePath(domain) = joinpath(path_output, "done", domain)
isDone(domain) = isfile(donePath(domain)) && !overwrite

function markDone(domain, domain_index)
    mkpath(dirname(donePath(domain)))
    write(donePath(domain), "$(domain_index)\n")
    return nothing
end
