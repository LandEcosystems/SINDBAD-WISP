#!/bin/bash
# Runs the stage in SINDBAD_STAGE on its own: prepares its input from the stage
# before, checks the system image and submits the jobs in the way set by
# SINDBAD_SUBMIT_MODE. In jobs mode it runs submit_as_jobs.sh --watch in a detached
# tmux session. run_all.sh runs the stages one after another instead.
# See README_SLURM.md.
# Usage: SINDBAD_STAGE=<stage> ./slurm_scripts/run.sh [--dry-run] [domains]

set -euo pipefail

source "$(dirname "$0")/env.sh"
source "$slurm_dir/prepare_stage.sh"

dry_run=0
[ "${1:-}" = "--dry-run" ] && dry_run=1

prepare_dry_run="$dry_run" prepare_stage

case "$(image_state)" in
    missing)
        cat >&2 << EOF
oops... there is no system image of the ${SINDBAD_STAGE} stage at ${SINDBAD_SYSIMAGE}.
Build it first with
    SINDBAD_STAGE=${SINDBAD_STAGE} ./slurm_scripts/build_image.sh
EOF
        exit 1 ;;
    stale)
        echo "warning: the system image of the ${SINDBAD_STAGE} stage is older than" \
            "Manifest.toml or a commit in dev/, rebuild it with" \
            "SINDBAD_STAGE=${SINDBAD_STAGE} ./slurm_scripts/build_image.sh" >&2 ;;
esac

case "$SINDBAD_SUBMIT_MODE" in
    jobs) ;;
    array) exec "$slurm_dir/submit_as_array.sh" "$@" ;;
    *) echo "SINDBAD_SUBMIT_MODE must be jobs or array" >&2; exit 1 ;;
esac

# show what would be submitted, in the foreground, no tmux
if [ "$dry_run" -eq 1 ]; then
    exec "$slurm_dir/submit_as_jobs.sh" "$@"
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

# a running tmux server does not pass the environment of this shell to a new
# session, so the stage is given on the command
command=$(printf "%q " cd "$slurm_dir")
command+="&& "
command+=$(printf "%q " env SINDBAD_STAGE="$SINDBAD_STAGE" ./submit_as_jobs.sh --watch "$@")
command+="2>&1 | tee -a $(printf "%q" "$path_log")"

tmux new-session -d -s "$session" "$command"
tmux set-option -t "$session" remain-on-exit on > /dev/null

echo "watcher of the ${SINDBAD_STAGE} stage started in tmux session ${session} on $(hostname)"
echo "  attach:   tmux attach -t ${session}   (detach with Ctrl-b d)"
echo "  log:      tail -f ${path_log}"
echo "  stop:     tmux kill-session -t ${session}"
