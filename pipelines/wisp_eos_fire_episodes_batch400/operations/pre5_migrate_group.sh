#!/bin/bash
# Each array element owns one immutable token list. No controller runs during import.
set -euo pipefail
campaign_root="$1"
: "${SLURM_ARRAY_TASK_ID:?This script requires a Slurm array task}"
list="$campaign_root/operations/pre5_migration_groups/group_${SLURM_ARRAY_TASK_ID}.txt"
while IFS= read -r token; do
    [[ "$token" =~ ^T[0-9]{6}$ ]] || exit 2
    "$campaign_root/code/run.sh" migrate-region "$token"
done < "$list"
