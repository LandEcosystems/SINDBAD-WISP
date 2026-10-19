#!/bin/bash
# Submits one SLURM job per entity within SINDBAD_USE_PERCENT % of the available
# resources of the partition. With --watch it submits again every
# SINDBAD_WATCH_MINUTES until every entity is done or submitted. See README_SLURM.md.
# Usage: ./slurm_scripts/submit_entities_as_jobs.sh [--watch] [--dry-run] [entities]
#   entities: the lines of entities.txt to run, e.g. 5,17,30-40; all by default

set -euo pipefail

source "$(dirname "$0")/env.sh"

if [ ! -f "$SINDBAD_SYSIMAGE" ]; then
    echo "no system image at ${SINDBAD_SYSIMAGE}, build it first" >&2
    exit 1
fi

watch=0
dry_run=0
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --watch) watch=1 ;;
        --dry-run) dry_run=1 ;;
        *) echo "unknown option $1" >&2; exit 1 ;;
    esac
    shift
done

log_dir="${SINDBAD_OUTPUT}/logs"
mkdir -p "$log_dir"

path_entities="${SINDBAD_OUTPUT}/entities.txt"
"$JULIA" --project="$SINDBAD_REPO" --startup-file=no --sysimage="$SINDBAD_SYSIMAGE" \
    "$slurm_dir/forward_entity_exp.jl" --list-entities
n_entities=$(grep -c . "$path_entities")

# expand a list such as 5,17,30-40 into one index per line
expand_indices() {
    local part
    for part in ${1//,/ }; do
        if [[ "$part" == *-* ]]; then
            seq "${part%-*}" "${part#*-}"
        else
            echo "$part"
        fi
    done
}

entity_indices=$(expand_indices "${1:-1-${n_entities}}")

source "$slurm_dir/slurm_job_limit.sh"

# entities submitted by this run, so a failed entity is not resubmitted while watching
submitted=""

# sets n_left to the entities still waiting for a free place
submit_round() {
    local queued_jobs budget_cpus budget_mem used_cpus used_mem entity_index entity
    queued_jobs=$(squeue -h -u "$USER" -o "%j")
    read -r budget_cpus budget_mem <<< "$(resource_budget)"
    read -r used_cpus used_mem <<< "$(experiment_usage RUNNING,PENDING)"

    local n_submitted=0 n_done=0 n_queued=0
    n_left=0
    for entity_index in $entity_indices; do
        entity=$(sed -n "${entity_index}p" "$path_entities")
        if [ -f "${SINDBAD_OUTPUT}/done/${entity}" ] && [ "$SINDBAD_OVERWRITE" != "1" ]; then
            n_done=$((n_done + 1))
            continue
        fi
        if grep -qx "${job_prefix}${entity}" <<< "$queued_jobs"; then
            n_queued=$((n_queued + 1))
            submitted+="${entity}"$'\n'
            continue
        fi
        if grep -qxF "$entity" <<< "$submitted"; then
            continue
        fi
        # one job always runs, so the experiment never waits for a free share forever
        if [ "$used_cpus" -gt 0 ] &&
            { [ $((used_cpus + job_cpus)) -gt "$budget_cpus" ] ||
              [ $((used_mem + job_mem)) -gt "$budget_mem" ]; }; then
            n_left=$((n_left + 1))
            continue
        fi
        local sbatch_args=(--job-name="${job_prefix}${entity}" "${sbatch_resources[@]}"
            --output="${log_dir}/fwd_${entity_index}_${entity}_%j.log" --export=ALL
            "$slurm_dir/forward_entity.sbatch" "$entity_index")
        if [ "$dry_run" -eq 1 ]; then
            echo "would run: sbatch ${sbatch_args[*]}"
            # SLURM checks the first request and estimates its start, not queuing it
            if [ "$n_submitted" -eq 0 ]; then
                sbatch --test-only "${sbatch_args[@]}" || true
            fi
        else
            sbatch "${sbatch_args[@]}"
        fi
        submitted+="${entity}"$'\n'
        n_submitted=$((n_submitted + 1))
        used_cpus=$((used_cpus + job_cpus))
        used_mem=$((used_mem + job_mem))
    done

    local verb="submitted"
    [ "$dry_run" -eq 1 ] && verb="would submit"
    echo "$(date '+%F %T') ${verb} ${n_submitted}, ${n_queued} in the queue," \
        "${n_done} done, ${n_left} left to submit, using ${used_cpus} of ${budget_cpus} cpus" \
        "and ${used_mem} of ${budget_mem} MB"
}

if [ "$dry_run" -eq 1 ]; then
    watch=0
    echo "dry run, nothing is submitted"
    echo "  repo:        ${SINDBAD_REPO}"
    echo "  experiment:  ${SINDBAD_EXPERIMENT}"
    echo "  parameters:  ${SINDBAD_PARAMETERS:-default} ${SINDBAD_PARAMETER_MAP}"
    echo "  entities:    ${SINDBAD_ENTITIES}"
    echo "  output:      ${SINDBAD_OUTPUT}"
    echo "  sysimage:    ${SINDBAD_SYSIMAGE} ($(date -r "$SINDBAD_SYSIMAGE" '+%F %T'))"
    echo "  per job:     ${SINDBAD_THREADS} cpus, ${SINDBAD_MEM} memory, ${SINDBAD_TIME} on ${SINDBAD_PARTITION}"
    echo "  share:       ${SINDBAD_USE_PERCENT}% of the available resources"
    echo "  selected:    $(wc -w <<< "$entity_indices" | tr -d " ") of ${n_entities} entities"
fi

submit_round
while [ "$watch" -eq 1 ] && [ "$n_left" -gt 0 ]; do
    sleep $(( SINDBAD_WATCH_MINUTES * 60 ))
    submit_round
done
