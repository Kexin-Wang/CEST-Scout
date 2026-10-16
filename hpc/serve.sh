#!/usr/bin/env bash
# Start an OpenAI-compatible vLLM server for one of the presets in hpc/presets/, ON a GPU node.
#
#   LLM_PRESET=qwen3.8-27b-fp8 bash hpc/serve.sh
#
# A preset fixes what is model-specific and must not be guessed: the weights directory, the
# served model name, the tool-call parser (the wrong parser makes the agents silently stop using
# their tools -- see hpc/README.md), the reasoning parser, whether the model needs
# --trust-remote-code, and how many GPUs its weights need. Everything a preset sets can be
# overridden from the environment, since presets use `: "${VAR:=default}"`.
#
# Deployment knobs (all optional):
#   LLM_PORT=8000            port to listen on
#   LLM_TP_SIZE              tensor-parallel size; preset default is the minimum that fits the weights
#   LLM_MAX_LEN              max context length (tokens)
#   LLM_GPU_UTIL=0.95        fraction of each GPU's memory vLLM may use
#   LLM_GPU_DEVICE           CUDA_VISIBLE_DEVICES, to pin this instance to specific GPU(s) of the
#                            allocation so a second instance can run on the others
#   LLM_ROPE_SCALING         JSON merged into --hf-overrides as rope_scaling, for models whose
#                            native window is too short (Qwen3-32B's YaRN extension); not needed
#                            for the Qwen3.5+/Kimi-Linear presets, whose native window is >= 256K
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${LLM_PRESET:-}" ]]; then
    echo "Set LLM_PRESET to one of:" >&2
    ls "${HERE}/presets" | sed 's/\.sh$//; s/^/  /' >&2
    exit 1
fi
PRESET_FILE="${HERE}/presets/${LLM_PRESET}.sh"
if [[ ! -f "${PRESET_FILE}" ]]; then
    echo "No such preset: ${PRESET_FILE}" >&2
    exit 1
fi
# shellcheck source=/dev/null
source "${PRESET_FILE}"

PORT="${LLM_PORT:-8000}"
GPU_UTIL="${LLM_GPU_UTIL:-0.95}"
API_KEY_FILE="${HOME}/hpc-llm/.api_key"

if [[ -n "${LLM_GPU_DEVICE:-}" ]]; then
    export CUDA_VISIBLE_DEVICES="${LLM_GPU_DEVICE}"
fi

if [[ ! -d "${LLM_MODEL_DIR}" ]]; then
    echo "Model not found at ${LLM_MODEL_DIR}. Run: bash hpc/download_model.sh ${LLM_PRESET}  (on the login node)" >&2
    exit 1
fi

if ! command -v nvidia-smi &>/dev/null || ! nvidia-smi &>/dev/null; then
    echo "No GPU visible on this node. Get a GPU allocation first, e.g.:" >&2
    echo "  salloc -p gpu --gres=gpu:h100:2 --cpus-per-task=12 --mem=50G --time=08:00:00" >&2
    exit 1
fi

mkdir -p "$(dirname "${API_KEY_FILE}")"
if [[ ! -f "${API_KEY_FILE}" ]]; then
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "${API_KEY_FILE}"
    chmod 600 "${API_KEY_FILE}"
fi
API_KEY=$(cat "${API_KEY_FILE}")

# vLLM's flashinfer sampling kernel JIT-compiles on first use and needs nvcc; the toolkit
# version must match (or exceed) the CUDA version PyTorch was built against (currently cu130).
source /etc/profile.d/modules.sh
module load cuda13.0/toolkit/13.0.2

source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate llm-serve

ARGS=(
    serve "${LLM_MODEL_DIR}"
    --served-model-name "${LLM_SERVED_NAME}"
    --tensor-parallel-size "${LLM_TP_SIZE}"
    --host 0.0.0.0
    --port "${PORT}"
    --api-key "${API_KEY}"
    --max-model-len "${LLM_MAX_LEN}"
    --gpu-memory-utilization "${GPU_UTIL}"
    --enable-auto-tool-choice
    --tool-call-parser "${LLM_TOOL_PARSER}"
)
if [[ -n "${LLM_REASONING_PARSER:-}" ]]; then
    ARGS+=(--reasoning-parser "${LLM_REASONING_PARSER}")
fi
if [[ -n "${LLM_ROPE_SCALING:-}" ]]; then
    ARGS+=(--hf-overrides "{\"rope_scaling\": ${LLM_ROPE_SCALING}}")
fi
ARGS+=(${LLM_EXTRA_ARGS[@]+"${LLM_EXTRA_ARGS[@]}"})

NODE=$(hostname)
echo "=============================================================="
echo "Preset ${LLM_PRESET}: ${LLM_SERVED_NAME} from ${LLM_MODEL_DIR}"
echo "Node ${NODE}:${PORT}   TP=${LLM_TP_SIZE}   max context ${LLM_MAX_LEN}   GPUs ${CUDA_VISIBLE_DEVICES:-all}"
echo "Tool parser: ${LLM_TOOL_PARSER}   Reasoning parser: ${LLM_REASONING_PARSER:-none}"
echo ""
echo "Point CEST_Scout at it with (see hpc/run_with_server.sh):"
echo "  OPENAI_BASE_URL=http://${NODE}:${PORT}/v1   model name: ${LLM_SERVED_NAME}"
echo "=============================================================="

exec vllm "${ARGS[@]}"
