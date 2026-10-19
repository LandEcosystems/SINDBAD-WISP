#!/bin/bash
#SBATCH --job-name=sindbad_fwd
#SBATCH --ntasks=1
#SBATCH --mail-type=FAIL

# Runs the forward model for one entity with forward_entity_exp.jl. Submitted by
# submit_entities.sh, which passes the resources and the settings of env.sh.
# See README_SLURM.md.

set -euo pipefail

: "${SINDBAD_REPO:?submit through the scripts in slurm_scripts/}"

cd "$SINDBAD_REPO"

export JULIA_NUM_THREADS="$SLURM_CPUS_PER_TASK"
entity_index="${1:-${SLURM_ARRAY_TASK_ID:-}}"
: "${entity_index:?give the entity index as the first argument or run as an array task}"
entity=$(sed -n "${entity_index}p" "${SINDBAD_OUTPUT}/entities.txt")
node="${SLURMD_NODENAME:-$(hostname -s)}"

echo "entity ${entity_index} ${entity} on ${node} with ${JULIA_NUM_THREADS} threads, started $(date)"

# Julia sometimes fails to start with "failed to map segment from shared object", and
# on some nodes it keeps failing. Failures within 2 minutes are retried a few times,
# then the entity moves to another node. Later failures are real errors.
status=1
started=0
for (( attempt = 1; attempt <= SINDBAD_START_ATTEMPTS; attempt++ )); do
    t_start=$(date +%s)
    if "$JULIA" --project="$SINDBAD_REPO" --startup-file=no --heap-size-hint="$SINDBAD_HEAP_HINT" \
        --sysimage="$SINDBAD_SYSIMAGE" slurm_scripts/forward_entity_exp.jl "$entity_index"; then
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

# The moved entity runs as a job of its own with the same cpus and memory. It does
# not count towards the limit of running array tasks.
n_moves="${SINDBAD_START_MOVES_DONE:-0}"
if (( started == 0 )); then
    if (( n_moves >= SINDBAD_START_MOVES )); then
        echo "julia failed to start on ${SINDBAD_START_MOVES} nodes, giving up"
    else
        export SINDBAD_EXCLUDE="${SINDBAD_EXCLUDE:+${SINDBAD_EXCLUDE},}${node}"
        export SINDBAD_START_MOVES_DONE=$(( n_moves + 1 ))
        echo "julia does not start on ${node}, moving the entity away from ${SINDBAD_EXCLUDE}"
        sbatch --job-name="${SINDBAD_EXP_NAME}_${entity}" --partition="$SINDBAD_PARTITION" \
            --cpus-per-task="$SLURM_CPUS_PER_TASK" --mem="${SLURM_MEM_PER_NODE}M" \
            --time="$SINDBAD_TIME" --exclude="$SINDBAD_EXCLUDE" --export=ALL \
            --output="${SINDBAD_OUTPUT}/logs/fwd_${entity_index}_${entity}_%j.log" \
            "$SINDBAD_REPO/slurm_scripts/forward_entity.sbatch" "$entity_index"
    fi
fi

echo "entity ${entity_index} ${entity} finished with status ${status} $(date)"
exit "$status"
