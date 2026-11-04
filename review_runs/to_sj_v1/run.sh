#!/usr/bin/env bash
set -euo pipefail
ROOT=${1:?usage: run.sh /absolute/to_SJ}
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PYTHON=/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/wisp_eos_q10_med10_v1/env/bin/python
MODEL=/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/sindbad_fire_nofire_batch1_v1
JULIA=/Net/Groups/Services/HPC_22/apps/julia/julia-1.13.1/bin/julia
mkdir -p "$ROOT/logs" "$ROOT/cache/julia" "$ROOT/cache/tmp"
exec 9>"$ROOT/review.lock"
flock -n 9 || { echo 'This one-entity review is already running'; exit 1; }
export JULIA_DEPOT_PATH="$ROOT/cache/julia:$MODEL/julia_depot:/Net/Groups/BGI/scratch/skoirala/.julia_crap:"
export JULIA_PKG_PRECOMPILE_AUTO=0 JULIA_NUM_THREADS=${SLURM_CPUS_PER_TASK:-8}
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 GKSwstype=100 TMPDIR="$ROOT/cache/tmp"
"$PYTHON" "$HERE/prepare.py" --root "$ROOT" --repo "$(dirname "$(dirname "$HERE")")"
for mode in preflight No_Fire VIIRS_Fire comparison; do
    "$JULIA" --startup-file=no --heap-size-hint=48G \
        --project="$MODEL/runtime/environment" --sysimage="$MODEL/runtime/sindbad_entity.so" \
        "$HERE/run_entity.jl" "$ROOT/review_manifest.json" "$mode" \
        > "$ROOT/logs/${mode}_${SLURM_JOB_ID:-manual}.log" 2>&1
done
"$PYTHON" "$HERE/validate.py" "$ROOT"
