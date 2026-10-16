#!/usr/bin/env bash
# Run the CEST_Scout pipeline with ALL FOUR agents pointed at one running server, tagging the
# output files with the preset name so runs of different models don't collide. Usage:
#
#   bash hpc/run_with_server.sh <preset> <endpoint-url> <body part> [extra cli args]
#   e.g. bash hpc/run_with_server.sh qwen3.8-27b-fp8 http://esplhpc-cp087:8000/v1 liver
#
# For the Qwen3-32B two-server layout, set PHYSICIST_ENDPOINT to the larger-context instance the
# physicist and experimentalist agents should use:
#   PHYSICIST_ENDPOINT=http://esplhpc-cp087:8001/v1 bash hpc/run_with_server.sh qwen3-32b-fp8 http://esplhpc-cp087:8000/v1 liver
#
# Environment variables set here take precedence over .env (python-dotenv never overrides
# variables that are already exported), so .env can keep describing the default setup.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRESET="${1:?usage: run_with_server.sh <preset> <endpoint-url> <body part>}"
ENDPOINT="${2:?endpoint url, e.g. http://node:8000/v1}"
BODY_PART="${3:?body part}"
shift 3

# shellcheck source=/dev/null
source "${HERE}/presets/${PRESET}.sh"

API_KEY=$(cat "${HOME}/hpc-llm/.api_key")
export OPENAI_BASE_URL="${ENDPOINT}"
export OPENAI_API_KEY="${API_KEY}"
export PHYSICIST_OPENAI_BASE_URL="${PHYSICIST_ENDPOINT:-${ENDPOINT}}"
export PHYSICIST_OPENAI_API_KEY="${API_KEY}"
export OPENAI_MODEL_LITERATURE="${LLM_SERVED_NAME}"
export OPENAI_MODEL_CHEMIST="${LLM_SERVED_NAME}"
export OPENAI_MODEL_PHYSICIST="${LLM_SERVED_NAME}"
export OPENAI_MODEL_EXPERIMENTALIST="${LLM_SERVED_NAME}"
export LLM_DISABLE_THINKING="${LLM_DISABLE_THINKING}"
export LLM_THINKING_KWARG="${LLM_THINKING_KWARG:-enable_thinking}"
export PHYSICIST_LLM_DISABLE_THINKING="${LLM_DISABLE_THINKING}"

cd "${HERE}/.."
source /etc/profile.d/modules.sh
module load miniconda3/23.11.0-2
# pipeline.py records `git rev-parse HEAD` in the run's provenance; compute nodes here have git
# only as a module, and without it the code-version column of a comparison is blank.
command -v git > /dev/null 2>&1 || module load git/2.33.1 2> /dev/null || true
eval "$(conda shell.bash hook)"
conda activate CEST_Scout

# Bounded BLAS threads: the deterministic sweeps call expm on small matrices, and unbounded numpy
# threads on a shared 16-CPU step oversubscribe (two concurrent runs took 2 h each instead of
# 15 min on 2026-10-13 with 31 threads apiece).
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}" OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-4}" MKL_NUM_THREADS="${MKL_NUM_THREADS:-4}"

# CEST_SCOUT_MODULE=cest_scout.vanilla runs the no-tools baseline with the same served model.
exec python -m "${CEST_SCOUT_MODULE:-cest_scout.cli}" "${BODY_PART}" --run-tag "${PRESET}" "$@"
