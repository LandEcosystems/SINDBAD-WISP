#!/bin/bash
#SBATCH --job-name=wisp-findings
#SBATCH --account=science
#SBATCH --qos=normal
#SBATCH --partition=work,big,small,gpu
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=24:00:00
set -euo pipefail
: "${ANALYSIS_CODE:?}" "${ANALYSIS_ROOT:?}" "${ANALYSIS_PYTHON:?}" "${ANALYSIS_PDF_LIBRARY:?}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export PYTHONPATH="$ANALYSIS_CODE:$ANALYSIS_PDF_LIBRARY${PYTHONPATH:+:$PYTHONPATH}"
stage="${1:-component}"
if [[ "$stage" == component ]]; then
    "$ANALYSIS_PYTHON" -m result_analysis.findings_report "$ANALYSIS_ROOT" component --index "${SLURM_ARRAY_TASK_ID:?}"
else
    "$ANALYSIS_PYTHON" -m result_analysis.findings_report "$ANALYSIS_ROOT" "$stage"
fi
