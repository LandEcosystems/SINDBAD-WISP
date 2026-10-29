ENV["GKSwstype"] = "100"

# A short forward run of a 4 x 4 block of pixels of the first entity and its plots,
# run while the system image is built so that the code of a forward run is compiled
# into the image. It uses the same setup as entity_forward_exp.jl.
include(joinpath(@__DIR__, "entity_forward_exp.jl"))

entity = first(listEntities())
# Sindbad subsets by coordinate value, so the values are read as Sindbad reads them
forcing_ds = open_dataset(forcingPath(entity))
lat_values = collect(forcing_ds.lat.val)
lon_values = collect(forcing_ds.lon.val)
centerBlock(coords, n) = coords[(length(coords) ÷ 2 - n ÷ 2 + 1):(length(coords) ÷ 2 - n ÷ 2 + n)]

path_warmup = joinpath(path_output, "sysimage_warmup")
runEntity(entity, path_warmup, first(fire_experiments);
    lat_subset=centerBlock(lat_values, 4), lon_subset=centerBlock(lon_values, 4))

# the comparison of the first experiment with itself compiles the comparison plots
saved = deserialize(summaryPath(path_warmup, entity, first(fire_experiments)))
plot_runs_comparison([saved.summary, saved.summary], ["warmup_a", "warmup_b"], saved.dates,
    comparisonPath(path_warmup, entity), "warmup")
