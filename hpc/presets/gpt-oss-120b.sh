# gpt-oss-120b (OpenAI, Aug 2025): 117B-total / 5.1B-active mixture-of-experts shipped with MXFP4
# expert weights (~63 GB), which is why OpenAI advertises it as a single-80-GB-GPU model. Apache
# 2.0. vLLM 0.29 runs it natively (GptOssForCausalLM); on Hopper the MXFP4 experts go through the
# Marlin kernel with no extra packages. Only the top-level safetensors are needed -- the repo's
# original/ and metal/ folders are other formats and are excluded from the download.
: "${LLM_HF_REPO:=openai/gpt-oss-120b}"
LLM_HF_EXCLUDE=("original/*" "metal/*")
: "${LLM_MODEL_DIR:=${HOME}/models/gpt-oss-120b}"
: "${LLM_SERVED_NAME:=gpt-oss-120b}"
# Harmony response format: vLLM's own `openai` tool parser and `openai_gptoss` reasoning parser
# (the reasoning parser keeps the analysis channel out of `content`).
: "${LLM_TOOL_PARSER:=openai}"
: "${LLM_REASONING_PARSER:=openai_gptoss}"
: "${LLM_TP_SIZE:=1}"
: "${LLM_MAX_LEN:=131072}"
# gpt-oss always reasons before answering; there is no non-thinking mode, only a reasoning_effort
# (low/medium/high, default medium, which the pipeline does not touch). So this is the one preset
# in the comparison whose model thinks before every tool call, and its provenance records
# thinking_disabled=false. Sending enable_thinking=false would be ignored, so it is not sent.
: "${LLM_DISABLE_THINKING:=false}"
LLM_EXTRA_ARGS=()
