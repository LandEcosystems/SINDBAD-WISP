#!/bin/bash
# Submits the domains as one SLURM job array, at most the resource limit running at
# once. Every task of an array has the same resources, so this works only for
# stages with a fixed job size such as the site and entity runs. See README_SLURM.md.
# Usage: ./slurm_scripts/submit_as_array.sh [--dry-run] [domains]

set -euo pipefail

source "$(dirname "$0")/env.sh"

if [ -n "${SINDBAD_MAX_THREADS:-}" ]; then
    echo "the jobs of the ${SINDBAD_STAGE} stage are sized by their sites, use SINDBAD_SUBMIT_MODE=jobs" >&2
    exit 1
fi
if [ ! -f "$SINDBAD_SYSIMAGE" ]; then
    echo "no system image at ${SINDBAD_SYSIMAGE}, build it first" >&2
    exit 1
fi

dry_run=0
if [ "${1:-}" = "--dry-run" ]; then
    dry_run=1
    shift
fi

source "$slurm_dir/slurm_job_limit.sh"
domain_resources 1
domain_sbatch_resources
read -r budget_cpus budget_mem <<< "$(resource_budget)"
max_running=$(( budget_cpus / domain_cpus < budget_mem / domain_mem ? budget_cpus / domain_cpus : budget_mem / domain_mem ))
(( max_running < 1 )) && max_running=1
log_dir="${SINDBAD_OUTPUT}/logs"
mkdir -p "$log_dir"

"$JULIA" --project="$SINDBAD_REPO" --startup-file=no --sysimage="$SINDBAD_SYSIMAGE" \
    "$slurm_dir/$SINDBAD_RUN_SCRIPT" --list-domains
n_domains=$(grep -c . "${SINDBAD_OUTPUT}/domains.txt")
# SLURM takes the same lists as the jobs mode, for example 5,17,30-40
if [ -n "${1:-}" ]; then
    array_range="$1"
else
    # all domains without a done marker, so finished ones take no place in the array
    array_range=$(awk -v done_dir="${SINDBAD_OUTPUT}/done" -v overwrite="$SINDBAD_OVERWRITE" '
        overwrite == "1" || system("test -f \"" done_dir "/" $1 "\"") != 0 { print NR }' \
        "${SINDBAD_OUTPUT}/domains.txt" | paste -sd, -)
    if [ -z "$array_range" ]; then
        echo "all ${n_domains} domains are done, set SINDBAD_OVERWRITE=1 to run them again"
        exit 0
    fi
fi

sbatch_args=(--array="${array_range}%${max_running}" --job-name="${job_prefix}array"
    "${sbatch_resources[@]}"
    --output="${log_dir}/${SINDBAD_STAGE}_%A_%a.log" --export=ALL
    "$slurm_dir/run.sbatch")

if [ "$dry_run" -eq 1 ]; then
    echo "dry run, nothing is submitted"
    echo "would run: sbatch ${sbatch_args[*]}"
    sbatch --test-only "${sbatch_args[@]}" || true
else
    echo "submitting domains ${array_range} of ${n_domains}, at most ${max_running} running"
    sbatch "${sbatch_args[@]}"
fi
