
# ================================== using tools ==================================================
# some of the things that will be using... Julia tools, SINDBAD tools, local codes...
using Revise
using Sindbad
using Sindbad.Setup.Dates
using Sindbad.Visualization

toggle_type_abbrev_in_stacktrace()

# ================================== get data / set paths ========================================= 
path_output         = "./";

# ================================== selecting a site =============================================

# ================================== setting up the experiment ====================================
# experiment is all set up according to a (collection of) json file(s)
experiment_json     = joinpath(@__DIR__,"setups/WROASTED_WISP/","experiment_WISP.json");
isfile(experiment_json) ? nothing : println("Hmmm... does not exist : $(experiment_json)");

# setting up the model spinup sequence : can change according to the site...
# spinup_sequence = getSpinupSequenceSite(y_dist, begin_year);
run_lazy = false
domain = "AU-WISP"
experiment_name     = "WISP_FORWARD_lazy_$(run_lazy)";
# default setting in experiment_json will be replaced by the "replace_info"
replace_info = Dict(
    "experiment.basics.name" => experiment_name,
    "experiment.flags.run_lazy" => run_lazy,
    "experiment.flags.spinup_TEM" => false,
    # "experiment.model_spinup.sequence" => spinup_sequence,
    "experiment.model_output.path" => path_output,
    );

info            = getExperimentInfo(experiment_json; replace_info=deepcopy(replace_info)); # note that this will modify information from json with the replace_info
forcing         = getForcing(info); 
run_helpers     = prepTEM(forcing, info); 
@time runTEM!(info.models.forward, run_helpers.space_forcing, run_helpers.space_spinup_forcing, run_helpers.loc_forcing_t, run_helpers.space_output, run_helpers.space_land, run_helpers.tem_info)

# ================================== plots ========================================================
# this is a gridded run, so the arrays carry explicit lat/lon dimensions:
#   output  : (time, layer, lat, lon)
#   forcing : (time, lat, lon)
# every variable is therefore reduced over time and shown as a lat/lon map. the reduction is
# nan-aware so that non-land pixels do not wipe out the whole map.
using Sindbad.NaNStatistics: nanmean

# (time, lat, lon) -> (lat, lon), collapsing the time axis
function time_mean_map(dat)
    arr = Array(dat)
    ndims(arr) == 3 || error("expected (time, lat, lon), got size $(size(arr))")
    return dropdims(nanmean(arr; dims=1); dims=1)
end

# heatmap of a (lat, lon) map: rows (lat) map to y, columns (lon) map to x
function plot_map(map_dat, title_str, fig_path)
    n_nan = sum(is_invalid_number.(map_dat))
    plots_heatmap(map_dat;
        title="$(title_str):: mean = $(round(nanmean(map_dat), digits=3)), nans=$(n_nan)",
        xlabel="lon (index)", ylabel="lat (index)", size=(1200, 1000))
    plots_savefig(fig_path)
    return nothing
end

plots_default(titlefont=(20, "times"), legendfontsize=18, tickfont=(15, :blue))

# ---------------------------------- model output -------------------------------------------------
# plotdat = out_opti.output.optimized;
plotdat = run_helpers.output_array;
# keys(plotdat) would only give 1:n, the variable names live in tem_info
output_vars = val_to_symbol(run_helpers.tem_info.vals.output_vars)
for i ∈ eachindex(output_vars)
    (v_group, v_sub) = output_vars[i]
    vname = "$(v_group)_$(v_sub)"
    pd = plotdat[i]
    n_layer = size(pd, 2)
    for ll ∈ 1:n_layer
        # layered variables (soilW, cEco, ...) get one map per layer
        v_suffix = n_layer == 1 ? "" : "_$(ll)"
        println("plot output-model => domain: $domain, variable: $(vname)$(v_suffix)")
        plot_map(time_mean_map(view(pd, :, ll, :, :)), "$(vname)$(v_suffix)",
            joinpath(info.output.dirs.figure, "$(domain)_$(vname)$(v_suffix).png"))
    end
end

# ---------------------------------- forcing ------------------------------------------------------
forc_vars = forcing.variables
for (o, v) in enumerate(forc_vars)
    println("plot forc-model => domain: $domain, variable: $v")
    def_var = forcing.data[o]
    plot_map(time_mean_map(def_var), "$(v)",
        joinpath(info.output.dirs.figure, "forc_$(domain)_$(v).png"))
end
# @time outdataset = runTEMYax(info.models.forward, forcing, info)

# ================================== forward run ================================================== 
# before running the optimization, check a forward run 
@time out_dflt  = runExperimentForward(experiment_json; replace_info=deepcopy(replace_info)); # full default model

# access some of the internals to do some plots with the forward runs...
info            = getExperimentInfo(experiment_json; replace_info=deepcopy(replace_info)); # note that this will modify information from json with the replace_info
forcing         = getForcing(info); 
run_helpers     = prepTEM(forcing, info); # not needed now

