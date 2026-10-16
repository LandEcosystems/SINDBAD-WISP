
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
# forcing file for this run, which replaces `default_forcing.data_path` of the forcing
# json. A relative path is taken from this script's directory.
path_forcing        = "../data/WISP/daily_master_dev_v5.nc";
path_forcing        = abspath(joinpath(@__DIR__, path_forcing));
isfile(path_forcing) ? nothing : println("oops... forcing does not exist : $(path_forcing)");

# ================================== selecting a site =============================================

# ================================== setting up the experiment ====================================
# experiment is all set up according to a (collection of) json file(s)
experiment_json     = joinpath(@__DIR__,"setups/WROASTED_WISP_DD/","experiment_WISP_DD.json");
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

domain = "AU-WISP-$temporal_resolution-disturbance-$(has_disturbance)"
experiment_name     = "WISP_$(domain)_FORWARD_lazy_$(run_lazy)";

replace_info = Dict(
    "experiment.basics.name" => experiment_name,
    "experiment.flags.run_lazy" => run_lazy,
    "experiment.flags.spinup_TEM" => true,
    # "experiment.model_spinup.sequence" => spinup_sequence,
    "experiment.model_output.path" => path_output,
    "forcing.default_forcing.data_path" => path_forcing,
    # "forcing.subset.lat" => [latidx],
    # "forcing.subset.lon" => [lonidx],
    );

if has_disturbance
    forc_multiplier = 0.0
else
    forc_multiplier = 1.0
end

# replace_info["forcing.variables.f_dist_intensity.source_to_sindbad_unit"] = forc_multiplier


# out_forward = runExperimentForward(experiment_json; replace_info=replace_info); 
info            = getExperimentInfo(experiment_json; replace_info=deepcopy(replace_info)); # note that this will modify information from json with the replace_info
forcing         = getForcing(info); 
run_helpers     = prepTEM(forcing, info); 
@time runTEM!(run_helpers.space_selected_models, run_helpers.space_forcing, run_helpers.space_spinup, run_helpers.loc_forcing_t, run_helpers.space_output, run_helpers.space_land, run_helpers.tem_info; show_progress=true)

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

# (time, lat, lon) -> (time,), nan-aware mean over all pixels at each time step
function space_mean_series(dat)
    arr = Array(dat)
    ndims(arr) == 3 || error("expected (time, lat, lon), got size $(size(arr))")
    return vec(nanmean(arr; dims=(2, 3)))
end

# nan-aware mean of a daily series within each period. period_of maps a date to its
# period key, so (year, month) gives monthly and year gives annual values. each period
# is placed at its first date.
function period_mean_series(series, dates, period_of)
    period_keys = period_of.(dates)
    periods = unique(period_keys)
    p_dates = [dates[findfirst(==(p), period_keys)] for p ∈ periods]
    p_vals = [nanmean(series[period_keys .== Ref(p)]) for p ∈ periods]
    return p_dates, p_vals
end

# daily, monthly and annual time series of one variable in three stacked subplots
function plot_time_series(series, dates, title_str, fig_path)
    m_dates, m_vals = period_mean_series(series, dates, d -> (year(d), month(d)))
    y_dates, y_vals = period_mean_series(series, dates, year)
    p_d = plot(dates, series; title="$(title_str):: daily", label=nothing)
    p_m = plot(m_dates, m_vals; title="monthly mean", marker=:circle, label=nothing)
    p_y = plot(y_dates, y_vals; title="annual mean", marker=:circle, label=nothing)
    plot(p_d, p_m, p_y; layout=(3, 1), size=(1400, 1500), left_margin=10Plots.mm)
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

# ---------------------------------- model output time series -------------------------------------
# each variable is averaged over all pixels at every time step, then shown at daily,
# monthly and annual scales. the dates come from the experiment time range.
model_dates = Date.(collect(info.helpers.dates.range))
for i ∈ eachindex(output_vars)
    (v_group, v_sub) = output_vars[i]
    vname = "$(v_group)_$(v_sub)"
    pd = plotdat[i]
    size(pd, 1) == length(model_dates) ||
        error("$(vname) has $(size(pd, 1)) time steps but there are $(length(model_dates)) dates")
    n_layer = size(pd, 2)
    for ll ∈ 1:n_layer
        v_suffix = n_layer == 1 ? "" : "_$(ll)"
        println("plot output-model time series => domain: $domain, variable: $(vname)$(v_suffix)")
        plot_time_series(space_mean_series(view(pd, :, ll, :, :)), model_dates,
            "$(vname)$(v_suffix)",
            joinpath(info.output.dirs.figure, "$(domain)_$(vname)$(v_suffix)_timeseries.png"))
    end
end

# ---------------------------------- forcing ------------------------------------------------------
# forcing variables do not all share the same shape: spatiotemporal ones are (time, lat, lon),
# spatiovertical ones (soil texture, ...) are (soil_depth, lat, lon), and purely spatial ones
# (f_pft, ...) are just (lat, lon). temporal ones (f_ambient_CO2, ...) are only (time,), with no
# space dims at all. forcing.dims[o] already carries this per-variable, since it
# is exactly the leading, non-space dims computed at load time.
forc_vars = forcing.variables
for (o, v) in enumerate(forc_vars)
    def_var = forcing.data[o]
    extra_dim = forcing.dims[o]
    if isempty(extra_dim)
        println("plot forc-model => domain: $domain, variable: $v")
        plot_map(Array(def_var), "$(v)",
            joinpath(info.output.dirs.figure, "forc_$(domain)_$(v).png"))
    elseif extra_dim == (:time,) && ndims(def_var) == 1
        # purely temporal variables (f_ambient_CO2, ...) have no space dims, so they are
        # shown as a time series instead of a map
        println("plot forc-model => domain: $domain, variable: $v")
        dat = Array(def_var)
        plot(dat; title="$(v):: mean = $(round(nanmean(dat), digits=3))",
            xlabel="time (index)", legend=false, size=(1200, 600))
        savefig(joinpath(info.output.dirs.figure, "forc_$(domain)_$(v).png"))
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
@time out_dflt  = runExperimentForward(experiment_json; replace_info=deepcopy(replace_info)); # full default model

# # access some of the internals to do some plots with the forward runs...
# info            = getExperimentInfo(experiment_json; replace_info=deepcopy(replace_info)); # note that this will modify information from json with the replace_info
# forcing         = getForcing(info); 
# run_helpers     = prepTEM(forcing, info); # not needed now

