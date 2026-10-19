# ================================== plots of a forward run =======================================
# maps and time series of the model output and maps of the forcing of one gridded forward run.
# include this file and call `plot_forward_run` after the run.
#
# this is a gridded run, so the arrays carry explicit lat/lon dimensions:
#   output  : (time, layer, lat, lon)
#   forcing : (time, lat, lon)
# every variable is therefore reduced over time and shown as a lat/lon map. the reduction is
# nan-aware so that non-land pixels do not wipe out the whole map.
using Plots
using Sindbad.Setup.Dates
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

# maps and time series of every output variable and maps of every forcing variable of a
# forward run, saved to the figure directory of the run.
#   output_array : the output arrays of the run, in the order of output_vars
#   output_vars  : the (field, subfield) pair of each output variable
function plot_forward_run(info, forcing, output_array, output_vars, domain)
    default(titlefont=(20, "times"), legendfontsize=18, tickfont=(15, :blue))
    fig_dir = info.output.dirs.figure

    # ------------------------------ model output ---------------------------------------------
    for i ∈ eachindex(output_vars)
        (v_group, v_sub) = output_vars[i]
        vname = "$(v_group)_$(v_sub)"
        pd = output_array[i]
        n_layer = size(pd, 2)
        for ll ∈ 1:n_layer
            # layered variables (soilW, cEco, ...) get one map per layer
            v_suffix = n_layer == 1 ? "" : "_$(ll)"
            println("plot output-model => domain: $domain, variable: $(vname)$(v_suffix)")
            plot_map(time_mean_map(view(pd, :, ll, :, :)), "$(vname)$(v_suffix)",
                joinpath(fig_dir, "$(domain)_$(vname)$(v_suffix).png"))
        end
    end

    # ------------------------------ model output time series ---------------------------------
    # each variable is averaged over all pixels at every time step, then shown at daily,
    # monthly and annual scales. the dates come from the experiment time range.
    model_dates = Date.(collect(info.helpers.dates.range))
    for i ∈ eachindex(output_vars)
        (v_group, v_sub) = output_vars[i]
        vname = "$(v_group)_$(v_sub)"
        pd = output_array[i]
        size(pd, 1) == length(model_dates) ||
            error("$(vname) has $(size(pd, 1)) time steps but there are $(length(model_dates)) dates")
        n_layer = size(pd, 2)
        for ll ∈ 1:n_layer
            v_suffix = n_layer == 1 ? "" : "_$(ll)"
            println("plot output-model time series => domain: $domain, variable: $(vname)$(v_suffix)")
            plot_time_series(space_mean_series(view(pd, :, ll, :, :)), model_dates,
                "$(vname)$(v_suffix)",
                joinpath(fig_dir, "$(domain)_$(vname)$(v_suffix)_timeseries.png"))
        end
    end

    # ------------------------------ forcing --------------------------------------------------
    # forcing variables do not all share the same shape: spatiotemporal ones are (time, lat, lon),
    # spatiovertical ones (soil texture, ...) are (soil_depth, lat, lon), and purely spatial ones
    # (f_pft, ...) are just (lat, lon). temporal ones (f_ambient_CO2, ...) are only (time,), with no
    # space dims at all. forcing.dims[o] already carries this per-variable, since it
    # is exactly the leading, non-space dims computed at load time.
    for (o, v) in enumerate(forcing.variables)
        def_var = forcing.data[o]
        extra_dim = forcing.dims[o]
        if isempty(extra_dim)
            println("plot forc-model => domain: $domain, variable: $v")
            plot_map(Array(def_var), "$(v)",
                joinpath(fig_dir, "forc_$(domain)_$(v).png"))
        elseif extra_dim == (:time,) && ndims(def_var) == 1
            # purely temporal variables (f_ambient_CO2, ...) have no space dims, so they are
            # shown as a time series instead of a map
            println("plot forc-model => domain: $domain, variable: $v")
            dat = Array(def_var)
            plot(dat; title="$(v):: mean = $(round(nanmean(dat), digits=3))",
                xlabel="time (index)", legend=false, size=(1200, 600))
            savefig(joinpath(fig_dir, "forc_$(domain)_$(v).png"))
        elseif extra_dim == (:time,)
            println("plot forc-model => domain: $domain, variable: $v")
            plot_map(time_mean_map(def_var), "$(v)",
                joinpath(fig_dir, "forc_$(domain)_$(v).png"))
        else
            n_layer = size(def_var, 1)
            for ll ∈ 1:n_layer
                v_suffix = n_layer == 1 ? "" : "_$(ll)"
                println("plot forc-model => domain: $domain, variable: $(v)$(v_suffix)")
                plot_map(leading_dim_map(def_var, ll), "$(v)$(v_suffix)",
                    joinpath(fig_dir, "forc_$(domain)_$(v)$(v_suffix).png"))
            end
        end
    end
    return nothing
end
