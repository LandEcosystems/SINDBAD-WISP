#!/bin/bash
# Submits the entities as one SLURM job array, with SINDBAD_THREADS cpus per entity
# and at most SINDBAD_MAX_JOBS entities running at once, or no limit when it is empty.
# Usage: ./slurm_scripts/submit_entities.sh [--dry-run] [entities]
#   entities: the lines of entities.txt to run, e.g. 5,17,30-40; all by default

set -euo pipefail

source "$(dirname "$0")/env.sh"

if [ ! -f "$SINDBAD_SYSIMAGE" ]; then
    echo "no system image at ${SINDBAD_SYSIMAGE}, build it first" >&2
    exit 1
fi

dry_run=0
if [ "${1:-}" = "--dry-run" ]; then
    dry_run=1
    shift
fi

log_dir="${SINDBAD_OUTPUT}/logs"
mkdir -p "$log_dir"

"$JULIA" --project="$SINDBAD_REPO" --startup-file=no --sysimage="$SINDBAD_SYSIMAGE" \
    "$slurm_dir/forward_entity_exp.jl" --list-entities
n_entities=$(grep -c . "${SINDBAD_OUTPUT}/entities.txt")
if [ -n "${1:-}" ]; then
    array_range="$1"
else
    # all entities without a done marker, so finished ones take no place in the array
    array_range=$(awk -v done_dir="${SINDBAD_OUTPUT}/done" -v overwrite="$SINDBAD_OVERWRITE" '
        overwrite == "1" || system("test -f \"" done_dir "/" $1 "\"") != 0 { print NR }' \
        "${SINDBAD_OUTPUT}/entities.txt" | paste -sd, -)
    if [ -z "$array_range" ]; then
        echo "all ${n_entities} entities are done, set SINDBAD_OVERWRITE=1 to run them again"
        exit 0
    fi
fi

# an empty SINDBAD_MAX_JOBS runs as many entities at once as SLURM finds room for
array_spec="$array_range"
max_running="no limit"
if [ -n "$SINDBAD_MAX_JOBS" ]; then
    array_spec="${array_range}%${SINDBAD_MAX_JOBS}"
    max_running="at most ${SINDBAD_MAX_JOBS}"
fi

sbatch_args=(--array="$array_spec" --job-name="${SINDBAD_EXP_NAME}_array"
    --partition="$SINDBAD_PARTITION" --cpus-per-task="$SINDBAD_THREADS"
    --mem="$SINDBAD_MEM" --time="$SINDBAD_TIME")
if [ -n "$SINDBAD_EXCLUDE" ]; then
    sbatch_args+=(--exclude="$SINDBAD_EXCLUDE")
fi
sbatch_args+=(--output="${log_dir}/fwd_%a_%A.log" --export=ALL
    "$slurm_dir/forward_entity.sbatch")

if [ "$dry_run" -eq 1 ]; then
    echo "dry run, nothing is submitted"
    echo "  repo:        ${SINDBAD_REPO}"
    echo "  experiment:  ${SINDBAD_EXPERIMENT}"
    echo "  parameters:  ${SINDBAD_PARAMETERS:-default} ${SINDBAD_PARAMETER_MAP}"
    echo "  entities:    ${SINDBAD_ENTITIES}"
    echo "  output:      ${SINDBAD_OUTPUT}"
    echo "  sysimage:    ${SINDBAD_SYSIMAGE} ($(date -r "$SINDBAD_SYSIMAGE" '+%F %T'))"
    echo "  per job:     ${SINDBAD_THREADS} cpus, ${SINDBAD_MEM} memory, ${SINDBAD_TIME} on ${SINDBAD_PARTITION}"
    echo "  running:     ${max_running} entities at once"
    echo "  selected:    ${array_range} of ${n_entities} entities"
    echo "would run: sbatch ${sbatch_args[*]}"
    sbatch --test-only "${sbatch_args[@]}" || true
else
    echo "submitting entities ${array_range} of ${n_entities}, ${max_running} running"
    sbatch "${sbatch_args[@]}"
fi
