using PackageCompiler

# Builds a system image with Sindbad, Plots and the code compiled by a short forward
# run of the experiment, so that the jobs start without loading or compiling packages.
# The image is built only for the cpus in SINDBAD_CPU_TARGET, since every extra target
# is compiled again.
#
# Run it through slurm_scripts/build_image.sh, which sets the paths and the cpu
# target.

path_sysimage = ARGS[1]
mkpath(dirname(path_sysimage))

create_sysimage([:Sindbad, :Plots];
    sysimage_path=path_sysimage,
    project=ENV["SINDBAD_REPO"],
    precompile_execution_file=joinpath(@__DIR__, "forward_entity_warmup.jl"),
    cpu_target=ENV["SINDBAD_CPU_TARGET"])
