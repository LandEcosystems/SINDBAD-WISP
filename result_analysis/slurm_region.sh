#!/bin/bash
#SBATCH --job-name=wisp-analysis
#SBATCH --account=science
#SBATCH --qos=normal
#SBATCH --partition=work,big,small,gpu
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=24:00:00
set -euo pipefail
: "${ANALYSIS_CODE:?}" "${ANALYSIS_CONFIG:?}" "${ANALYSIS_IDS:?}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export PYTHONPATH="$ANALYSIS_CODE${PYTHONPATH:+:$PYTHONPATH}"
py="${ANALYSIS_PYTHON:?Set the audited scientific Python executable}"
rid=$(sed -n "$((SLURM_ARRAY_TASK_ID+1))p" "$ANALYSIS_IDS")
test -n "$rid"
"$py" -m result_analysis --config "$ANALYSIS_CONFIG" region --region "$rid"
