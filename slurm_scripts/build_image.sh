#!/bin/bash
# Builds the system image of the jobs of the stage in SINDBAD_STAGE. The warmup run
# of the build reads the input of the stage, so that input is prepared first. With
# --if-needed the build is skipped when the image is up to date. See README_SLURM.md.
# Usage: ./slurm_scripts/build_image.sh [--if-needed]

set -euo pipefail

source "$(dirname "$0")/env.sh"
source "$slurm_dir/prepare_stage.sh"

prepare_stage

state=$(image_state)
if [ "${1:-}" = "--if-needed" ] && [ "$state" = "ok" ]; then
    echo "the system image of the ${SINDBAD_STAGE} stage is up to date: ${SINDBAD_SYSIMAGE}"
    exit 0
fi

echo "building the system image of the ${SINDBAD_STAGE} stage, ${state} before: ${SINDBAD_SYSIMAGE}"
cd "$SINDBAD_REPO"
"$JULIA" --project=. --startup-file=no slurm_scripts/build_sysimage.jl "$SINDBAD_SYSIMAGE"
