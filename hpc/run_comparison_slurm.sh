#!/usr/bin/env bash
# Run the three-Qwen comparison for one body part unattended inside an existing SLURM GPU job,
# detached from the terminal that launched it (survives the user's disconnect):
#
#   nohup setsid bash hpc/run_comparison_slurm.sh <jobid> "<body part>" <tag> > hpc/logs/batch_<tag>.out 2>&1 &
#   e.g. nohup setsid bash hpc/run_comparison_slurm.sh 6038525 "intervertebral disc" ivd > hpc/logs/batch_ivd.out 2>&1 &
#
# Phase 1: Qwen3.8-27B (GPU 0, PORT_A) and Qwen3.6-35B-A3B (GPU 1, PORT_B) served and run at the
#          same time; the pipeline itself runs ON the compute node (srun --overlap), whose CPUs are
#          dedicated to the job -- the shared login node is far too loaded for the Bloch-McConnell
#          sweeps.
# Phase 2: the Qwen3-32B baseline in its two-instance YaRN layout (64K on GPU 0, 128K on GPU 1).
# Phase 3: cest_scout.compare over the three runs -> outputs/compare_<slug>_<tag>.{md,html}.
#
# Env: PORT_A/PORT_B (default 8002/8003 -- the shared H100 boxes may have other users' or
# sessions' servers on 8000/8001); SKIP_PHASE1=1 waits for phase-1 pipelines already running
# (e.g. started by an earlier controller) instead of starting them, then continues with phase 2.
# Servers are (re)started by this script so they belong to its own process group, not to the
# interactive session that may vanish. Progress: hpc/logs/batch_<tag>.log.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
JOBID="${1:?usage: run_comparison_slurm.sh <jobid> <body part> <tag>}"
BODY="${2:?body part}"
TAG="${3:?tag}"
PORT_A="${PORT_A:-8002}"
PORT_B="${PORT_B:-8003}"
LOGDIR="${HERE}/logs"
mkdir -p "${LOGDIR}"
BATCHLOG="${LOGDIR}/batch_${TAG}.log"
SLUG=$(python3 -c "import sys; print(''.join(c if c.isalnum() else '_' for c in sys.argv[1].strip().lower()).strip('_'))" "${BODY}")

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "${BATCHLOG}"; }

node() { squeue -j "${JOBID}" -h -o "%N"; }

port_free() {  # <port>: true once nothing on the node answers on it
    ! curl -sf -m 3 -H "Authorization: Bearer $(cat "${HOME}/hpc-llm/.api_key" 2>/dev/null)" \
        "http://$(node):$1/v1/models" > /dev/null 2>&1
}

serve() {  # <preset> <gpus> <port> [env assignments...]
    local preset="$1" gpus="$2" port="$3"; shift 3
    bash "${HERE}/stop_server.sh" "${preset}" "${port}" > /dev/null 2>&1 || true
    rm -f "${LOGDIR}/${preset}_${port}.pid"
    # A server from an earlier session may still be answering while it shuts down; starting now
    # would fail with "Address already in use" while the health check is fooled by the old one.
    for _ in $(seq 1 30); do
        port_free "${port}" && break
        sleep 5
    done
    port_free "${port}" || { log "port ${port} is still in use on $(node); cannot start ${preset}"; return 1; }
    log "starting ${preset} on GPU ${gpus} port ${port}"
    if ! env "$@" bash "${HERE}/start_server.sh" "${preset}" "${JOBID}" "${gpus}" "${port}" >> "${BATCHLOG}" 2>&1; then
        log "FAILED to start ${preset}"; return 1
    fi
}

run_on_node() {  # <preset> <endpoint> [physicist endpoint]
    local preset="$1" endpoint="$2" phys="${3:-}"
    local out="${LOGDIR}/run_${preset}_${SLUG}_${TAG}.log"
    log "running ${preset} on ${BODY} (log ${out})"
    PHYSICIST_ENDPOINT="${phys}" srun --jobid="${JOBID}" --overlap --export=ALL \
        bash "${HERE}/run_with_server.sh" "${preset}" "${endpoint}" "${BODY}" > "${out}" 2>&1
    local rc=$?
    log "${preset}: exit ${rc}; $(grep -m1 'Run took' "${out}" || echo 'no summary')"
    return ${rc}
}

NODE=$(node)
[[ -n "${NODE}" ]] || { log "job ${JOBID} is not running"; exit 1; }
log "batch ${TAG}: ${BODY} on ${NODE} (job ${JOBID}); ports ${PORT_A}/${PORT_B}"

# --- Phase 1: two single-GPU presets side by side --------------------------------------------
if [[ "${SKIP_PHASE1:-0}" == "1" ]]; then
    log "SKIP_PHASE1: waiting for already-running phase-1 pipelines to finish"
    while pgrep -u "${USER}" -f "run_with_server.sh (qwen3.8-27b-fp8|qwen3.6-35b-a3b-fp8) " > /dev/null 2>&1; do
        sleep 30
    done
    log "phase-1 pipelines finished"
else
    serve qwen3.8-27b-fp8 0 "${PORT_A}" && serve qwen3.6-35b-a3b-fp8 1 "${PORT_B}"
    run_on_node qwen3.8-27b-fp8 "http://${NODE}:${PORT_A}/v1" &
    P1=$!
    run_on_node qwen3.6-35b-a3b-fp8 "http://${NODE}:${PORT_B}/v1" &
    P2=$!
    wait ${P1} ${P2}
fi
# Stop whatever this user's servers for these presets are (pid files), on any port used so far.
for port in 8000 8001 "${PORT_A}" "${PORT_B}"; do
    for preset in qwen3.8-27b-fp8 qwen3.6-35b-a3b-fp8; do
        [[ -f "${LOGDIR}/${preset}_${port}.pid" ]] && bash "${HERE}/stop_server.sh" "${preset}" "${port}" >> "${BATCHLOG}" 2>&1
    done
done
sleep 10

# --- Phase 2: the Qwen3-32B two-instance layout -----------------------------------------------
serve qwen3-32b-fp8 0 "${PORT_A}" LLM_MAX_LEN=65536 LLM_ROPE_SCALING='{"rope_type":"yarn","factor":2.0,"original_max_position_embeddings":32768}' &
S1=$!
serve qwen3-32b-fp8 1 "${PORT_B}" LLM_MAX_LEN=131072 LLM_ROPE_SCALING='{"rope_type":"yarn","factor":4.0,"original_max_position_embeddings":32768}' &
S2=$!
wait ${S1} ${S2}
run_on_node qwen3-32b-fp8 "http://${NODE}:${PORT_A}/v1" "http://${NODE}:${PORT_B}/v1"
bash "${HERE}/stop_server.sh" qwen3-32b-fp8 "${PORT_A}" >> "${BATCHLOG}" 2>&1
bash "${HERE}/stop_server.sh" qwen3-32b-fp8 "${PORT_B}" >> "${BATCHLOG}" 2>&1

# --- Phase 3: compare the newest run of each preset for this body part -----------------------
cd "${ROOT}"
source /etc/profile.d/modules.sh
module load miniconda3/23.11.0-2
eval "$(conda shell.bash hook)"
conda activate CEST_Scout
FILES=()
for slugp in qwen3_32b_fp8 qwen3_8_27b_fp8 qwen3_6_35b_a3b_fp8; do
    f=$(ls -t outputs/${SLUG}_${slugp}_*.json 2> /dev/null | grep -v _resim_ | head -1)
    [[ -n "${f}" ]] && FILES+=("${f}") || log "no output for ${slugp}"
done
if (( ${#FILES[@]} >= 2 )); then
    python -m cest_scout.compare "${FILES[@]}" --out "outputs/compare_${SLUG}_${TAG}" > /dev/null 2>> "${BATCHLOG}" \
        && log "comparison written: outputs/compare_${SLUG}_${TAG}.{md,html}"
fi
log "batch ${TAG} finished"
