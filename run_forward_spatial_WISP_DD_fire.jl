
# ================================== using tools ==================================================
# runs the forward model for one entity forcing file, either on its whole lat/lon grid
# or on a block of pixels of it, and makes the maps and time series of
# `plot_forward_run`. It runs up to three fire experiments that differ only in the
# forcing of f_burnt_area, and compares them at the end.
using Revise
using Sindbad
using Sindbad.Visualization
using Plots
toggle_type_abbrev_in_stacktrace()
include(joinpath(@__DIR__, "plot_forward_WISP_DD.jl"))

# ================================== get data / set paths =========================================
# top directory of the output. A relative path is taken from this script's directory.
output_path         = "./output/";
output_path         = abspath(joinpath(@__DIR__, output_path));
make_plots          = true;
# compare the fire experiments at the end when two or more of them run
make_comparison     = true;
# forcing file of the entity, which replaces `default_forcing.data_path` of the forcing json.
# A relative path is taken from this script's directory.
entity              = "MED_ES_01";
path_forcing        = "../data/WISP/daily_master_dev_v5_$(entity)_frozen_v1.nc";
path_forcing        = abspath(joinpath(@__DIR__, path_forcing));
isfile(path_forcing) ? nothing : error("oops... forcing does not exist : $(path_forcing)");
# period of the entity forcing files, which replaces the dates of the experiment json. The
# files start on 2014-01-12 and have their daily time stamps at 00:00.
date_begin          = "2014-01-12T00:00:00";
date_end            = "2023-12-31T00:00:00";

# ================================== fire experiments =============================================
# the experiments differ in the source variable of f_burnt_area and its scalar, the
# source_to_sindbad_unit of the forcing json. The output of each goes to
# output_path/entity/output_WISP_<domain>_<experiment>.
#   No_Fire    : VIIRS_FIRE_VAR scaled by 0.0, which turns the fire off
#   VIIRS_Fire : VIIRS_FIRE_VAR scaled by 0.99
#   WISP_Fire  : WISP_FIRE_VAR scaled by 0.99
DO_NO_FIRE          = true;
DO_VIIRS_FIRE       = true;
DO_WISP_FIRE        = true;
VIIRS_FIRE_VAR      = "viirs_fire_binary";
WISP_FIRE_VAR       = "WISP_fire_binary";

fire_experiments = [
    (name="No_Fire", run=DO_NO_FIRE, source_variable=VIIRS_FIRE_VAR, scalar=0.0),
    (name="VIIRS_Fire", run=DO_VIIRS_FIRE, source_variable=VIIRS_FIRE_VAR, scalar=0.99),
    (name="WISP_Fire", run=DO_WISP_FIRE, source_variable=WISP_FIRE_VAR, scalar=0.99),
];
fire_experiments = filter(fire -> fire.run, fire_experiments);
isempty(fire_experiments) && error("oops... no fire experiment is switched on");

# ================================== selecting the pixels =========================================
# the whole grid is slow, so a block of n_pixels x n_pixels can be run instead.
#   n_pixels = nothing : run all pixels of the grid
#   n_pixels = 8       : run an 8 x 8 block of pixels
# the block is centered on the grid unless pixel_center gives the (lat, lon) array positions
# of its center, e.g., pixel_center = (64, 64). n_pixels = 1 runs a single pixel.
n_pixels            = 8;
# n_pixels            = nothing;
pixel_center        = nothing;

# Sindbad subsets by coordinate value and not by position, so the values of the chosen
# positions are read from the forcing file. The values must match exactly. YAXArrays turns
# the evenly spaced lat and lon of the file into a range whose values can differ from the
# stored ones in the last digit, so they are read with YAXArrays like Sindbad does.
function pixel_block(coords, n, center)
    n_coord = length(coords)
    n <= n_coord || error("n_pixels = $(n) is larger than the $(n_coord) pixels of the grid")
    i_first = clamp(center - n ÷ 2, 1, n_coord - n + 1)
    return coords[i_first:(i_first + n - 1)]
end

forcing_ds = Sindbad.DataLoaders.YAXArrays.open_dataset(path_forcing);
lat_values = collect(forcing_ds.lat.val);
lon_values = collect(forcing_ds.lon.val);
forcing_ds = nothing;
if isnothing(n_pixels)
    subset_tag = "full"
else
    center = isnothing(pixel_center) ?
        (length(lat_values) ÷ 2, length(lon_values) ÷ 2) : pixel_center
    lat_subset = pixel_block(lat_values, n_pixels, center[1])
    lon_subset = pixel_block(lon_values, n_pixels, center[2])
    subset_tag = "px$(n_pixels)_c$(center[2])"
end

# ================================== setting up the experiment ====================================
# experiment is all set up according to a (collection of) json file(s)
experiment_json     = joinpath(@__DIR__,"setups/WROASTED_WISP_DD/","experiment_WISP_DD.json");
isfile(experiment_json) ? nothing : println("Hmmm... does not exist : $(experiment_json)");

# parameters of the run. A relative path is taken from the directory of experiment_json.
#   nothing      : the default parameters of the model structure
#   a csv file   : one parameter vector for all pixels from its `optimized` column, e.g.,
#                  the parameters optimized for a site
#   a json file  : parameters per pixel from a zarr/nc parameter file. The json below is
#                  a class map by f_pft, so each pixel takes the parameters optimized for
#                  its PFT, as in the PFT runs of SINDBAD-TRENDY. Pixels of a PFT that is
#                  not in the map use the default of each parameter, unless the json sets
#                  a `default` class in `lookup_parameter_map`.
parameter_file      = "FR-Pue.csv";
parameter_file      = "params/params_by_pft_WISP_DD.json";
# the zarr of the class map, which replaces `default_parameter_map.data_path` of the json.
# A relative path is taken from the directory of experiment_json.
parameter_map       = "params/params_by_pft.zarr";


param_tag = isnothing(parameter_file) ? "default" : splitext(basename(parameter_file))[1]
domain = "$(entity)-$(subset_tag)"
entity_output = joinpath(output_path, entity)

# default setting in experiment_json will be replaced by the "replace_info". The name,
# output path and fire forcing are set per fire experiment below. The values are of
# any type, since the subset and the fire scalar are added later.
replace_info = Dict{String, Any}(
    "forcing.default_forcing.data_path" => path_forcing,
    "experiment.basics.time.date_begin" => date_begin,
    "experiment.basics.time.date_end" => date_end,
    );
if !isnothing(parameter_file)
    replace_info["experiment.basics.config_files.parameters"] = parameter_file
end
if !isnothing(parameter_file) && endswith(parameter_file, ".json")
    # a relative data_path in the json is taken from the data depot, so the zarr is
    # given here as an absolute path
    path_parameter_map = abspath(joinpath(dirname(experiment_json), parameter_map))
    isdir(path_parameter_map) || error("oops... parameter map does not exist : $(path_parameter_map)")
    replace_info["parameters.default_parameter_map.data_path"] = path_parameter_map
end
if !isnothing(n_pixels)
    replace_info["forcing.subset.lat"] = lat_subset
    replace_info["forcing.subset.lon"] = lon_subset
end

println("forcing: $(path_forcing)")
println("pixels : $(isnothing(n_pixels) ? "all $(length(lat_values)) x $(length(lon_values))" : subset_tag)")
println("output : $(entity_output)")
println("fire   : $(join([fire.name for fire in fire_experiments], ", "))")

# ================================== forward runs and plots =======================================
# only the output of the last experiment is kept in `out`, so that the whole grid
# does not hold three outputs in memory. The comparison uses the time-mean maps and
# space-mean series of each experiment.
fire_summaries = []
out = nothing
for fire in fire_experiments
    # `out` is assigned in the loop, so it has to be declared global to be kept after it
    global out
    fire_domain = "$(domain)-$(fire.name)"
    fire_info = deepcopy(replace_info)
    fire_info["experiment.basics.name"] = "$(domain)_$(fire.name)"
    fire_info["experiment.model_output.path"] = entity_output
    fire_info["forcing.variables.f_burnt_area.source_variable"] = fire.source_variable
    fire_info["forcing.variables.f_burnt_area.source_to_sindbad_unit"] = fire.scalar
    println("fire experiment $(fire.name): $(fire.source_variable) x $(fire.scalar)")

    @time out = runExperimentForward(experiment_json; replace_info=fire_info);
    push!(fire_summaries, summarize_forward_run(values(out.output), out.info.output.variables))
    if make_plots
        plot_forward_run(out.info, out.forcing, values(out.output), out.info.output.variables, fire_domain)
    end
end

# ================================== comparison of the fire experiments ===========================
# with two or more experiments, every output variable gets an n x n panel of the
# time-mean maps and a time series plot with one line per experiment. The metrics of
# every pair are saved as a csv in output_path/entity/comparison.
if make_comparison && length(fire_experiments) >= 2
    plot_runs_comparison(fire_summaries, [fire.name for fire in fire_experiments],
        Date.(collect(out.info.helpers.dates.range)), joinpath(entity_output, "comparison"),
        domain)
end
