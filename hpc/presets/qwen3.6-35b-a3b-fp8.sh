# Qwen3.6-35B-A3B-FP8 (Apr 2026): sparse mixture-of-experts Qwen, 35B total / ~3B active params
# per token, FP8, ~36 GB of weights. Same hybrid-attention lineage and 262144 native context as
# Qwen3.5/3.8. Much faster per token than the dense models; the question the comparison asks is
# whether the small active parameter count costs agentic reliability.
: "${LLM_HF_REPO:=Qwen/Qwen3.6-35B-A3B-FP8}"
: "${LLM_MODEL_DIR:=${HOME}/models/Qwen3.6-35B-A3B-FP8}"
: "${LLM_SERVED_NAME:=qwen3.6-35b-a3b}"
: "${LLM_TOOL_PARSER:=qwen3_coder}"
: "${LLM_REASONING_PARSER:=qwen3}"
: "${LLM_TP_SIZE:=1}"
: "${LLM_MAX_LEN:=131072}"
: "${LLM_DISABLE_THINKING:=true}"
# --max-num-seqs: see qwen3.8-27b-fp8.sh -- linear-attention state blocks are per sequence.
LLM_EXTRA_ARGS=(--language-model-only --max-num-seqs "${LLM_MAX_NUM_SEQS:-32}")
