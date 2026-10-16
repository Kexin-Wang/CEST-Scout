#!/usr/bin/env bash
# Download a preset's weights to $HOME/models (NFS-shared, so GPU nodes read them directly).
# Run on the login node, which has internet access. Usage: bash hpc/download_model.sh qwen3.8-27b-fp8
#
# --max-workers is kept low on purpose: the login node caps each process at ~1 GB of RAM, and the
# default worker count gets the download OOM-killed (exit 137) part-way through.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRESET="${1:?usage: download_model.sh <preset>  (see hpc/presets/)}"
# shellcheck source=/dev/null
source "${HERE}/presets/${PRESET}.sh"

source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate llm-serve

mkdir -p "${LLM_MODEL_DIR}"
# A preset may list repo folders that are other weight formats (e.g. gpt-oss's original/ and
# metal/) so only the safetensors vLLM loads are fetched. One --exclude per pattern: a second
# bare pattern after --exclude is read by `hf download` as an explicit filename instead.
EXCLUDE_ARGS=()
for pat in ${LLM_HF_EXCLUDE[@]+"${LLM_HF_EXCLUDE[@]}"}; do
    EXCLUDE_ARGS+=(--exclude "${pat}")
done
hf download "${LLM_HF_REPO}" --local-dir "${LLM_MODEL_DIR}" --max-workers "${HF_MAX_WORKERS:-4}" \
    ${EXCLUDE_ARGS[@]+"${EXCLUDE_ARGS[@]}"}

echo "Downloaded ${LLM_HF_REPO} to ${LLM_MODEL_DIR}"
du -sh "${LLM_MODEL_DIR}"
