# GLM-4.7-Flash (Z.ai, Jan 2026): 30B-total / 3B-active mixture-of-experts with DeepSeek-style
# multi-head latent attention (kv_lora_rank 512 + 64 rope dims), so its KV cache is a few tens of
# KB per token and the 62.5 GB of bf16 weights plus a 131072-token window fit on ONE 80 GB H100.
# No FP8 checkpoint is published. MIT license. vLLM 0.29 implements the architecture natively
# (Glm4MoeLiteForCausalLM). Generation defaults ship as temperature 1.0.
: "${LLM_HF_REPO:=zai-org/GLM-4.7-Flash}"
: "${LLM_MODEL_DIR:=${HOME}/models/GLM-4.7-Flash}"
: "${LLM_SERVED_NAME:=glm-4.7-flash}"
# The model card's vLLM command names these two parsers. GLM-4.7 emits
# <tool_call>name<arg_key>k</arg_key><arg_value>v</arg_value></tool_call>, which neither hermes
# (Qwen3) nor qwen3_coder (Qwen3.5+) parse.
: "${LLM_TOOL_PARSER:=glm47}"
: "${LLM_REASONING_PARSER:=glm45}"
: "${LLM_TP_SIZE:=1}"
: "${LLM_MAX_LEN:=131072}"
# Thinking is on by default; the chat template honours enable_thinking=false (it then opens the
# assistant turn with a closed </think>), which is what LLM_DISABLE_THINKING=true sends, so this
# model runs in the same non-thinking mode as the Qwen presets.
: "${LLM_DISABLE_THINKING:=true}"
# The model card's --speculative-config (MTP draft layer) is left off: it costs GPU memory that
# the KV cache needs on a single GPU, and the agent loop is strictly sequential anyway.
LLM_EXTRA_ARGS=(--max-num-seqs "${LLM_MAX_NUM_SEQS:-32}")
