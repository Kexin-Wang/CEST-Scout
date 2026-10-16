# DeepSeek-V4-Flash (June 2026): the smaller of the two DeepSeek V4 releases -- a sparse MoE with
# 256 routed experts (6 active per token) plus one shared expert, 43 layers, a 1M-token native
# window, shipped as block-quantized FP8 (e4m3 weights, ue8m0 scales, 128x128 blocks; ~160 GB).
# Needs Hopper-class GPUs for this FP8 format: two H200 (282 GB) hold the weights with ~100 GB of
# KV cache; four H200 are more comfortable. Not runnable on the 48 GB L40S.
: "${LLM_HF_REPO:=deepseek-ai/DeepSeek-V4-Flash}"
: "${LLM_MODEL_DIR:=${HOME}/models/DeepSeek-V4-Flash}"
: "${LLM_SERVED_NAME:=deepseek-v4-flash}"
# Tool-call / reasoning parsers: set from what the installed vLLM registers for the V4 chat
# template (checked at preset creation, see hpc/README.md); overridable from the environment.
: "${LLM_TOOL_PARSER:=deepseek_v4}"
: "${LLM_REASONING_PARSER:=deepseek_v4}"
: "${LLM_TP_SIZE:=2}"
: "${LLM_MAX_LEN:=131072}"
: "${LLM_DISABLE_THINKING:=true}"
# vLLM 0.29's DeepSeek V4 attention ("fp8_ds_mla" MLA layout) only runs with an FP8 KV cache;
# with the default "auto" the workers abort at model load ("only supports fp8 kv-cache, got auto").
LLM_EXTRA_ARGS=(--trust-remote-code --max-num-seqs "${LLM_MAX_NUM_SEQS:-16}" --kv-cache-dtype "${LLM_KV_CACHE_DTYPE:-fp8_ds_mla}")
