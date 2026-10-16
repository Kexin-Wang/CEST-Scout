#!/usr/bin/env bash
# Run a whole model comparison as ONE SLURM batch job on healthy GPUs (the 8xH100 boxes
# cp087-cp089 have failed CUDA initialisation, error 802, since 2026-09-25):
#
#   REPLICATES=2 sbatch --gres=gpu:h200:2 hpc/sbatch_compare.sh "intervertebral disc" ivd20260927
#   REPLICATES=2 sbatch --gres=gpu:h100:2 --nodelist=esplhpc-cp090 hpc/sbatch_compare.sh liver liv1
#
# The job CUDA-checks its GPUs first (and exits non-zero at once if they cannot initialise, so
# the allocation is not held by a batch that can only fail), then runs hpc/compare_batch_local.sh
# on the job's two GPUs (indices 0 and 1 inside the allocation). Ports default to 8002/8003 so a
# second batch of this user on another node (pid/log files are keyed by preset+port in
# hpc/logs/) cannot clobber this one. Progress: hpc/logs/batch_<tag>.log.
#SBATCH --job-name=cest_compare
#SBATCH --partition=gpu
#SBATCH --cpus-per-task=12
#SBATCH --mem=50G
#SBATCH --time=08:00:00
#SBATCH --exclude=esplhpc-cp087,esplhpc-cp088,esplhpc-cp089
#SBATCH --output=hpc/logs/sbatch_%x_%j.out
set -uo pipefail
BODY="${1:?usage: sbatch [--gres=...] hpc/sbatch_compare.sh <body part> <tag>}"
TAG="${2:?tag}"
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
echo "[$(date '+%F %T')] job ${SLURM_JOB_ID:-?} on $(hostname): ${BODY} / ${TAG}; CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-unset}"
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv
source /etc/profile.d/modules.sh
module load miniconda3/23.11.0-2
eval "$(conda shell.bash hook)"
conda activate llm-serve
if ! python -c 'import torch; torch.cuda.init(); n = torch.cuda.device_count(); print("CUDA_OK", n); raise SystemExit(0 if n >= 2 else 1)'; then
    echo "[$(date '+%F %T')] CUDA cannot initialise on $(hostname); giving the allocation back" >&2
    exit 3
fi
conda deactivate
PORT_A="${PORT_A:-8002}" PORT_B="${PORT_B:-8003}" REPLICATES="${REPLICATES:-1}" PHASES_OVERRIDE="${PHASES_OVERRIDE:-}" \
    bash hpc/compare_batch_local.sh 0 1 "${BODY}" "${TAG}"
