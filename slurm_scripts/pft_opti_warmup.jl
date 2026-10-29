ENV["GKSwstype"] = "100"

# A very short optimization of the PFT with the fewest sites, run while the system
# image is built so that the code of a PFT optimization is compiled into the image.
# It uses the same setup as pft_opti_exp.jl and only replaces the optimizer settings.
include(joinpath(@__DIR__, "pft_opti_exp.jl"))

pft_class, sites = argmin(p -> length(last(p)), pftSites())
optimizePFT(pft_class, sites;
    replace_extra=Dict("optimization.algorithm_optimization" => ENV["SINDBAD_WARMUP_OPTIMIZER"]),
    output_path=joinpath(path_output, "sysimage_warmup"))
