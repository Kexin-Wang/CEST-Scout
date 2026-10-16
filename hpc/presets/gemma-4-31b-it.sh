# Gemma 4 31B instruction-tuned (Google, 2026): dense 31B vision-language checkpoint, bf16,
# 62.6 GB, Gemma license; native function calling. 60 layers, of which every 6th is full
# attention and the rest use a 1024-token sliding window, 16 KV heads x 256 dims: the full-
# attention KV cache alone is ~164 KB/token, so next to the weights ONE 80 GB GPU has room for
# only a ~80K-token window and the 131072 tokens the physicist/experimentalist loops need require
# TP=2 -- i.e. both GPUs of a 2xH100 allocation.
: "${LLM_HF_REPO:=google/gemma-4-31B-it}"
: "${LLM_MODEL_DIR:=${HOME}/models/gemma-4-31b-it}"
: "${LLM_SERVED_NAME:=gemma-4-31b-it}"
# vLLM 0.29 ships a gemma4 tool-call parser and reasoning parser for Gemma 4's
# <|tool_call>call:name{...}<tool_call|> and <|channel>thought ... <channel|> formats.
: "${LLM_TOOL_PARSER:=gemma4}"
: "${LLM_REASONING_PARSER:=gemma4}"
: "${LLM_TP_SIZE:=2}"
: "${LLM_MAX_LEN:=131072}"
# Gemma 4's chat template defaults enable_thinking to false, so the model is already in the
# non-thinking mode the comparison uses; sending enable_thinking=false explicitly is harmless.
: "${LLM_DISABLE_THINKING:=true}"
LLM_EXTRA_ARGS=(--max-num-seqs "${LLM_MAX_NUM_SEQS:-32}")
