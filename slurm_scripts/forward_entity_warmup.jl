ENV["GKSwstype"] = "100"

# A short forward run of a 4 x 4 block of pixels of the first entity and its plots,
# run while the system image is built so that the code of a forward run is compiled
# into the image. It uses the same setup as forward_entity_exp.jl.
include(joinpath(@__DIR__, "forward_entity_exp.jl"))

entity = first(listEntities())
# Sindbad subsets by coordinate value, so the values are read as Sindbad reads them
forcing_ds = open_dataset(forcingPath(entity))
lat_values = collect(forcing_ds.lat.val)
lon_values = collect(forcing_ds.lon.val)
centerBlock(coords, n) = coords[(length(coords) ÷ 2 - n ÷ 2 + 1):(length(coords) ÷ 2 - n ÷ 2 + n)]

runEntity(entity, joinpath(path_output, "sysimage_warmup");
    lat_subset=centerBlock(lat_values, 4), lon_subset=centerBlock(lon_values, 4))
