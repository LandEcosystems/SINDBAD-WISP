#!/bin/bash
#SBATCH --job-name=analysis-prefire
#SBATCH --account=science
#SBATCH --qos=normal
#SBATCH --partition=work,big,small
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=02:00:00
set -euo pipefail
: "${ANALYSIS_CODE:?}" "${ANALYSIS_ROOT:?}" "${ANALYSIS_PYTHON:?}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export PYTHONPATH="$ANALYSIS_CODE${PYTHONPATH:+:$PYTHONPATH}"
"$ANALYSIS_PYTHON" -m result_analysis.sujan_followup extract "$ANALYSIS_ROOT" \
  --index "$SLURM_ARRAY_TASK_ID" --workers 32
