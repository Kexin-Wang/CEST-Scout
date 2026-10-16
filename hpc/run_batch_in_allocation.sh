#!/usr/bin/env bash
# Run hpc/compare_batch_local.sh INSIDE an existing SLURM GPU allocation, detached from the
# terminal, so the whole seven-model comparison runs on the allocated node (servers, smoke tests,
# pipelines and the final comparison), with the node's CPUs for the Bloch-McConnell sweeps.
#
#   bash hpc/run_batch_in_allocation.sh <jobid> "<body part>" <tag> [gpuA gpuB]
#   e.g. REPLICATES=2 bash hpc/run_batch_in_allocation.sh 6050304 pancreas panc20260927
#
# Inside the job, CUDA_VISIBLE_DEVICES lists only the allocated GPUs, so nvidia-smi indices 0 and 1
# are the job's two GPUs (the default). Progress: hpc/logs/batch_<tag>.log; this wrapper's own
# output: hpc/logs/batch_<tag>.out.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOBID="${1:?usage: run_batch_in_allocation.sh <jobid> <body part> <tag> [gpuA gpuB]}"
BODY="${2:?body part}"
TAG="${3:?tag}"
GPU_A="${4:-0}"
GPU_B="${5:-1}"
mkdir -p "${HERE}/logs"
NODE=$(squeue -j "${JOBID}" -h -o "%N")
[[ -n "${NODE}" ]] || { echo "Job ${JOBID} is not running." >&2; exit 1; }
echo "batch ${TAG} (${BODY}) on ${NODE} in job ${JOBID}; log hpc/logs/batch_${TAG}.log" >&2
REPLICATES="${REPLICATES:-1}" PORT_A="${PORT_A:-8000}" PORT_B="${PORT_B:-8001}" PHASES_OVERRIDE="${PHASES_OVERRIDE:-}" \
    nohup setsid srun --jobid="${JOBID}" --overlap --export=ALL \
    bash "${HERE}/compare_batch_local.sh" "${GPU_A}" "${GPU_B}" "${BODY}" "${TAG}" \
    > "${HERE}/logs/batch_${TAG}.out" 2>&1 < /dev/null &
echo "srun pid $!" >&2
