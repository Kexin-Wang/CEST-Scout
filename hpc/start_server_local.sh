#!/usr/bin/env bash
# ON a GPU node you are logged into directly (a shared node, no SLURM step around you): start
# hpc/serve.sh for one preset, pinned to the given GPUs, and block until the server answers.
#
#   bash hpc/start_server_local.sh <preset> <gpu-indices> <port>
#   e.g. bash hpc/start_server_local.sh glm-4.7-flash 1 8000
#        bash hpc/start_server_local.sh gemma-4-31b-it 1,2 8000        # TP=2 needs two GPUs
#
# GPU indices are nvidia-smi's and are translated to UUIDs before launch, so they mean the same
# GPUs whatever CUDA_VISIBLE_DEVICES the login shell inherited (on a shared node it may list every
# GPU of the box as a UUID). A GPU that already has more than 1 GB in use -- someone else's job --
# is refused rather than shared. Logs go to hpc/logs/<preset>_<port>.log and the server pid to
# hpc/logs/<preset>_<port>.pid, so hpc/stop_server.sh works unchanged. Prints the endpoint URL.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRESET="${1:?usage: start_server_local.sh <preset> <gpu-indices> <port>}"
GPUS="${2:?gpu indices, e.g. 1 or 1,2}"
PORT="${3:?port}"
TIMEOUT_S="${SERVER_START_TIMEOUT_S:-1800}"

[[ -f "${HERE}/presets/${PRESET}.sh" ]] || { echo "No such preset: ${PRESET}" >&2; exit 1; }
mkdir -p "${HERE}/logs"
LOG="${HERE}/logs/${PRESET}_${PORT}.log"
PIDFILE="${HERE}/logs/${PRESET}_${PORT}.pid"

if [[ -f "${PIDFILE}" ]] && kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    echo "A server for ${PRESET} on port ${PORT} is already running (pid $(cat "${PIDFILE}")); stop it first." >&2
    exit 1
fi
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${PORT}\$"; then
    echo "Port ${PORT} is already in use on $(hostname); pick another." >&2
    exit 1
fi

UUIDS=""
IFS=',' read -ra IDX <<< "${GPUS}"
for i in "${IDX[@]}"; do
    line=$(nvidia-smi --query-gpu=index,uuid,memory.used --format=csv,noheader,nounits | awk -F', ' -v i="${i}" '$1 == i')
    [[ -n "${line}" ]] || { echo "No GPU with index ${i} on this node." >&2; exit 1; }
    uuid=$(awk -F', ' '{print $2}' <<< "${line}")
    used=$(awk -F', ' '{print $3}' <<< "${line}")
    # A GPU with more than LLM_GPU_BUSY_MIB (default 1024) in use is someone else's; a SLURM
    # allocation sometimes hands over a GPU with a small stale context, so a batch may raise it.
    if (( used > ${LLM_GPU_BUSY_MIB:-1024} )); then
        echo "GPU ${i} (${uuid}) already has ${used} MiB in use -- someone else's job; pick another GPU." >&2
        exit 1
    fi
    UUIDS="${UUIDS:+${UUIDS},}${uuid}"
done

# serve.sh resolves conda through `conda info --base`; make sure that is the module's conda and
# not whatever condabin happens to be first on this shell's PATH.
source /etc/profile.d/modules.sh
module load miniconda3/23.11.0-2

# setsid: the server becomes its own session and process group, so stop_server.sh can end vLLM's
# engine subprocesses together with the API server instead of leaving them holding the GPU. The
# pid is written by the NEW process itself (`$$` after setsid, then `exec` keeps it through
# serve.sh into vllm): `$!` of a `setsid ... &` job is setsid's own pid, which exits at once
# when setsid has to fork, and a pidfile holding a dead pid made stop_server.sh report "Stopped"
# while the server lived on -- observed 2026-09-23, every later preset then found its port busy.
rm -f "${PIDFILE}"
LLM_PRESET="${PRESET}" LLM_GPU_DEVICE="${UUIDS}" LLM_PORT="${PORT}" \
    setsid bash -c 'echo $$ > "$0"; exec bash "$1"' "${PIDFILE}" "${HERE}/serve.sh" > "${LOG}" 2>&1 < /dev/null &
for _ in $(seq 1 50); do
    [[ -s "${PIDFILE}" ]] && break
    sleep 0.2
done
[[ -s "${PIDFILE}" ]] || { echo "Server process never wrote its pid file ${PIDFILE}." >&2; exit 1; }

API_KEY=$(cat "${HOME}/hpc-llm/.api_key" 2>/dev/null || true)
URL="http://$(hostname):${PORT}/v1"
echo "Starting ${PRESET} on $(hostname):${PORT} (GPUs ${GPUS} = ${UUIDS}); log: ${LOG}" >&2
for ((t = 0; t < TIMEOUT_S; t += 10)); do
    if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
        echo "Server process exited during startup. Last log lines:" >&2
        tail -30 "${LOG}" >&2
        rm -f "${PIDFILE}"
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
