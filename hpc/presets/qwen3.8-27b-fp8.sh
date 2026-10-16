# Qwen3.8-27B-FP8 (Aug 2026): newest dense Qwen at a size comparable to the Qwen3-32B baseline.
# 27.8B params, FP8, ~28 GB of weights, hybrid Gated-DeltaNet/full-attention layers (every 4th
# layer is full attention), 262144 native context -- so no YaRN is needed and one instance can
# serve all four agents.
: "${LLM_HF_REPO:=Qwen/Qwen3.8-27B-FP8}"
: "${LLM_MODEL_DIR:=${HOME}/models/Qwen3.8-27B-FP8}"
: "${LLM_SERVED_NAME:=qwen3.8-27b}"
# Qwen3.5+ moved to an XML tool-call format (<tool_call><function=name><parameter=...>); the model
# card's vLLM command names qwen3_coder as the parser. hermes (used for Qwen3) does NOT parse it.
: "${LLM_TOOL_PARSER:=qwen3_coder}"
: "${LLM_REASONING_PARSER:=qwen3}"
: "${LLM_TP_SIZE:=1}"
: "${LLM_MAX_LEN:=131072}"
# Thinking mode is on by default; we run every model in non-thinking ("instruct") mode so the
# comparison is like-for-like with the Qwen3-32B baseline.
: "${LLM_DISABLE_THINKING:=true}"
# The checkpoint is a vision-language model; we only use the text side, so skip loading the
# vision tower (a flag the model card documents for text-only serving).
# --max-num-seqs: every in-flight sequence of a linear-attention (Mamba-style) model pins a
# state block, and vLLM's default of 1024 concurrent sequences exceeds what fits next to the
# weights on one 80 GB GPU ("max_num_seqs (1024) exceeds available Mamba cache blocks"). The
# agent loop is strictly sequential, so a small cap costs nothing.
LLM_EXTRA_ARGS=(--language-model-only --max-num-seqs "${LLM_MAX_NUM_SEQS:-32}")
