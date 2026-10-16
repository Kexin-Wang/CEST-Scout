#!/usr/bin/env bash
# Stop a server started by hpc/start_server.sh. Usage: bash hpc/stop_server.sh <preset> <port>
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRESET="${1:?usage: stop_server.sh <preset> <port>}"
PORT="${2:?port}"
PIDFILE="${HERE}/logs/${PRESET}_${PORT}.pid"

if [[ ! -f "${PIDFILE}" ]]; then
    echo "No pid file ${PIDFILE}" >&2
    exit 1
fi
PID=$(cat "${PIDFILE}")
if kill -0 "${PID}" 2>/dev/null; then
    # Started by start_server.sh: srun forwards the signal to the remote vllm process. Started by
    # start_server_local.sh (setsid, so the pid leads its own process group): signal the whole
    # group, which takes vLLM's engine subprocesses down with the API server. Then wait for the
    # GPUs to be released.
    if [[ "$(ps -o pgid= -p "${PID}" | tr -d ' ')" == "${PID}" ]]; then
        TARGET="-${PID}"
    else
        TARGET="${PID}"
    fi
    kill -TERM -- "${TARGET}"
    for _ in $(seq 1 60); do
        kill -0 "${PID}" 2>/dev/null || break
        sleep 2
    done
    kill -0 "${PID}" 2>/dev/null && kill -KILL -- "${TARGET}" || true
fi
rm -f "${PIDFILE}"
# A dead pid in the pidfile is not proof the server is gone (see start_server_local.sh): report
# a listener that survived on the port instead of claiming success.
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${PORT}\$"; then
    echo "WARNING: pid ${PID} is gone but something still listens on port ${PORT} on $(hostname):" >&2
    ss -ltnp 2>/dev/null | grep ":${PORT} " >&2 || true
    exit 1
fi
echo "Stopped ${PRESET} on port ${PORT}."
