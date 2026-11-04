#!/bin/bash
set -euo pipefail
umask 002
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=/Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/shift_v3_60km/wisp_eos_q10_v1/code/experiments/sindbad_wisp_batch
export WISP_CACHE=/Net/Groups/BGI/tscratch/ybai/wisp_eos_batch1_v1/cache
export TMPDIR="$WISP_CACHE/cube_first_tmp/${SLURM_JOB_ID:-local}"
export XDG_CACHE_HOME="$WISP_CACHE/xdg"
export PYTHONPYCACHEPREFIX="$WISP_CACHE/pycache"
export HF_HOME="$WISP_CACHE/hf"
export TORCH_HOME="$WISP_CACHE/torch"
export TORCHINDUCTOR_CACHE_DIR="$WISP_CACHE/inductor"
export PYTORCH_KERNEL_CACHE_PATH="$WISP_CACHE/kernels"
export TRITON_CACHE_DIR="$WISP_CACHE/triton"
export CUDA_CACHE_PATH="$WISP_CACHE/cuda"
export NUMBA_CACHE_DIR="$WISP_CACHE/numba"
export TORCH_EXTENSIONS_DIR="$WISP_CACHE/extensions"
export MPLCONFIGDIR="$WISP_CACHE/matplotlib"
export OMP_NUM_THREADS=2
export MKL_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=2
export BLOSC_NTHREADS=1
mkdir -p "$TMPDIR" "$PYTORCH_KERNEL_CACHE_PATH" "$CUDA_CACHE_PATH" "$TORCHINDUCTOR_CACHE_DIR" "$TRITON_CACHE_DIR"
for cache_path in "$PYTORCH_KERNEL_CACHE_PATH" "$CUDA_CACHE_PATH"; do
    probe_file=$(mktemp "$cache_path/.wisp-write-probe.XXXXXX")
    rm -- "$probe_file"
done
exec /Net/Groups/BGI/work_3/sindbad/project/sindbad_wisp_eos_batch_test_v1/env/bin/python -u /Net/Groups/BGI/work_1/yuchen/SINDBAD_WISP_longcube/Batch1_400_cube/wisp_eos_cube_first_v1/code/controller.py "$1"
