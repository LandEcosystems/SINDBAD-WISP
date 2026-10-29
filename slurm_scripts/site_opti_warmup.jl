ENV["GKSwstype"] = "100"
using Sindbad
using CMAEvolutionStrategy
using Plots
using Sindbad.DataLoaders.YAXArrays

# A very short optimization of the first site and its figures, run while the system
# image is built so that the code of a full site optimization is compiled into the
# image. It uses the same MT setup as site_opti_exp.jl and only replaces the
# optimizer settings, so the threaded cost is compiled too.
#
# build_sysimage.jl runs the warmup script set by SINDBAD_WARMUP_SCRIPT. The PFT
# experiment uses pft_opti_warmup.jl instead.

experiment_json = joinpath(ENV["SINDBAD_REPO"], "setups", ENV["SINDBAD_EXPERIMENT"])
path_input = joinpath(getSindbadDataDepot(), ENV["SINDBAD_INPUT"])
path_output = joinpath(ENV["SINDBAD_OUTPUT"], "sysimage_warmup")
site = first(open_dataset(path_input).site.val)

replace_info = Dict(
    "experiment.basics.name" => ENV["SINDBAD_EXP_NAME"],
    "experiment.basics.domain" => site,
    "experiment.model_output.path" => path_output,
    "forcing.default_forcing.data_path" => path_input,
    "forcing.subset.site" => [site],
    "optimization.observations.default_observation.data_path" => path_input,
    "optimization.algorithm_optimization" => ENV["SINDBAD_WARMUP_OPTIMIZER"],
)

out_opti = runExperimentOpti(experiment_json; replace_info=replace_info, log_level=:info)
