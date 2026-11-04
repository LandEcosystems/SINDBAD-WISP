#!/usr/bin/env bash
#SBATCH --job-name=sj-entity-review
#SBATCH --account=science
#SBATCH --qos=normal
#SBATCH --partition=work
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=7-00:00:00
set -euo pipefail
REPO=${1:?pass the frozen checkout path}
ROOT=${2:?pass the to_SJ output directory}
bash "$REPO/review_runs/to_sj_v1/run.sh" "$ROOT"
