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
                joinpath(fig_dir, "$(vname)$(v_suffix)_$(domain).png"))
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
                joinpath(fig_dir, "$(vname)$(v_suffix)_timeseries_$(domain).png"))
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
                joinpath(fig_dir, "forc_$(v)_$(domain).png"))
        elseif extra_dim == (:time,) && ndims(def_var) == 1
            # purely temporal variables (f_ambient_CO2, ...) have no space dims, so they are
            # shown as a time series instead of a map
            println("plot forc-model => domain: $domain, variable: $v")
            dat = Array(def_var)
            plot(dat; title="$(v):: mean = $(round(nanmean(dat), digits=3))",
                xlabel="time (index)", legend=false, size=(1200, 600))
            savefig(joinpath(fig_dir, "forc_$(v)_$(domain).png"))
        elseif extra_dim == (:time,)
            println("plot forc-model => domain: $domain, variable: $v")
            plot_map(time_mean_map(def_var), "$(v)",
                joinpath(fig_dir, "forc_$(v)_$(domain).png"))
        else
            n_layer = size(def_var, 1)
            for ll ∈ 1:n_layer
                v_suffix = n_layer == 1 ? "" : "_$(ll)"
                println("plot forc-model => domain: $domain, variable: $(v)$(v_suffix)")
                plot_map(leading_dim_map(def_var, ll), "$(v)$(v_suffix)",
                    joinpath(fig_dir, "forc_$(v)$(v_suffix)_$(domain).png"))
            end
        end
    end
    return nothing
end

# ================================== comparison of forward runs ===================================
# compares runs of the same grid and period, e.g., the fire experiments. each run is
# first reduced with `summarize_forward_run`, so that only small maps and series stay
# in memory.

# the time-mean map and the space-mean series of every output variable and layer of a
# run, as a vector of (name, map, series) in the order of output_vars
function summarize_forward_run(output_array, output_vars)
    summary = Tuple{String, Matrix{Float64}, Vector{Float64}}[]
    for i ∈ eachindex(output_vars)
        (v_group, v_sub) = output_vars[i]
        pd = output_array[i]
        n_layer = size(pd, 2)
        for ll ∈ 1:n_layer
            v_suffix = n_layer == 1 ? "" : "_$(ll)"
            dat = view(pd, :, ll, :, :)
            push!(summary, ("$(v_group)_$(v_sub)$(v_suffix)",
                Float64.(time_mean_map(dat)), Float64.(space_mean_series(dat))))
        end
    end
    return summary
end

# r2, bias and rmse of `other` against `ref`, over the values that are finite in
# both. bias is the mean of other - ref.
function compare_metrics(ref, other)
    ok = isfinite.(ref) .& isfinite.(other)
    x = Float64.(ref[ok])
    y = Float64.(other[ok])
    n = length(x)
    n < 2 && return (r=NaN, r2=NaN, bias=NaN, rmse=NaN, n=n)
    d = y .- x
    dx = x .- sum(x) / n
    dy = y .- sum(y) / n
    r = sum(dx .* dy) / sqrt(sum(dx .^ 2) * sum(dy .^ 2))
    return (r=r, r2=r^2, bias=sum(d) / n, rmse=sqrt(sum(d .^ 2) / n), n=n)
end

metric_text(m) = "r2=$(round(m.r2, sigdigits=3)), bias=$(round(m.bias, sigdigits=3)), rmse=$(round(m.rmse, sigdigits=3))"

# a horizontal colorbar of `cmap` from lo to hi as a panel of its own, so that one
# colorbar serves all the maps of a comparison matrix
function colorbar_panel(cmap, lo, hi, label)
    xs = range(lo, hi; length=201)
    return heatmap(xs, [0.0, 1.0], repeat(collect(xs)', 2); c=cmap, colorbar=false,
        yticks=false, title=label, titlefontsize=12, tickfontsize=9, framestyle=:box,
        top_margin=0Plots.mm, bottom_margin=0Plots.mm)
end

# the diverging colors of the differences, with the smallest differences in grey
function difference_cmap()
    base = cgrad(:RdBu; rev=true)
    grey = RGB(0.8, 0.8, 0.8)
    return cgrad([abs(t - 0.5) < 0.025 ? grey : base[t] for t ∈ range(0, 1; length=201)])
end

# n x n matrix of the time-mean maps of n runs. the diagonal has the map of each run
# on a shared color scale. above it, panel (i, j) has the difference of column minus
# row, run j - run i, on a symmetric scale. below it, panel (i, j) has the histogram
# of the difference run i - run j, which is the map mirrored across the diagonal, in
# the same colors and with the r2, bias and rmse of run i against run j.
function plot_mean_comparison(maps, names, title_str, fig_path)
    n = length(maps)
    finite_vals(a) = filter(isfinite, vec(a))
    all_vals = reduce(vcat, finite_vals.(maps))
    lo, hi = isempty(all_vals) ? (0.0, 1.0) : extrema(all_vals)
    lo == hi && ((lo, hi) = (lo - 0.5, hi + 0.5))
    diffs = Dict((i, j) => maps[j] .- maps[i] for i ∈ 1:n for j ∈ 1:n if i != j)
    d_max = maximum(d -> maximum(abs, finite_vals(d); init=0.0), values(diffs))
    d_max = d_max > 0 ? d_max : 1.0
    v_cmap = cgrad(:viridis; rev=true)
    d_cmap = difference_cmap()
    map_kw = (colorbar=false, framestyle=:none, ticks=false, aspect_ratio=:equal,
        titlefontsize=12, guidefontsize=12)
    hist_kw = (legend=false, grid=false, framestyle=:axes, titlefontsize=10,
        tickfontsize=8, guidefontsize=10, xlims=(-d_max, d_max))

    # the colorbars of the maps and of the differences above the first and last
    # column
    panels = Any[plot(; framestyle=:none, grid=false, ticks=false) for _ ∈ 1:n]
    panels[1] = colorbar_panel(v_cmap, lo, hi, "$(title_str):: time mean")
    panels[n] = colorbar_panel(d_cmap, -d_max, d_max, "difference: column - row")

    n_bins = 40
    edges = range(-d_max, d_max; length=n_bins + 1)
    centers = (edges[1:(end - 1)] .+ edges[2:end]) ./ 2
    for i ∈ 1:n, j ∈ 1:n
        col_title = i == 1 ? names[j] : ""
        row_label = j == 1 ? names[i] : ""
        if i == j
            mean_text = "mean = $(round(nanmean(maps[i]), sigdigits=4))"
            push!(panels, heatmap(maps[i]; c=v_cmap, clims=(lo, hi),
                title=i == 1 ? "$(col_title)\n$(mean_text)" : mean_text,
                ylabel=row_label, map_kw...))
        elseif i < j
            push!(panels, heatmap(diffs[(i, j)]; c=d_cmap, clims=(-d_max, d_max),
                title=col_title, map_kw...))
        else
            d = finite_vals(diffs[(j, i)])
            m = compare_metrics(maps[j], maps[i])
            counts = zeros(Int, n_bins)
            for v ∈ d
                counts[clamp(searchsortedlast(edges, v), 1, n_bins)] += 1
            end
            p = bar(centers, counts; bar_width=step(edges), linewidth=0,
                color=[d_cmap[(c + d_max) / (2 * d_max)] for c ∈ centers],
                title="r=$(round(m.r, sigdigits=3)), r2=$(round(m.r2, sigdigits=3))\n" *
                      "bias=$(round(m.bias, sigdigits=3)), rmse=$(round(m.rmse, sigdigits=3))",
                ylabel=row_label, xlabel="$(names[i]) - $(names[j])", hist_kw...)
            vline!(p, [0.0]; color=:gray, linewidth=1)
            push!(panels, p)
        end
    end
    # the colorbar row is kept low, so that the bars are thin strips with their title
    # and ticks
    top_px = 12
    height = 450 * n + top_px
    top = top_px / height
    plot(panels...; layout=grid(n + 1, n; heights=[top; fill((1 - top) / n, n)]),
        size=(500 * n, height), margin=4Plots.mm)
    savefig(fig_path)
    return nothing
end

# daily, monthly and annual space-mean series of n runs in three stacked subplots,
# one color and line type per run. the legend has the metrics of each run against the
# first.
function plot_time_series_comparison(series, names, dates, title_str, fig_path)
    styles = [:solid, :dash, :dot, :dashdot, :dashdotdot]
    markers = [:circle, :rect, :utriangle, :diamond, :star5]
    colors = palette(:tab10)
    p_d = plot(; title="$(title_str):: daily")
    p_m = plot(; title="monthly mean")
    p_y = plot(; title="annual mean")
    for k ∈ eachindex(series)
        label = k == 1 ? names[k] : "$(names[k]) vs $(names[1]): $(metric_text(compare_metrics(series[1], series[k])))"
        kw = (color=colors[mod1(k, length(colors))], linestyle=styles[mod1(k, length(styles))],
            linewidth=2)
        marker = markers[mod1(k, length(markers))]
        m_dates, m_vals = period_mean_series(series[k], dates, d -> (year(d), month(d)))
        y_dates, y_vals = period_mean_series(series[k], dates, year)
        plot!(p_d, dates, series[k]; label=label, kw...)
        plot!(p_m, m_dates, m_vals; label=names[k], marker=marker, kw...)
        plot!(p_y, y_dates, y_vals; label=names[k], marker=marker, kw...)
    end
    plot(p_d, p_m, p_y; layout=(3, 1), size=(1400, 1500), left_margin=10Plots.mm,
        titlefontsize=16, legendfontsize=11)
    savefig(fig_path)
    return nothing
end

# comparison plots of every variable of n runs and a csv of the metrics of every pair
# of runs, for the time-mean maps (spatial) and the daily space-mean series
# (temporal).
#   summaries : the `summarize_forward_run` of each run, in the order of names
function plot_runs_comparison(summaries, names, dates, fig_dir, domain)
    mkpath(fig_dir)
    n = length(summaries)
    rows = ["variable,scale,reference,other,n,r,r2,bias,rmse"]
    for (v, (vname, _, _)) ∈ enumerate(first(summaries))
        maps = [s[v][2] for s ∈ summaries]
        series = [s[v][3] for s ∈ summaries]
        println("plot comparison => domain: $domain, variable: $(vname)")
        plot_mean_comparison(maps, names, vname,
            joinpath(fig_dir, "$(vname)_mean_comparison_$(domain).png"))
        plot_time_series_comparison(series, names, dates, vname,
            joinpath(fig_dir, "$(vname)_timeseries_comparison_$(domain).png"))
        for i ∈ 1:n, j ∈ (i + 1):n
            for (scale, ref, other) ∈ (("spatial", maps[i], maps[j]), ("temporal", series[i], series[j]))
                m = compare_metrics(ref, other)
                push!(rows, "$(vname),$(scale),$(names[i]),$(names[j]),$(m.n),$(m.r),$(m.r2),$(m.bias),$(m.rmse)")
            end
        end
    end
    metrics_path = joinpath(fig_dir, "comparison_metrics_$(domain).csv")
    write(metrics_path, join(rows, "\n") * "\n")
    println("comparison metrics: $(metrics_path)")
    return nothing
end
