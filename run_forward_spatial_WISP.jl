
# ================================== using tools ==================================================
# some of the things that will be using... Julia tools, SINDBAD tools, local codes...
using Revise
using Sindbad
using Sindbad.Setup.Dates
using Sindbad.Visualization
using Plots
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

# single-pixel debug subset: array positions into the forcing grid's lat/lon dims
# (0-based grid is 128x128; pick the pixel to debug here)
latidx = 64
lonidx = 64

# default setting in experiment_json will be replaced by the "replace_info"

temporal_resolution = "hourly"
temporal_resolution = "daily"
has_disturbance = false
forc_src_var = "active_fire"
forc_multiplier = 1.0
forc_path = "WISP/AUST-1_2019-11-06_2019-11-20_-32.0_-29.0_150.0_153.0_daily_grid_ent00_physical_$(temporal_resolution)_soil_pft_rgpot_viirs_binary_wisp_firefrac.nc"

domain = "AU-WISP-$temporal_resolution-disturbance-$(has_disturbance)"
experiment_name     = "WISP_$(domain)_FORWARD_lazy_$(run_lazy)";

replace_info = Dict(
    "experiment.basics.name" => experiment_name,
    "experiment.flags.run_lazy" => run_lazy,
    "experiment.flags.spinup_TEM" => true,
    "forcing.default_forcing.data_path" => forc_path,
    # "experiment.model_spinup.sequence" => spinup_sequence,
    "experiment.model_output.path" => path_output,
    # "forcing.subset.lat" => [latidx],
    # "forcing.subset.lon" => [lonidx],
    );

if has_disturbance
    forc_multiplier = 0.0
else
    forc_multiplier = 1.0
end

replace_info["forcing.variables.f_dist_intensity.source_to_sindbad_unit"] = forc_multiplier

if temporal_resolution == "hourly"
    replace_info["experiment.basics.time.temporal_resolution"] = "hour"
    replace_info["experiment.basics.time.date_begin"] = "2019-11-13T11:00:00"
    replace_info["experiment.basics.time.date_end"] = "2019-11-18T10:00:00"
elseif temporal_resolution == "daily"
    replace_info["experiment.basics.time.temporal_resolution"] = "day"
    replace_info["experiment.basics.time.date_begin"] = "2019-11-13"
    replace_info["experiment.basics.time.date_end"] = "2019-11-18"
else
    error("temporal_resolution must be either 'hourly' or 'daily'")
end

out_forward = runExperimentForward(experiment_json; replace_info=replace_info); 
info            = getExperimentInfo(experiment_json; replace_info=deepcopy(replace_info)); # note that this will modify information from json with the replace_info
forcing         = getForcing(info); 
run_helpers     = prepTEM(forcing, info); 
@time runTEM!(info.models.forward, run_helpers.space_forcing, run_helpers.space_spinup, run_helpers.loc_forcing_t, run_helpers.space_output, run_helpers.space_land, run_helpers.tem_info)

# ================================== plots ========================================================
# this is a gridded run, so the arrays carry explicit lat/lon dimensions:
#   output  : (time, layer, lat, lon)
#   forcing : (time, lat, lon)
# every variable is therefore reduced over time and shown as a lat/lon map. the reduction is
# nan-aware so that non-land pixels do not wipe out the whole map.
using Sindbad.NaNStatistics: nanmean

# (leading_dim, lat, lon) -> (lat, lon), taking a single index along the leading dim
# used for a layer of a spatiovertical variable
function leading_dim_map(dat, index)
    arr = Array(dat)
    ndims(arr) == 3 || error("expected (leading_dim, lat, lon), got size $(size(arr))")
    return dropdims(arr[index:index, :, :]; dims=1)
end

# (time, lat, lon) -> (lat, lon), nan-aware mean over the leading (time) dim
# used for a spatiotemporal variable, in place of a single time-step snapshot
function time_mean_map(dat)
    arr = Array(dat)
    ndims(arr) == 3 || error("expected (time, lat, lon), got size $(size(arr))")
    return dropdims(nanmean(arr; dims=1); dims=1)
end

# heatmap of a (lat, lon) map: rows (lat) map to y, columns (lon) map to x
function plot_map(map_dat, title_str, fig_path)
    n_nan = sum(is_invalid_number.(map_dat))
    heatmap(map_dat;
        title="$(title_str):: mean = $(round(nanmean(map_dat), digits=3)), nans=$(n_nan)",
        xlabel="lon (index)", ylabel="lat (index)", size=(1200, 1000))
    savefig(fig_path)
    return nothing
end

default(titlefont=(20, "times"), legendfontsize=18, tickfont=(15, :blue))

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
# forcing variables do not all share the same shape: spatiotemporal ones are (time, lat, lon),
# spatiovertical ones (soil texture, ...) are (soil_depth, lat, lon), and purely spatial ones
# (f_pft, ...) are just (lat, lon). forcing.dims[o] already carries this per-variable, since it
# is exactly the leading, non-space dims computed at load time.
forc_vars = forcing.variables
for (o, v) in enumerate(forc_vars)
    def_var = forcing.data[o]
    extra_dim = forcing.dims[o]
    if isempty(extra_dim)
        println("plot forc-model => domain: $domain, variable: $v")
        plot_map(Array(def_var), "$(v)",
            joinpath(info.output.dirs.figure, "forc_$(domain)_$(v).png"))
    elseif extra_dim == (:time,)
        println("plot forc-model => domain: $domain, variable: $v")
        plot_map(time_mean_map(def_var), "$(v)",
            joinpath(info.output.dirs.figure, "forc_$(domain)_$(v).png"))
    else
        n_layer = size(def_var, 1)
        for ll ∈ 1:n_layer
            v_suffix = n_layer == 1 ? "" : "_$(ll)"
            println("plot forc-model => domain: $domain, variable: $(v)$(v_suffix)")
            plot_map(leading_dim_map(def_var, ll), "$(v)$(v_suffix)",
                joinpath(info.output.dirs.figure, "forc_$(domain)_$(v)$(v_suffix).png"))
        end
    end
end
# @time outdataset = runTEMYax(info.models.forward, forcing, info)

# ================================== forward run ================================================== 
# before running the optimization, check a forward run 
# @time out_dflt  = runExperimentForward(experiment_json; replace_info=deepcopy(replace_info)); # full default model

# # access some of the internals to do some plots with the forward runs...
# info            = getExperimentInfo(experiment_json; replace_info=deepcopy(replace_info)); # note that this will modify information from json with the replace_info
# forcing         = getForcing(info); 
# run_helpers     = prepTEM(forcing, info); # not needed now

