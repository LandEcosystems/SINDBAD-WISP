#!/bin/bash
# Builds the system image of the jobs on the login node. See README_SLURM.md.

set -euo pipefail

source "$(dirname "$0")/env.sh"

cd "$SINDBAD_REPO"
"$JULIA" --project=. --startup-file=no slurm_scripts/build_sysimage.jl "$SINDBAD_SYSIMAGE"
