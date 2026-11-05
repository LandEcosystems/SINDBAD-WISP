#!/bin/bash
#SBATCH --job-name=wisp-plot-merge
#SBATCH --account=science
#SBATCH --qos=normal
#SBATCH --partition=work,big,small,gpu
#SBATCH --cpus-per-task=2
#SBATCH --mem=16G
#SBATCH --time=24:00:00
set -euo pipefail
: "${ANALYSIS_CODE:?}" "${ANALYSIS_ROOT:?}" "${ANALYSIS_PYTHON:?}" "${ANALYSIS_PDF_LIBRARY:?}"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export PYTHONPATH="$ANALYSIS_CODE:$ANALYSIS_PDF_LIBRARY${PYTHONPATH:+:$PYTHONPATH}"
"$ANALYSIS_PYTHON" -m result_analysis.parallel_report "$ANALYSIS_ROOT" merge
