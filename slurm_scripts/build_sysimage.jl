using PackageCompiler

# Builds a system image with Sindbad, CMAEvolutionStrategy, Plots and the code
# compiled by a short run of the stage, so that the jobs start without loading or
# compiling packages. The warmup run is SINDBAD_WARMUP_SCRIPT of the stage:
# site_opti_warmup.jl, pft_opti_warmup.jl or forward_entity_warmup.jl. The image is
# built only for the cpus in SINDBAD_CPU_TARGET, since every extra target is compiled
# again.
#
# Run it through slurm_scripts/build_image.sh, which sets the paths and the cpu
# target.

path_sysimage = ARGS[1]
mkpath(dirname(path_sysimage))

create_sysimage([:Sindbad, :CMAEvolutionStrategy, :Plots];
    sysimage_path=path_sysimage,
    project=ENV["SINDBAD_REPO"],
    precompile_execution_file=joinpath(@__DIR__, ENV["SINDBAD_WARMUP_SCRIPT"]),
    cpu_target=ENV["SINDBAD_CPU_TARGET"])
