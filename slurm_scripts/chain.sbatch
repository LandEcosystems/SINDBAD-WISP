#!/bin/bash
#SBATCH --job-name=sindbad_chain
#SBATCH --ntasks=1
#SBATCH --mail-type=FAIL

# One step of the chain of run_all.sh for the stage in SINDBAD_STAGE. prep combines
# the output of the stage before and builds the system image when needed. watch
# submits the jobs of the stage and waits until none is left in the queue. Submitted
# by run_all.sh, which passes the resources and the stage. See README_SLURM.md.

set -euo pipefail

: "${SINDBAD_REPO:?submit through slurm_scripts/run_all.sh}"
step="${1:?give the step, prep or watch}"

cd "$SINDBAD_REPO"
echo "${step} of the ${SINDBAD_STAGE} stage on ${SLURMD_NODENAME:-$(hostname -s)}, started $(date)"

case "$step" in
    prep) ./slurm_scripts/build_image.sh --if-needed ;;
    watch) ./slurm_scripts/submit_as_jobs.sh --until-done ;;
    *) echo "the step must be prep or watch, not ${step}" >&2; exit 1 ;;
esac

echo "${step} of the ${SINDBAD_STAGE} stage finished $(date)"
