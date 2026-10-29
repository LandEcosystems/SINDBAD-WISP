#!/bin/bash
# Runs a script of combine_outputs/ with the Julia and the system image of the runs
# in slurm_scripts/env.sh, so it uses the Julia and Sindbad of the runs whatever
# julia is on the PATH. See README.md.
# Usage: ./combine_outputs/run_combine.sh <script> [path_output] [exp_name] [more]
#   script       combine_restarts, pft_parameter_map or site_parameter_map
#   path_output  the output folder of the runs, by default SINDBAD_OUTPUT of env.sh
#   exp_name     the experiment name of the runs, by default SINDBAD_EXP_NAME

set -euo pipefail

combine_dir="$(cd "$(dirname "$0")" && pwd)"
source "$combine_dir/../slurm_scripts/env.sh"

if [ $# -lt 1 ]; then
    echo "usage: $0 combine_restarts|pft_parameter_map|site_parameter_map [path_output] [exp_name] [more]" >&2
    exit 1
fi
script="$combine_dir/${1%.jl}.jl"
shift
if [ ! -f "$script" ]; then
    echo "no script ${script}" >&2
    exit 1
fi

sysimage=()
if [ -f "$SINDBAD_SYSIMAGE" ]; then
    sysimage=(--sysimage="$SINDBAD_SYSIMAGE")
else
    echo "no system image at ${SINDBAD_SYSIMAGE}, Sindbad is loaded from the project" >&2
fi
echo "$JULIA $(basename "$script") ${*:-with SINDBAD_OUTPUT=${SINDBAD_OUTPUT} SINDBAD_EXP_NAME=${SINDBAD_EXP_NAME}}"
"$JULIA" --project="$SINDBAD_REPO" --startup-file=no ${sysimage[@]+"${sysimage[@]}"} "$script" "$@"
