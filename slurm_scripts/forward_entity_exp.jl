ENV["GKSwstype"] = "100"
using Sindbad
using Sindbad.Visualization
using Plots
using Sindbad.DataLoaders.YAXArrays

# Forward run of the experiment in SINDBAD_EXPERIMENT for one entity per call, on the
# whole lat/lon grid of its forcing file, with the setup of
# run_forward_spatial_WISP_DD.jl.
#
# Usage:
#   julia --project=. slurm_scripts/forward_entity_exp.jl <entity_index>
#   julia --project=. slurm_scripts/forward_entity_exp.jl --list-entities
#
# `--list-entities` writes entities.txt to the output directory with one entity per
# line, so that line i is the entity of array task i. Inside a SLURM job array the
# index is taken from SLURM_ARRAY_TASK_ID.
#
# The settings come from slurm_scripts/env.sh, so source it first when running by
# hand.
include(joinpath(ENV["SINDBAD_REPO"], "plot_forward_WISP_DD.jl"))

experiment_json = joinpath(ENV["SINDBAD_REPO"], "setups", ENV["SINDBAD_EXPERIMENT"])
path_entities = ENV["SINDBAD_ENTITIES"]
path_output = ENV["SINDBAD_OUTPUT"]
path_list = joinpath(path_output, "entities.txt")
mkpath(path_output)
overwrite = ENV["SINDBAD_OVERWRITE"] == "1"
run_lazy = ENV["SINDBAD_RUN_LAZY"] == "1"
make_plots = ENV["SINDBAD_MAKE_PLOTS"] == "1"
date_begin = ENV["SINDBAD_DATE_BEGIN"]
date_end = ENV["SINDBAD_DATE_END"]
temporal_resolution = "daily"
has_disturbance = false

# an empty SINDBAD_PARAMETERS runs the default parameters of the model structure
parameter_file = ENV["SINDBAD_PARAMETERS"]
parameter_file = isempty(parameter_file) ? nothing : parameter_file
param_tag = isnothing(parameter_file) ? "default" : splitext(basename(parameter_file))[1]
if !isnothing(parameter_file)
    # joinpath keeps an absolute path as it is
    path_parameter_file = abspath(joinpath(dirname(experiment_json), parameter_file))
    isfile(path_parameter_file) || error("oops... parameter file does not exist : $(path_parameter_file)")
end
# a relative data_path in the json is taken from the data depot, so the zarr of a
# parameter map is given as an absolute path. An empty SINDBAD_PARAMETER_MAP keeps the
# data_path of the json.
path_parameter_map = nothing
if !isnothing(parameter_file) && endswith(parameter_file, ".json") && !isempty(ENV["SINDBAD_PARAMETER_MAP"])
    path_parameter_map = abspath(joinpath(dirname(experiment_json), ENV["SINDBAD_PARAMETER_MAP"]))
    isdir(path_parameter_map) || error("oops... parameter map does not exist : $(path_parameter_map)")
end

forcingPath(entity) = joinpath(path_entities, entity, ENV["SINDBAD_FORCING_PREFIX"] * entity * ".nc")

"""
    listEntities()

Returns the sorted names of the entity folders in SINDBAD_ENTITIES that have a forcing
file. Folders without one are reported and left out.
"""
function listEntities()
    entities = sort(filter(e -> isdir(joinpath(path_entities, e)), readdir(path_entities)))
    for entity ∈ filter(e -> !isfile(forcingPath(e)), entities)
        println("no forcing file, leaving out $(entity) : $(forcingPath(entity))")
    end
    return filter(e -> isfile(forcingPath(e)), entities)
end

"""
    runEntity(entity, output_path; lat_subset=nothing, lon_subset=nothing)

Runs the forward model for `entity` and saves its output in `output_path/entity`. The
lat and lon subsets select a block of pixels, e.g., for the warmup, and the whole grid
runs without them. A failure of the plots is reported but does not fail the run.
"""
function runEntity(entity, output_path; lat_subset=nothing, lon_subset=nothing)
    subset_tag = isnothing(lat_subset) ? "all" : "px$(length(lat_subset))x$(length(lon_subset))"
    domain = "$(entity)-$(subset_tag)-params_$(param_tag)-$(temporal_resolution)-disturbance-$(has_disturbance)"
    experiment_name = "WISP_$(domain)_lazy_$(run_lazy)"
    replace_info = Dict(
        "experiment.basics.name" => experiment_name,
        "experiment.flags.run_lazy" => run_lazy,
        "experiment.flags.spinup_TEM" => true,
        "experiment.model_output.path" => joinpath(output_path, entity),
        "forcing.default_forcing.data_path" => forcingPath(entity),
        "experiment.basics.time.date_begin" => date_begin,
        "experiment.basics.time.date_end" => date_end,
    )
    if !isnothing(parameter_file)
        replace_info["experiment.basics.config_files.parameters"] = parameter_file
    end
    if !isnothing(path_parameter_map)
        replace_info["parameters.default_parameter_map.data_path"] = path_parameter_map
    end
    if !isnothing(lat_subset)
        replace_info["forcing.subset.lat"] = lat_subset
        replace_info["forcing.subset.lon"] = lon_subset
    end

    out = runExperimentForward(experiment_json; replace_info=replace_info)
    if make_plots
        try
            plot_forward_run(out.info, out.forcing, values(out.output), out.info.output.variables, domain)
        catch err
            println("oops... plots of $(entity) failed : $(sprint(showerror, err))")
        end
    end
    return out
end

# A marker per finished entity lets a job skip the entities that are done.
donePath(entity) = joinpath(path_output, "done", entity)
isDone(entity) = isfile(donePath(entity)) && !overwrite

if abspath(PROGRAM_FILE) == @__FILE__
    if "--list-entities" in ARGS
        entities = listEntities()
        write(path_list, join(entities, "\n") * "\n")
        println("$(length(entities)) entities written to $(path_list)")
        exit(0)
    end

    entity_index = haskey(ENV, "SLURM_ARRAY_TASK_ID") ? parse(Int, ENV["SLURM_ARRAY_TASK_ID"]) :
        isempty(ARGS) ? error("give the entity index as the first argument or through SLURM_ARRAY_TASK_ID") :
        parse(Int, ARGS[1])
    # the entity is read from entities.txt, so the job runs the entity it was
    # submitted for
    entity = strip(readlines(path_list)[entity_index])

    if isDone(entity)
        println("entity $(entity_index) $(entity) is already done, skipping")
        exit(0)
    end

    println("forward run of entity $(entity_index): $(entity) on $(Threads.nthreads()) threads")
    println("forcing   : $(forcingPath(entity))")
    println("parameters: $(something(parameter_file, "default"))")
    # output to a slurm log file is buffered, so flush to see the progress while running
    flush(stdout)

    @time runEntity(entity, path_output)

    mkpath(dirname(donePath(entity)))
    write(donePath(entity), "$(entity_index)\n")
end
