ENV["GKSwstype"] = "100"
using Sindbad
using Sindbad.Visualization
using Plots
using Sindbad.DataLoaders.YAXArrays
using Serialization

# Forward run of the experiment in SINDBAD_EXPERIMENT for one entity per call, on the
# whole lat/lon grid of its forcing file, with the fire experiments of the test script
# run_forward_spatial_WISP_DD_fire.jl.
#
# Usage:
#   julia --project=. slurm_scripts/forward_entity_exp.jl <domain_index>
#   julia --project=. slurm_scripts/forward_entity_exp.jl --list-domains
#
# Each entity is a domain. `--list-domains` writes domains.txt to the output
# directory with one entity per line, so that line i is the entity of job or array
# task i. Inside a SLURM job array the index is taken from SLURM_ARRAY_TASK_ID.
#
# The settings come from slurm_scripts/env.sh with SINDBAD_STAGE=entity, so source it
# first when running by hand.
include(joinpath(ENV["SINDBAD_REPO"], "plot_forward_WISP_DD.jl"))
include(joinpath(@__DIR__, "common.jl"))

path_entities = ENV["SINDBAD_ENTITIES"]
make_plots = ENV["SINDBAD_MAKE_PLOTS"] == "1"
make_comparison = ENV["SINDBAD_MAKE_COMPARISON"] == "1"
date_begin = ENV["SINDBAD_DATE_BEGIN"]
date_end = ENV["SINDBAD_DATE_END"]

# an empty SINDBAD_PFT_PARAMETERS runs the default parameters of the model structure
parameter_file = ENV["SINDBAD_PFT_PARAMETERS"]
parameter_file = isempty(parameter_file) ? nothing : parameter_file
if !isnothing(parameter_file)
    # joinpath keeps an absolute path as it is
    path_parameter_file = abspath(joinpath(dirname(experiment_json), parameter_file))
    isfile(path_parameter_file) || error("oops... parameter file does not exist : $(path_parameter_file)")
end
# a relative data_path in the json is taken from the data depot, so the zarr of a
# parameter map is given as an absolute path. An empty SINDBAD_PFT_PARAMETER_MAP
# keeps the data_path of the json.
path_parameter_map = nothing
if !isnothing(parameter_file) && endswith(parameter_file, ".json") && !isempty(ENV["SINDBAD_PFT_PARAMETER_MAP"])
    path_parameter_map = abspath(joinpath(dirname(experiment_json), ENV["SINDBAD_PFT_PARAMETER_MAP"]))
    isdir(path_parameter_map) || error("oops... parameter map does not exist : $(path_parameter_map)")
end

# fire experiments that differ in the source variable of f_burnt_area and its scalar.
# No_Fire scales the VIIRS fire by 0.0, which turns the fire off.
fire_experiments = [
    (name="No_Fire", run=ENV["SINDBAD_DO_NO_FIRE"] == "1",
        source_variable=ENV["SINDBAD_VIIRS_FIRE_VAR"], scalar=0.0),
    (name="VIIRS_Fire", run=ENV["SINDBAD_DO_VIIRS_FIRE"] == "1",
        source_variable=ENV["SINDBAD_VIIRS_FIRE_VAR"], scalar=0.99),
    (name="WISP_Fire", run=ENV["SINDBAD_DO_WISP_FIRE"] == "1",
        source_variable=ENV["SINDBAD_WISP_FIRE_VAR"], scalar=0.99),
]
fire_experiments = filter(fire -> fire.run, fire_experiments)
isempty(fire_experiments) && error("oops... no fire experiment is switched on in env.sh")

# the folder and the forcing file of an entity both carry the suffix, e.g.,
# MED_ES_01_frozen_v1/daily_master_dev_v5_MED_ES_01_frozen_v1.nc, while the entity is
# named without it in the output
forcing_suffix = ENV["SINDBAD_FORCING_SUFFIX"]
entityFolder(entity) = joinpath(path_entities, entity * forcing_suffix)
forcingPath(entity) = joinpath(entityFolder(entity), ENV["SINDBAD_FORCING_PREFIX"] * entity * forcing_suffix * ".nc")

"""
    listEntities()

Returns the sorted names of the entities in SINDBAD_ENTITIES, i.e., the folders whose
name ends with SINDBAD_FORCING_SUFFIX, without the suffix, that have a forcing file.
Folders without one are reported and left out.
"""
function listEntities()
    folders = filter(f -> isdir(joinpath(path_entities, f)) && endswith(f, forcing_suffix),
        readdir(path_entities))
    entities = sort([chopsuffix(f, forcing_suffix) for f ∈ folders])
    for entity ∈ filter(e -> !isfile(forcingPath(e)), entities)
        println("no forcing file, leaving out $(entity) : $(forcingPath(entity))")
    end
    return filter(e -> isfile(forcingPath(e)), entities)
end

# the output of an entity is organized as in run_forward_spatial_WISP_DD_fire.jl:
#   <output_path>/<entity>/output_<domain>_<experiment> : output and figures of a run
#   <output_path>/<entity>/comparison                   : comparison of the experiments
# the comparison folder also holds the summary of each experiment that the
# comparison reads
entityDomain(entity, subset_tag) = "$(entity)-$(subset_tag)"
comparisonPath(output_path, entity) = joinpath(output_path, entity, "comparison")
summaryPath(output_path, entity, fire) = joinpath(comparisonPath(output_path, entity), "summary_$(fire.name).jls")

"""
    runEntity(entity, output_path, fire; lat_subset=nothing, lon_subset=nothing)

Runs the forward model for `entity` with the f_burnt_area forcing of the fire
experiment `fire` and saves its output in `output_path/entity`, in the folder of the
run named after the domain and the experiment. The lat and lon subsets select a block
of pixels, e.g., for the warmup, and the whole grid runs without them. The time-mean maps and space-mean series of the run are saved for the
comparison of the fire experiments. A failure of the plots is reported but does not
fail the run.
"""
function runEntity(entity, output_path, fire; lat_subset=nothing, lon_subset=nothing)
    subset_tag = isnothing(lat_subset) ? "full" : "px$(length(lat_subset))x$(length(lon_subset))"
    domain = entityDomain(entity, subset_tag)
    # run_lazy is false in the experiment json, since the plots and the comparison
    # need the output in memory
    replace_info = Dict{String, Any}(
        "experiment.basics.name" => "$(domain)_$(fire.name)",
        "experiment.model_output.path" => joinpath(output_path, entity),
        "forcing.default_forcing.data_path" => forcingPath(entity),
        "forcing.variables.f_burnt_area.source_variable" => fire.source_variable,
        "forcing.variables.f_burnt_area.source_to_sindbad_unit" => fire.scalar,
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
    # a rerun of the job skips the experiments that are done, so the comparison reads
    # the summary of each experiment from its file
    mkpath(comparisonPath(output_path, entity))
    serialize(summaryPath(output_path, entity, fire),
        (dates=Date.(collect(out.info.helpers.dates.range)),
            summary=summarize_forward_run(values(out.output), out.info.output.variables)))
    if make_plots
        try
            plot_forward_run(out.info, out.forcing, values(out.output), out.info.output.variables,
                "$(domain)-$(fire.name)")
        catch err
            println("oops... plots of $(entity) failed : $(sprint(showerror, err))")
        end
    end
    return out
end

"""
    compareEntity(entity, output_path; subset_tag="full")

Compares the switched-on fire experiments of `entity` from their saved summaries, with
the plots and metrics of `plot_runs_comparison` in `output_path/entity/comparison`.
Nothing is compared with fewer than two experiments or when a summary is missing. A
failure is reported but does not fail the entity.
"""
function compareEntity(entity, output_path; subset_tag="full")
    length(fire_experiments) >= 2 || return nothing
    paths = [summaryPath(output_path, entity, fire) for fire in fire_experiments]
    missing_paths = filter(!isfile, paths)
    if !isempty(missing_paths)
        println("oops... no comparison of $(entity), summaries are missing : $(join(missing_paths, ", "))")
        return nothing
    end
    try
        saved = deserialize.(paths)
        plot_runs_comparison([s.summary for s in saved], [fire.name for fire in fire_experiments],
            first(saved).dates, comparisonPath(output_path, entity),
            entityDomain(entity, subset_tag))
    catch err
        println("oops... comparison of $(entity) failed : $(sprint(showerror, err))")
    end
    return nothing
end

# A marker per finished fire experiment of an entity lets a rerun of the job skip the
# experiments that are done. The marker of the entity, from common.jl, is written
# once all switched-on experiments are done, and the submit scripts skip the
# entities that have one.
donePath(entity, fire) = joinpath(path_output, "done", fire.name, entity)
isDone(entity, fire) = isfile(donePath(entity, fire)) && !overwrite

if abspath(PROGRAM_FILE) == @__FILE__
    if "--list-domains" in ARGS
        entities = listEntities()
        writeDomains(entities, ones(Int, length(entities)))
        exit(0)
    end

    entity_index = domainIndex()
    # the entity is read from domains.txt, so the job runs the entity it was
    # submitted for
    entity = first(split(readlines(path_domains)[entity_index]))

    if isDone(entity)
        println("entity $(entity_index) $(entity) is already done, skipping")
        exit(0)
    end

    println("forward run of entity $(entity_index): $(entity) on $(Threads.nthreads()) threads")
    println("forcing   : $(forcingPath(entity))")
    println("parameters: $(something(parameter_file, "default"))")
    println("fire      : $(join([fire.name for fire in fire_experiments], ", "))")
    # output to a slurm log file is buffered, so flush to see the progress while running
    flush(stdout)

    for fire in fire_experiments
        if isDone(entity, fire)
            println("fire experiment $(fire.name) of $(entity) is already done, skipping")
            continue
        end
        println("fire experiment $(fire.name): $(fire.source_variable) x $(fire.scalar)")
        flush(stdout)
        @time runEntity(entity, path_output, fire)
        # free the output of this experiment before the next one starts
        GC.gc()
        mkpath(dirname(donePath(entity, fire)))
        write(donePath(entity, fire), "$(entity_index)\n")
    end

    if make_comparison
        compareEntity(entity, path_output)
    end

    markDone(entity, entity_index)
end
