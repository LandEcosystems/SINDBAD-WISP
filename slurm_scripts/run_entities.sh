#!/bin/bash
# Submits the entities in the way set by SINDBAD_SUBMIT_MODE. In jobs mode it runs
# submit_entities_as_jobs.sh --watch in a detached tmux session. See README_SLURM.md.
# Usage: ./slurm_scripts/run_entities.sh [--dry-run] [entities]

set -euo pipefail

source "$(dirname "$0")/env.sh"

case "$SINDBAD_SUBMIT_MODE" in
    jobs) ;;
    array) exec "$slurm_dir/submit_entities.sh" "$@" ;;
    *) echo "SINDBAD_SUBMIT_MODE must be jobs or array" >&2; exit 1 ;;
esac

# show what would be submitted, in the foreground, no tmux
if [ "${1:-}" = "--dry-run" ]; then
    exec "$slurm_dir/submit_entities_as_jobs.sh" "$@"
fi
session="$SINDBAD_EXP_NAME"
path_log="${SINDBAD_OUTPUT}/logs/submit_watch.log"
mkdir -p "$(dirname "$path_log")"

if ! command -v tmux > /dev/null; then
    echo "tmux is not available on $(hostname)" >&2
    exit 1
fi
if tmux has-session -t "$session" 2> /dev/null; then
    echo "a tmux session ${session} already runs on $(hostname)," \
        "attach with: tmux attach -t ${session}" >&2
    exit 1
fi

command=$(printf "%q " cd "$slurm_dir")
command+="&& "
command+=$(printf "%q " ./submit_entities_as_jobs.sh --watch "$@")
command+="2>&1 | tee -a $(printf "%q" "$path_log")"

tmux new-session -d -s "$session" "$command"
tmux set-option -t "$session" remain-on-exit on > /dev/null

echo "watcher started in tmux session ${session} on $(hostname)"
echo "  attach:   tmux attach -t ${session}   (detach with Ctrl-b d)"
echo "  log:      tail -f ${path_log}"
echo "  stop:     tmux kill-session -t ${session}"
