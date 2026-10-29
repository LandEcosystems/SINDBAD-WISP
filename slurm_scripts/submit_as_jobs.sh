#!/bin/bash
# Submits one SLURM job per domain of the stage within a share of the resources.
# --watch submits again every SINDBAD_WATCH_MINUTES until every domain is done or
# submitted. --until-done also waits until no job of the stage is left in the queue,
# then exits with 0 when every domain is done, or some are and
# SINDBAD_ALLOW_PARTIAL=1, else with 1. See README_SLURM.md.
# Usage: ./slurm_scripts/submit_as_jobs.sh [--watch] [--until-done] [--dry-run]
#                                          [domains]

set -euo pipefail

source "$(dirname "$0")/env.sh"

if [ ! -f "$SINDBAD_SYSIMAGE" ]; then
    echo "no system image at ${SINDBAD_SYSIMAGE}, build it first" >&2
    exit 1
fi

log_dir="${SINDBAD_OUTPUT}/logs"
mkdir -p "$log_dir"

path_domains="${SINDBAD_OUTPUT}/domains.txt"
"$JULIA" --project="$SINDBAD_REPO" --startup-file=no --sysimage="$SINDBAD_SYSIMAGE" \
    "$slurm_dir/$SINDBAD_RUN_SCRIPT" --list-domains
n_domains=$(grep -c . "$path_domains")

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

source "$slurm_dir/slurm_job_limit.sh"

watch=0
until_done=0
dry_run=0
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --watch) watch=1 ;;
        --until-done) watch=1; until_done=1 ;;
        --dry-run) dry_run=1 ;;
        *) echo "unknown option $1" >&2; exit 1 ;;
    esac
    shift
done

# the selected domains with the most sites first, since they take the longest
domain_indices=$(expand_indices "${1:-1-${n_domains}}" |
    awk 'NR == FNR { n_sites[FNR] = $2; next } { print $1, n_sites[$1] }' "$path_domains" - |
    sort -k2,2nr -k1,1n | cut -d " " -f 1)

# domains submitted by this run, so a failed domain is not resubmitted while watching
submitted=""

# sets n_left to the domains still waiting for a free place, n_queued to those in the
# queue and n_done to those that are done
submit_round() {
    local queued_jobs budget_cpus budget_mem used_cpus used_mem domain_index domain n_sites
    queued_jobs=$(squeue -h -u "$USER" -o "%j")
    read -r budget_cpus budget_mem <<< "$(resource_budget)"
    read -r used_cpus used_mem <<< "$(experiment_usage RUNNING,PENDING)"

    local n_submitted=0 full=0
    n_left=0
    n_queued=0
    n_done=0
    for domain_index in $domain_indices; do
        read -r domain n_sites <<< "$(sed -n "${domain_index}p" "$path_domains")"
        if [ -f "${SINDBAD_OUTPUT}/done/${domain}" ] && [ "$SINDBAD_OVERWRITE" != "1" ]; then
            n_done=$((n_done + 1))
            continue
        fi
        if grep -qx "${job_prefix}${domain}" <<< "$queued_jobs"; then
            n_queued=$((n_queued + 1))
            submitted+="${domain}"$'\n'
            continue
        fi
        if grep -qxF "$domain" <<< "$submitted"; then
            continue
        fi
        domain_resources "$n_sites"
        # once a domain does not fit, the smaller ones wait too, so the largest is
        # not held back by smaller ones filling the free places; one job always runs
        if [ "$full" -eq 1 ] || { [ "$used_cpus" -gt 0 ] &&
            { [ $((used_cpus + domain_cpus)) -gt "$budget_cpus" ] ||
              [ $((used_mem + domain_mem)) -gt "$budget_mem" ]; }; }; then
            full=1
            n_left=$((n_left + 1))
            continue
        fi
        domain_sbatch_resources
        local sbatch_args=(--job-name="${job_prefix}${domain}" "${sbatch_resources[@]}"
            --output="${log_dir}/${SINDBAD_STAGE}_${domain_index}_${domain}_%j.log"
            --export=ALL,SINDBAD_HEAP_HINT="$domain_heap_hint"
            "$slurm_dir/run.sbatch" "$domain_index")
        if [ "$dry_run" -eq 1 ]; then
            echo "would run: sbatch ${sbatch_args[*]}"
            # SLURM checks the first request and estimates its start, not queuing it
            if [ "$n_submitted" -eq 0 ]; then
                sbatch --test-only "${sbatch_args[@]}" || true
            fi
        else
            sbatch "${sbatch_args[@]}"
        fi
        submitted+="${domain}"$'\n'
        n_submitted=$((n_submitted + 1))
        used_cpus=$((used_cpus + domain_cpus))
        used_mem=$((used_mem + domain_mem))
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
    echo "  experiment:  ${SINDBAD_EXPERIMENT} (stage ${SINDBAD_STAGE})"
    echo "  run script:  ${SINDBAD_RUN_SCRIPT}"
    echo "  input:       ${SINDBAD_INPUT}"
    echo "  output:      ${SINDBAD_OUTPUT}"
    echo "  data depot:  ${SINDBAD_DATA_DEPOT}"
    echo "  sysimage:    ${SINDBAD_SYSIMAGE} ($(date -r "$SINDBAD_SYSIMAGE" '+%F %T'))"
    echo "  julia:       ${JULIA}"
    if [ -n "${SINDBAD_MAX_THREADS:-}" ]; then
        echo "  per job:     up to ${SINDBAD_MAX_THREADS} threads, ${SINDBAD_MEM_BASE} +" \
            "${SINDBAD_MEM_PER_SITE} per site, ${SINDBAD_TIME} on ${SINDBAD_PARTITION}"
    else
        echo "  per job:     ${SINDBAD_THREADS} threads, ${SINDBAD_MEM} memory," \
            "${SINDBAD_TIME} on ${SINDBAD_PARTITION}"
    fi
    echo "  share:       ${SINDBAD_USE_PERCENT}% of the available resources"
    echo "  domains:     $(wc -w <<< "$domain_indices" | tr -d " ") selected of ${n_domains}"
fi

submit_round
while [ "$watch" -eq 1 ] &&
    { [ "$n_left" -gt 0 ] || { [ "$until_done" -eq 1 ] && [ "$n_queued" -gt 0 ]; }; }; do
    sleep $(( SINDBAD_WATCH_MINUTES * 60 ))
    submit_round
done

if [ "$until_done" -eq 1 ] && [ "$dry_run" -eq 0 ]; then
    # done markers count also with SINDBAD_OVERWRITE=1, since every job has ended
    failed=""
    for domain_index in $domain_indices; do
        read -r domain _ <<< "$(sed -n "${domain_index}p" "$path_domains")"
        [ -f "${SINDBAD_OUTPUT}/done/${domain}" ] || failed+="${domain}"$'\n'
    done
    n_selected=$(wc -w <<< "$domain_indices" | tr -d " ")
    if [ -z "$failed" ]; then
        echo "$(date '+%F %T') all ${n_selected} domains of the ${SINDBAD_STAGE} stage are done"
        exit 0
    fi
    n_failed=$(grep -c . <<< "$failed")
    echo "$(date '+%F %T') ${n_failed} of ${n_selected} domains of the ${SINDBAD_STAGE} stage" \
        "failed: $(head -n 20 <<< "$failed" | paste -sd " " -), logs in ${log_dir}"
    if [ "$SINDBAD_ALLOW_PARTIAL" = "1" ] && [ "$n_failed" -lt "$n_selected" ]; then
        echo "going on with the done domains, since SINDBAD_ALLOW_PARTIAL=1"
        exit 0
    fi
    echo "rerun the failed domains with SINDBAD_STAGE=${SINDBAD_STAGE} ./slurm_scripts/run.sh," \
        "or go on with the done ones with SINDBAD_ALLOW_PARTIAL=1"
    exit 1
fi
