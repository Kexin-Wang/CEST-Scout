#!/usr/bin/env bash
# Run a whole model comparison unattended on a shared GPU node you are logged into directly (no
# SLURM step), using two free GPUs. Presets are served in pairs (one per GPU) or alone (TP=2, or
# the Qwen3-32B two-server layout); every server is smoke-tested (hpc/smoke_test.py) before a
# 10-30 minute pipeline run is spent on it; presets whose weights are still downloading are
# waited for; and at the end every run of the batch is compared with cest_scout.compare.
#
#   nohup bash hpc/compare_batch_local.sh <gpuA> <gpuB> <body part> <batch tag> \
#       > hpc/logs/batch_<tag>.out 2>&1 &
#   e.g. nohup bash hpc/compare_batch_local.sh 1 2 liver batch20260923 > hpc/logs/batch_batch20260923.out 2>&1 &
#   REPLICATES=2 runs the pipeline twice per model on the same server (within-model spread);
#   PORT_A/PORT_B pick the ports when 8000/8001 are taken by someone else on a shared node.
#
# Batch log: hpc/logs/batch_<tag>.log (progress + per-preset outcome). Per-run logs:
# hpc/logs/run_<preset>_<body>_<tag>.log; smoke tests: hpc/logs/smoke_<preset>_<tag>.log.
# Comparison: outputs/compare_<body>_<tag>.{md,html}. The phase plan is the PHASES list below.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
GPU_A="${1:?usage: compare_batch_local.sh <gpuA> <gpuB> <body part> <batch tag>}"
GPU_B="${2:?gpuB}"
BODY="${3:?body part}"
TAG="${4:?batch tag}"
# The CLI slugs the body part into the output file names (cest_scout.cli._slug: lower-case,
# non-alphanumerics -> '_'), so "intervertebral disc" writes intervertebral_disc_<preset>_*.json.
# Use the same slug for our own log/compare file names and for finding the runs to compare.
BODY_SLUG=$(printf '%s' "${BODY}" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9\n' '_' | sed 's/^_*//; s/_*$//')
PORT_A="${PORT_A:-8000}"
PORT_B="${PORT_B:-8001}"
# Runs of the pipeline per served model. Local-model runs are stochastic (each model samples at
# its shipped default temperature), so one run per model cannot separate a model trait from
# run-to-run noise; with REPLICATES=2 or more the comparison page reports within-model spread and
# replicate agreement next to the between-model differences. The server is started once per
# preset and the replicates run back to back on it, so replicates cost only pipeline time.
REPLICATES="${REPLICATES:-1}"
# Where the runs and the comparison are written. A batch on different hardware (e.g. L40S with
# TP=2 instead of H100/H200) should use its own directory so its runs are never swept into another
# batch's comparison, which collects every run of the same tissue newer than its start stamp.
OUTPUT_DIR="${OUTPUT_DIR:-${ROOT}/outputs}"

LOGDIR="${HERE}/logs"
mkdir -p "${LOGDIR}"
BATCHLOG="${LOGDIR}/batch_${TAG}.log"
STAMP_FILE="${LOGDIR}/batch_${TAG}.started"
# Re-running a tag (e.g. only the phases that failed) keeps the original stamp, so the final
# comparison still picks up the runs the first attempt finished.
[[ -e "${STAMP_FILE}" ]] || touch "${STAMP_FILE}"

# The plan. "pair:A:B" serves A on GPU_A and B on GPU_B at the same time (single-GPU presets);
# "pair:A" serves A alone; "tp2:A" gives A both GPUs; "qwen32b" is the two-instance YaRN layout
# documented in hpc/README.md. Kimi-Linear is deliberately absent (see hpc/README.md).
PHASES=(
    "pair:qwen3.8-27b-fp8:qwen3.6-35b-a3b-fp8"
    "pair:glm-4.7-flash:nemotron-3-nano-30b-a3b-fp8"
    "pair:gpt-oss-120b"
    "tp2:gemma-4-31b-it"
    "qwen32b"
)
# PHASES_OVERRIDE="pair:glm-4.7-flash:nemotron-3-nano-30b-a3b-fp8 qwen32b" runs only those phases
# (space-separated), e.g. to redo the ones an earlier attempt of the same tag did not finish.
if [[ -n "${PHASES_OVERRIDE:-}" ]]; then
    read -r -a PHASES <<< "${PHASES_OVERRIDE}"
fi
PRESETS_ALL=(qwen3.8-27b-fp8 qwen3.6-35b-a3b-fp8 glm-4.7-flash nemotron-3-nano-30b-a3b-fp8 gpt-oss-120b gemma-4-31b-it qwen3-32b-fp8)

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "${BATCHLOG}"; }
set_status() { printf '%s\n' "$2" > "${LOGDIR}/status_${1}_${TAG}"; log "$1: $2"; }

source /etc/profile.d/modules.sh
module load miniconda3/23.11.0-2
command -v git > /dev/null 2>&1 || module load git/2.33.1 2> /dev/null || true

preset_var() {  # <preset> <variable>: a preset's value, read in a subshell so presets never leak
    ( source "${HERE}/presets/$1.sh"; eval "printf '%s' \"\${$2:-}\"" )
}

wait_download() {  # <preset>: block while `hf download <repo>` is alive, then check completeness
    local repo dir waited=0
    repo=$(preset_var "$1" LLM_HF_REPO)
    dir=$(preset_var "$1" LLM_MODEL_DIR)
    while pgrep -u "${USER}" -f "hf download ${repo} " > /dev/null 2>&1; do
        (( waited % 300 == 0 )) && log "$1: waiting for the download of ${repo} ($(du -sh "${dir}" 2> /dev/null | cut -f1) so far)"
        sleep 30
        waited=$(( waited + 30 ))
    done
    if [[ ! -d "${dir}" ]] || find "${dir}" -name '*.incomplete' -print -quit | grep -q .; then
        set_status "$1" "SKIPPED: download of ${repo} is incomplete (${dir})"
        return 1
    fi
    if ! python3 - "${dir}" <<'PY'
import glob, json, os, sys
d = sys.argv[1]
idx = os.path.join(d, "model.safetensors.index.json")
if os.path.exists(idx):
    need = set(json.load(open(idx))["weight_map"].values())
    have = {os.path.basename(p) for p in glob.glob(os.path.join(d, "*.safetensors"))}
    missing = sorted(need - have)
    if missing:
        print("missing shards:", missing[:5], "..." if len(missing) > 5 else "")
        sys.exit(1)
elif not glob.glob(os.path.join(d, "*.safetensors")):
    print("no safetensors in", d)
    sys.exit(1)
PY
    then
        set_status "$1" "SKIPPED: ${dir} is missing weight shards"
        return 1
    fi
    return 0
}

start_server() {  # <preset> <gpus> <port> -> URL on stdout
    bash "${HERE}/start_server_local.sh" "$1" "$2" "$3" 2>> "${BATCHLOG}"
}

smoke() {  # <preset> <url>
    local name dis flag=""
    name=$(preset_var "$1" LLM_SERVED_NAME)
    dis=$(preset_var "$1" LLM_DISABLE_THINKING)
    [[ "${dis}" == "true" ]] && flag="--disable-thinking"
    (
        eval "$(conda shell.bash hook)"
        conda activate CEST_Scout
        cd "${ROOT}"
        timeout 900 python hpc/smoke_test.py "$2" "${name}" ${flag}
    ) > "${LOGDIR}/smoke_${1}_${TAG}.log" 2>&1
}

run_pipeline() {  # <preset> <url> [physicist url] [replicate index]
    local suffix=""
    (( REPLICATES > 1 )) && suffix="_r${4:-1}"
    PHYSICIST_ENDPOINT="${3:-$2}" bash "${HERE}/run_with_server.sh" "$1" "$2" "${BODY}" --output-dir "${OUTPUT_DIR}" \
        > "${LOGDIR}/run_${1}_${BODY_SLUG}_${TAG}${suffix}.log" 2>&1
}

run_replicates() {  # <preset> <url> [physicist url]: run the pipeline REPLICATES times, record the outcome
    local p="$1" ok=0 r t0 suffix=""
    t0=${SECONDS}
    for r in $(seq 1 "${REPLICATES}"); do
        (( REPLICATES > 1 )) && { suffix="_r${r}"; log "${p}: replicate ${r}/${REPLICATES} on ${BODY}"; }
        if run_pipeline "${p}" "$2" "${3:-$2}" "${r}"; then
            ok=$(( ok + 1 ))
        else
            log "${p}: replicate ${r} exited non-zero (hpc/logs/run_${p}_${BODY_SLUG}_${TAG}${suffix}.log)"
        fi
    done
    if (( ok == REPLICATES )); then
        set_status "${p}" "OK: ${ok}/${REPLICATES} run(s) finished in $(( (SECONDS - t0) / 60 )) min"
    elif (( ok > 0 )); then
        set_status "${p}" "PARTIAL: ${ok}/${REPLICATES} run(s) finished in $(( (SECONDS - t0) / 60 )) min (see hpc/logs/run_${p}_${BODY_SLUG}_${TAG}*.log)"
    else
        set_status "${p}" "FAILED: pipeline exited non-zero in every replicate after $(( (SECONDS - t0) / 60 )) min (hpc/logs/run_${p}_${BODY_SLUG}_${TAG}*.log)"
    fi
}

stop_server() {  # <preset> <port> [gpu indices]: stop, then wait for the GPUs to be released
    local i used
    if ! bash "${HERE}/stop_server.sh" "$1" "$2" >> "${BATCHLOG}" 2>&1; then
        log "$1: stop_server.sh reported a problem on port $2 (see above); the next phase on that port/GPU will refuse to start"
    fi
    if [[ -n "${3:-}" ]]; then
        for _ in $(seq 1 18); do  # up to ~90 s
            used=0
            IFS=',' read -ra idx <<< "$3"
            for i in "${idx[@]}"; do
                (( $(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -i "${i}" 2> /dev/null || echo 99999) > 1024 )) && used=1
            done
            (( used == 0 )) && break
            sleep 5
        done
    fi
    sleep 5
}

serve_and_run() {  # <preset> <gpus> <port>: start, smoke-test, run the pipeline, stop
    local p="$1" gpus="$2" port="$3" url
    log "${p}: starting server on GPU(s) ${gpus}, port ${port}"
    if ! url=$(start_server "${p}" "${gpus}" "${port}"); then
        set_status "${p}" "FAILED: server did not start (hpc/logs/${p}_${port}.log)"
        return 1
    fi
    log "${p}: server up at ${url}; smoke test"
    if smoke "${p}" "${url}"; then
        log "${p}: smoke test passed; running the pipeline on ${BODY} (${REPLICATES} replicate(s))"
        run_replicates "${p}" "${url}"
    else
        set_status "${p}" "FAILED: smoke test (hpc/logs/smoke_${p}_${TAG}.log) -- pipeline not run"
    fi
    stop_server "${p}" "${port}" "${gpus}"
}

phase_pair() {  # <presetA> [presetB]
    local a="$1" b="${2:-}" pa pb=""
    { wait_download "${a}" && serve_and_run "${a}" "${GPU_A}" "${PORT_A}"; } &
    pa=$!
    if [[ -n "${b}" ]]; then
        { wait_download "${b}" && serve_and_run "${b}" "${GPU_B}" "${PORT_B}"; } &
        pb=$!
    fi
    wait "${pa}"
    [[ -n "${pb}" ]] && wait "${pb}"
}

phase_tp2() {  # <preset>
    wait_download "$1" && serve_and_run "$1" "${GPU_A},${GPU_B}" "${PORT_A}"
}

phase_qwen32b() {
    local p="qwen3-32b-fp8" urlA urlB
    wait_download "${p}" || return 1
    log "${p}: two YaRN-scaled instances -- 65536 tokens on GPU ${GPU_A}:${PORT_A} for literature+chemist, 131072 on GPU ${GPU_B}:${PORT_B} for physicist+experimentalist"
    (
        export LLM_MAX_LEN=65536 LLM_ROPE_SCALING='{"rope_type":"yarn","factor":2.0,"original_max_position_embeddings":32768}'
        start_server "${p}" "${GPU_A}" "${PORT_A}" > "${LOGDIR}/url_${p}_${PORT_A}_${TAG}"
    ) &
    local sa=$!
    (
        export LLM_MAX_LEN=131072 LLM_ROPE_SCALING='{"rope_type":"yarn","factor":4.0,"original_max_position_embeddings":32768}'
        start_server "${p}" "${GPU_B}" "${PORT_B}" > "${LOGDIR}/url_${p}_${PORT_B}_${TAG}"
    ) &
    local sb=$!
    wait "${sa}"; local ra=$?
    wait "${sb}"; local rb=$?
    urlA=$(cat "${LOGDIR}/url_${p}_${PORT_A}_${TAG}" 2> /dev/null)
    urlB=$(cat "${LOGDIR}/url_${p}_${PORT_B}_${TAG}" 2> /dev/null)
    if (( ra != 0 || rb != 0 )) || [[ -z "${urlA}" || -z "${urlB}" ]]; then
        set_status "${p}" "FAILED: one of the two servers did not start (hpc/logs/${p}_${PORT_A}.log, hpc/logs/${p}_${PORT_B}.log)"
        stop_server "${p}" "${PORT_A}"; stop_server "${p}" "${PORT_B}"
        return 1
    fi
    log "${p}: servers up at ${urlA} and ${urlB}; smoke test on the 131072 instance"
    if smoke "${p}" "${urlB}"; then
        log "${p}: smoke test passed; running the pipeline on ${BODY} (${REPLICATES} replicate(s))"
        run_replicates "${p}" "${urlA}" "${urlB}"
    else
        set_status "${p}" "FAILED: smoke test (hpc/logs/smoke_${p}_${TAG}.log) -- pipeline not run"
    fi
    stop_server "${p}" "${PORT_A}" "${GPU_A}"
    stop_server "${p}" "${PORT_B}" "${GPU_B}"
}

phase_compare() {
    # Every run of this batch (all replicates of every preset), in preset order then by time.
    local files=() p slug f
    for p in "${PRESETS_ALL[@]}"; do
        slug=$(printf '%s' "${p}" | tr -c 'a-z0-9\n' '_')   # cest_scout.cli._slug: non-alphanumerics -> '_'
        while IFS= read -r f; do
            [[ -n "${f}" ]] && files+=("${f}")
        done < <(find "${OUTPUT_DIR}" -maxdepth 1 -name "${BODY_SLUG}_${slug}_*.json" -newer "${STAMP_FILE}" ! -name '*_resim_*' | sort)
    done
    if (( ${#files[@]} < 2 )); then
        log "compare: fewer than two runs from this batch; nothing to compare"
        return 1
    fi
    log "compare: ${#files[@]} runs -> ${OUTPUT_DIR}/compare_${BODY_SLUG}_${TAG}.{md,html}"
    (
        eval "$(conda shell.bash hook)"
        conda activate CEST_Scout
        cd "${ROOT}"
        python -m cest_scout.compare "${files[@]}" --out "${OUTPUT_DIR}/compare_${BODY_SLUG}_${TAG}"
    ) >> "${BATCHLOG}" 2>&1
}

mkdir -p "${OUTPUT_DIR}"
log "batch ${TAG}: ${BODY} on $(hostname), GPUs ${GPU_A} and ${GPU_B}, ports ${PORT_A}/${PORT_B}, ${REPLICATES} replicate(s) per model, TP=${LLM_TP_SIZE:-preset}, outputs in ${OUTPUT_DIR}; code $(git -C "${ROOT}" rev-parse --short HEAD 2> /dev/null || echo n/a)"
for phase in "${PHASES[@]}"; do
    IFS=':' read -r kind a b <<< "${phase}"
    log "---- phase ${kind} ${a} ${b:-}"
    case "${kind}" in
        pair)    phase_pair "${a}" "${b:-}" ;;
        tp2)     phase_tp2 "${a}" ;;
        qwen32b) phase_qwen32b ;;
        *)       log "unknown phase kind ${kind}" ;;
    esac
done
phase_compare
log "---- batch ${TAG} finished. Outcomes:"
for p in "${PRESETS_ALL[@]}"; do
    log "  ${p}: $(cat "${LOGDIR}/status_${p}_${TAG}" 2> /dev/null || echo 'not run')"
done
