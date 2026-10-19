#!/bin/bash
# Helpers sourced by the submit scripts to fit the jobs in a share of the partition.
# See README_SLURM.md.

# convert a SLURM memory value such as 12G or 500M to MB
memory_in_mb() {
    local value="$1"
    case "$value" in
        *T) echo $(( ${value%T} * 1024 * 1024 )) ;;
        *G) echo $(( ${value%G} * 1024 )) ;;
        *M) echo "${value%M}" ;;
        *) echo "$value" ;;
    esac
}

# jobs of the experiment are named <prefix><entity>, the array <prefix>array
job_prefix="${SINDBAD_EXP_NAME}_"

# the cpus and memory in MB of one job
job_cpus="$SINDBAD_THREADS"
job_mem=$(memory_in_mb "$SINDBAD_MEM")

# Prints "<cpus> <memory in MB>" held by the jobs of the experiment in the given
# states, e.g., RUNNING or RUNNING,PENDING.
experiment_usage() {
    squeue -h -u "$USER" -t "$1" -o "%j %C %m" | awk -v prefix="$job_prefix" '
        index($1, prefix) == 1 {
            cpus += $2
            mem = $3
            if (mem ~ /T$/) mem = mem * 1024 * 1024
            else if (mem ~ /G$/) mem = mem * 1024
            mem_total += mem + 0
        }
        END { printf "%d %d\n", cpus, mem_total }'
}

# Prints "<cpus> <memory in MB>" this experiment may hold in total: the share of the
# idle cpus and unallocated memory of the partition, plus what its running jobs hold,
# so that a submitter that runs again does not grow its share.
resource_budget() {
    local held_cpus held_mem
    read -r held_cpus held_mem <<< "$(experiment_usage RUNNING)"

    # CPUsState is allocated/idle/other/total; memory counts only on usable nodes
    sinfo -h -N -p "$SINDBAD_PARTITION" -O "CPUsState:40,Memory:20,AllocMem:20,StateCompact:20" |
        awk -v partition="$SINDBAD_PARTITION" -v percent="$SINDBAD_USE_PERCENT" \
            -v held_cpus="$held_cpus" -v held_mem="$held_mem" '
            {
                split($1, cpus, "/")
                idle_cpus += cpus[2]
                if ($4 ~ /^(idle|mix)/) idle_mem += $2 - $3
            }
            END {
                avail_cpus = idle_cpus + held_cpus
                avail_mem = idle_mem + held_mem
                budget_cpus = int(avail_cpus * percent / 100)
                budget_mem = int(avail_mem * percent / 100)
                printf "%d cpus and %d MB memory available in %s, %d%% is %d cpus and %d MB\n", avail_cpus, avail_mem, partition, percent, budget_cpus, budget_mem > "/dev/stderr"
                printf "%d %d\n", budget_cpus, budget_mem
            }'
}

# resources of one job, for sbatch
sbatch_resources=(--partition="$SINDBAD_PARTITION" --cpus-per-task="$job_cpus"
    --mem="${job_mem}M" --time="$SINDBAD_TIME")
if [ -n "$SINDBAD_EXCLUDE" ]; then
    sbatch_resources+=(--exclude="$SINDBAD_EXCLUDE")
fi
