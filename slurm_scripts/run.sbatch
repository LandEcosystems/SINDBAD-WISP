#!/bin/bash
#SBATCH --job-name=sindbad_run
#SBATCH --ntasks=1
#SBATCH --mail-type=FAIL

# Runs one domain of the stage with SINDBAD_RUN_SCRIPT: a site, a PFT or an entity.
# Submitted by the submit scripts, which pass the resources and the settings of
# env.sh. See README_SLURM.md.

set -euo pipefail

: "${SINDBAD_REPO:?submit through the scripts in slurm_scripts/}"

cd "$SINDBAD_REPO"

export JULIA_NUM_THREADS="$SLURM_CPUS_PER_TASK"
domain_index="${1:-${SLURM_ARRAY_TASK_ID:-}}"
: "${domain_index:?give the domain index as the first argument or run as an array task}"
node="${SLURMD_NODENAME:-$(hostname -s)}"

echo "domain ${domain_index} on ${node} with ${JULIA_NUM_THREADS} threads, started $(date)"

# Julia sometimes fails to start with "failed to map segment from shared object", and
# on some nodes it keeps failing. Failures within 2 minutes are retried a few times,
# then the domain moves to another node. Later failures are real errors.
status=1
started=0
for (( attempt = 1; attempt <= SINDBAD_START_ATTEMPTS; attempt++ )); do
    t_start=$(date +%s)
    if "$JULIA" --project="$SINDBAD_REPO" --startup-file=no --heap-size-hint="$SINDBAD_HEAP_HINT" \
        --sysimage="$SINDBAD_SYSIMAGE" "slurm_scripts/$SINDBAD_RUN_SCRIPT" "$domain_index"; then
        status=0
        started=1
        break
    else
        status=$?
    fi
    if (( $(date +%s) - t_start > 120 )); then
        started=1
        break
    fi
    echo "julia failed at start on attempt ${attempt}, retrying in 30 s"
    sleep 30
done

# The moved domain runs as a job of its own with the name and log of the jobs mode,
# so the watcher sees it queued, also when it was an array task. It keeps the cpus
# and memory of this job.
n_moves="${SINDBAD_START_MOVES_DONE:-0}"
if (( started == 0 )); then
    if (( n_moves >= SINDBAD_START_MOVES )); then
        echo "julia failed to start on ${SINDBAD_START_MOVES} nodes, giving up"
    else
        export SINDBAD_EXCLUDE="${SINDBAD_EXCLUDE:+${SINDBAD_EXCLUDE},}${node}"
        export SINDBAD_START_MOVES_DONE=$(( n_moves + 1 ))
        domain=$(awk -v i="$domain_index" 'NR == i { print $1 }' "${SINDBAD_OUTPUT}/domains.txt")
        echo "julia does not start on ${node}, moving the domain away from ${SINDBAD_EXCLUDE}"
        sbatch --job-name="${SINDBAD_EXP_NAME}_${domain}" --partition="$SINDBAD_PARTITION" \
            --cpus-per-task="$SLURM_CPUS_PER_TASK" --mem="${SLURM_MEM_PER_NODE}M" \
            --time="$SINDBAD_TIME" --exclude="$SINDBAD_EXCLUDE" --export=ALL \
            --output="${SINDBAD_OUTPUT}/logs/${SINDBAD_STAGE}_${domain_index}_${domain}_%j.log" \
            "$SINDBAD_REPO/slurm_scripts/run.sbatch" "$domain_index"
    fi
fi

echo "domain ${domain_index} finished with status ${status} $(date)"
exit "$status"
