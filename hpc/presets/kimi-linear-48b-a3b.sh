# Kimi-Linear-48B-A3B-Instruct (Moonshot AI, Oct 2025): the only Kimi model that fits a 2xH100
# allocation -- Kimi-K2.x/K3 are 1-2.8T-parameter models. Mixture-of-experts, 48B total / 3B
# active, bf16 (no FP8 checkpoint is published), ~98 GB of weights, so it needs both GPUs
# (TP=2). Kimi Delta Attention (linear attention, 3:1 with full attention) gives it a 1M-token
# native window; we cap at 131072 to match the other presets.
: "${LLM_HF_REPO:=moonshotai/Kimi-Linear-48B-A3B-Instruct}"
: "${LLM_MODEL_DIR:=${HOME}/models/Kimi-Linear-48B-A3B-Instruct}"
: "${LLM_SERVED_NAME:=kimi-linear-48b-a3b}"
# Its chat template emits Kimi-K2's <|tool_calls_section_begin|><|tool_call_begin|> format.
: "${LLM_TOOL_PARSER:=kimi_k2}"
# Instruct model, no thinking mode -- nothing to strip.
: "${LLM_REASONING_PARSER:=}"
: "${LLM_TP_SIZE:=2}"
: "${LLM_MAX_LEN:=131072}"
: "${LLM_DISABLE_THINKING:=false}"
# The model card's own vllm serve command passes --trust-remote-code: the architecture is native
# to vLLM 0.29, but the repo ships its own tokenizer code (tiktoken-based) that transformers must
# execute to load it. This runs Python from the moonshotai/Kimi-Linear-48B-A3B-Instruct repo.
# --max-num-seqs: see qwen3.8-27b-fp8.sh -- linear-attention state blocks are per sequence.
LLM_EXTRA_ARGS=(--trust-remote-code --max-num-seqs "${LLM_MAX_NUM_SEQS:-32}")
