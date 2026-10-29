#!/bin/bash
# Runs the stages of the WISP experiment one after another: site, pft and entity.
# Each stage gets a prep job, which combines the output of the stage before and
# builds the system image when needed, and a watch job, which submits the jobs of the
# stage and waits until they are done. Each job starts once the one before has
# succeeded, and a failed job cancels the rest of the chain. See README_SLURM.md.
#
# Usage: ./slurm_scripts/run_all.sh [--from <stage>] [--to <stage>] [--dry-run]
#                                   [--tmux]
#   --from, --to  the first and last stage, site and entity by default
#   --dry-run     prints the sbatch commands, submits nothing
#   --tmux        runs the same steps in a tmux session on this node instead of jobs,
#                 for a cluster where jobs may not submit jobs

set -euo pipefail

slurm_dir="$(cd "$(dirname "$0")" && pwd)"
stages=(site pft entity)

from=site
to=entity
dry_run=0
use_tmux=0
while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --from) from="${2:?--from needs a stage}"; shift ;;
        --to) to="${2:?--to needs a stage}"; shift ;;
        --dry-run) dry_run=1 ;;
        --tmux) use_tmux=1 ;;
        *) echo "unknown option $1" >&2; exit 1 ;;
    esac
    shift
done

stage_index() {
    local i
    for i in "${!stages[@]}"; do
        if [ "${stages[$i]}" = "$1" ]; then
            echo "$i"
            return 0
        fi
    done
    echo "the stage must be one of ${stages[*]}, not $1" >&2
    return 1
}
i_from=$(stage_index "$from")
i_to=$(stage_index "$to")
if [ "$i_from" -gt "$i_to" ]; then
    echo "--from ${from} comes after --to ${to}" >&2
    exit 1
fi
selected=("${stages[@]:i_from:i_to - i_from + 1}")

# the settings shared by the stages; each job reads env.sh again for its own stage
export SINDBAD_STAGE="$from"
source "$slurm_dir/env.sh"
log_dir="${SINDBAD_OUTPUT_ROOT}/logs/chain"
[ "$dry_run" -eq 1 ] || mkdir -p "$log_dir"

if [ "$use_tmux" -eq 1 ]; then
    session="${SINDBAD_WISP_EXPERIMENT}_chain"
    if tmux has-session -t "$session" 2> /dev/null; then
        echo "a tmux session ${session} already runs on $(hostname)," \
            "attach with: tmux attach -t ${session}" >&2
        exit 1
    fi
    command=$(printf "%q " cd "$slurm_dir")
    for stage in "${selected[@]}"; do
        command+="&& "
        command+=$(printf "%q " env SINDBAD_STAGE="$stage" ./build_image.sh --if-needed)
        command+="&& "
        command+=$(printf "%q " env SINDBAD_STAGE="$stage" ./submit_as_jobs.sh --until-done)
    done
    command="{ ${command}; } 2>&1 | tee -a $(printf "%q" "${log_dir}/chain_tmux.log")"
    if [ "$dry_run" -eq 1 ]; then
        echo "would run in tmux session ${session}: ${command}"
        exit 0
    fi
    tmux new-session -d -s "$session" "$command"
    tmux set-option -t "$session" remain-on-exit on > /dev/null
    echo "stages ${selected[*]} started in tmux session ${session} on $(hostname)"
    echo "  attach:   tmux attach -t ${session}   (detach with Ctrl-b d)"
    echo "  log:      tail -f ${log_dir}/chain_tmux.log"
    exit 0
fi

[ "$dry_run" -eq 1 ] && echo "dry run, nothing is submitted"
dependency=""
job_names=()
for stage in "${selected[@]}"; do
    for step in prep watch; do
        if [ "$step" = "prep" ]; then
            resources=(--cpus-per-task="$SINDBAD_BUILD_CPUS" --mem="$SINDBAD_BUILD_MEM"
                --time="$SINDBAD_BUILD_TIME")
        else
            resources=(--cpus-per-task=1 --mem="$SINDBAD_WATCH_MEM" --time="$SINDBAD_WATCH_TIME")
        fi
        job_name="${SINDBAD_WISP_EXPERIMENT}_${step}_${stage}"
        job_names+=("$job_name")
        sbatch_args=(--parsable --job-name="$job_name" --partition="$SINDBAD_PARTITION"
            "${resources[@]}" --output="${log_dir}/${step}_${stage}_%j.log"
            --export=ALL,SINDBAD_STAGE="$stage")
        if [ -n "$SINDBAD_EXCLUDE" ]; then
            sbatch_args+=(--exclude="$SINDBAD_EXCLUDE")
        fi
        if [ -n "$dependency" ]; then
            sbatch_args+=(--dependency=afterok:"$dependency" --kill-on-invalid-dep=yes)
        fi
        sbatch_args+=("$slurm_dir/chain.sbatch" "$step")
        if [ "$dry_run" -eq 1 ]; then
            echo "would run: sbatch ${sbatch_args[*]}"
            dependency="<${job_name}>"
        else
            job_id=$(sbatch "${sbatch_args[@]}")
            # --parsable prints <id> or <id>;<cluster>
            dependency="${job_id%%;*}"
            echo "submitted ${job_name} as ${dependency}"
        fi
    done
done

echo "  jobs:     squeue -u ${USER} -n $(IFS=,; echo "${job_names[*]}")"
echo "  logs:     ${log_dir}"
echo "  stage logs and done markers: ${SINDBAD_OUTPUT_ROOT}/<stage>/"
