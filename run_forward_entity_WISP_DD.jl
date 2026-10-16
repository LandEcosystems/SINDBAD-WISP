# ================================== using tools ==================================================
# runs the forward model for several sites, one forcing file per site, and saves the output of
# each site to its own output folder
using Revise
using Sindbad
using Sindbad.Visualization
using Plots
toggle_type_abbrev_in_stacktrace()
# maps and time series of the output and forcing of each run, see `plot_forward_run`
include(joinpath(@__DIR__, "plot_forward_WISP_DD.jl"))

# ================================== get data / set paths =========================================
# top directory of the output, with one directory per site inside it. A relative path is
# taken from this script's directory.
output_path         = "./output/";
output_path         = abspath(joinpath(@__DIR__, output_path));
# a site that already has output files is skipped, unless overwrite is true
overwrite           = false;
# also make the maps and time series of `plot_forward_run` for each site. the built-in
# figures of Sindbad are made by `runExperimentForward` either way.
make_plots          = true;
# each site has a folder in this directory with its forcing file, which is named by
# `forcing_file_name`, e.g., daily_master_dev_v5_MED_PT_01.nc for the site MED_PT_01
path_sites          = "/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/wisp_eos_q10_med10_v1/outputs/base_v5";
forcing_file_name(site) = "daily_master_dev_v5_$(site).nc";
# period of the entity forcing files, which replaces the dates of the experiment json. The
# files start on 2014-01-12 and have their daily time stamps at 00:00.
date_begin          = "2014-01-12T00:00:00";
date_end            = "2023-12-31T00:00:00";

# ================================== selecting sites ==============================================
# all site folders by default. give a list of names instead to run only some of them, e.g.,
# sites = ["MED_ES_01", "MED_FR_02"]
sites = sort(filter(s -> isdir(joinpath(path_sites, s)), readdir(path_sites)));

# ================================== setting up the experiment ====================================
# experiment is all set up according to a (collection of) json file(s)
experiment_json     = joinpath(@__DIR__,"setups/WROASTED_WISP_DD/","experiment_WISP_DD.json");
isfile(experiment_json) ? nothing : println("Hmmm... does not exist : $(experiment_json)");

run_lazy = false
temporal_resolution = "daily"
has_disturbance = false

# true when a site directory already has saved output files. The files are written at the
# end of a run, so a run that failed halfway does not count.
function has_output(site_output)
    isdir(site_output) || return false
    for run_dir ∈ readdir(site_output; join=true)
        data_dir = joinpath(run_dir, "data")
        isdir(data_dir) || continue
        any(f -> endswith(f, ".nc") || endswith(f, ".zarr"), readdir(data_dir)) && return true
    end
    return false
end

# ================================== run all sites ================================================
# a site that fails is reported and skipped, so that the other sites still run
failed_sites = Dict{String,Any}()
skipped_sites = String[]
failed_plots = Dict{String,Any}()
for (site_index, site) ∈ enumerate(sites)
    path_forcing = joinpath(path_sites, site, forcing_file_name(site))
    if !isfile(path_forcing)
        println("oops... forcing does not exist, skipping $(site) : $(path_forcing)")
        failed_sites[site] = "missing forcing file"
        continue
    end
    site_output = joinpath(output_path, site)
    if has_output(site_output) && !overwrite
        println("output exists, skipping $(site) : $(site_output)")
        push!(skipped_sites, site)
        continue
    end
    domain = "$(site)-$temporal_resolution-disturbance-$(has_disturbance)"
    experiment_name = "WISP_$(domain)_lazy_$(run_lazy)"
    println("\n================ site $(site_index)/$(length(sites)): $(site) ================")
    println("forcing: $(path_forcing)")
    println("output : $(site_output)")

    replace_info = Dict(
        "experiment.basics.name" => experiment_name,
        "experiment.flags.run_lazy" => run_lazy,
        "experiment.flags.spinup_TEM" => true,
        "experiment.model_output.path" => site_output,
        "forcing.default_forcing.data_path" => path_forcing,
        "experiment.basics.time.date_begin" => date_begin,
        "experiment.basics.time.date_end" => date_end,
        )
    out = try
        @time runExperimentForward(experiment_json; replace_info=deepcopy(replace_info))
    catch err
        println("oops... $(site) failed : $(sprint(showerror, err))")
        failed_sites[site] = err
        nothing
    end
    # the output is saved by now, so a failed plot is reported but does not fail the site
    if make_plots && !isnothing(out)
        try
            plot_forward_run(out.info, out.forcing, values(out.output), out.info.output.variables, domain)
        catch err
            println("oops... plots of $(site) failed : $(sprint(showerror, err))")
            failed_plots[site] = err
        end
    end
    out = nothing
    # free the output of this site before the next one
    GC.gc()
end

n_ran = length(sites) - length(failed_sites) - length(skipped_sites)
println("\n================ done: $(n_ran) ran, $(length(skipped_sites)) skipped, $(length(failed_sites)) failed of $(length(sites)) sites ================")
for site ∈ skipped_sites
    println("skipped : $(site) => output exists, set overwrite = true to run it again")
end
for (site, err) ∈ failed_sites
    println("failed : $(site) => $(err isa String ? err : sprint(showerror, err))")
end
for (site, err) ∈ failed_plots
    println("plots failed : $(site) => $(sprint(showerror, err))")
end
