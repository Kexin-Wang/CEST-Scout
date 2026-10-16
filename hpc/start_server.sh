#!/usr/bin/env bash
# From the LOGIN node: start hpc/serve.sh inside an existing SLURM GPU allocation and block until
# the server answers. Usage:
#
#   bash hpc/start_server.sh <preset> <jobid> <gpu-devices> <port>
#   e.g. bash hpc/start_server.sh qwen3.8-27b-fp8 6034621 0 8000
#        bash hpc/start_server.sh kimi-linear-48b-a3b 6034621 0,1 8000
#
# Logs go to hpc/logs/<preset>_<port>.log; the srun PID is written to hpc/logs/<preset>_<port>.pid
# so hpc/stop_server.sh can end it. Prints the endpoint URL on success.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRESET="${1:?usage: start_server.sh <preset> <jobid> <gpu-devices> <port>}"
JOBID="${2:?jobid}"
GPUS="${3:?gpu devices, e.g. 0 or 0,1}"
PORT="${4:?port}"
TIMEOUT_S="${SERVER_START_TIMEOUT_S:-1800}"

mkdir -p "${HERE}/logs"
LOG="${HERE}/logs/${PRESET}_${PORT}.log"
PIDFILE="${HERE}/logs/${PRESET}_${PORT}.pid"

NODE=$(squeue -j "${JOBID}" -h -o "%N")
if [[ -z "${NODE}" ]]; then
    echo "Job ${JOBID} is not running." >&2
    exit 1
fi

# --overlap lets this step share the GPUs the interactive allocation already holds. The variables
# are set in srun's own environment and propagated with --export=ALL, not listed inline after
# --export=, because srun splits that list at commas and would cut a GPU list like "0,1" in two
# (observed: a TP=2 server then saw one GPU and refused to start).
LLM_PRESET="${PRESET}" LLM_GPU_DEVICE="${GPUS}" LLM_PORT="${PORT}" \
    srun --jobid="${JOBID}" --overlap --export=ALL bash "${HERE}/serve.sh" > "${LOG}" 2>&1 &
echo $! > "${PIDFILE}"

API_KEY=$(cat "${HOME}/hpc-llm/.api_key" 2>/dev/null || true)
URL="http://${NODE}:${PORT}/v1"
echo "Starting ${PRESET} on ${NODE}:${PORT} (GPUs ${GPUS}); log: ${LOG}" >&2
for ((t = 0; t < TIMEOUT_S; t += 10)); do
    if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
        echo "Server process exited during startup. Last log lines:" >&2
        tail -30 "${LOG}" >&2
        exit 1
    fi
    if curl -sf -m 5 -H "Authorization: Bearer ${API_KEY}" "${URL}/models" > /dev/null 2>&1; then
        echo "${URL}"
        exit 0
    fi
    sleep 10
done
echo "Server did not come up within ${TIMEOUT_S}s. Last log lines:" >&2
tail -30 "${LOG}" >&2
exit 1
