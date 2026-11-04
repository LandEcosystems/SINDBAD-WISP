#!/bin/bash
#SBATCH --job-name=wisp-analysis-report
#SBATCH --account=science
#SBATCH --qos=normal
#SBATCH --partition=work,big,small,gpu
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=24:00:00
set -euo pipefail
: "${ANALYSIS_CODE:?}" "${ANALYSIS_CONFIG:?}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export PYTHONPATH="$ANALYSIS_CODE${PYTHONPATH:+:$PYTHONPATH}"
py="${ANALYSIS_PYTHON:?Set the audited scientific Python executable}"
"$py" -m result_analysis --config "$ANALYSIS_CONFIG" references
"$py" -m result_analysis --config "$ANALYSIS_CONFIG" report
