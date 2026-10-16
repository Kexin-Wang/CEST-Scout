# Qwen3-32B-FP8: the original CEST_Scout model. Dense 32.8B, FP8, ~33 GB of weights.
# Native context is only 32768, so the production setup runs TWO instances of this preset with
# YaRN rope scaling (LLM_ROPE_SCALING) -- one at 65536 for literature+chemist, one at 131072 for
# physicist+experimentalist; see hpc/README.md.
: "${LLM_HF_REPO:=Qwen/Qwen3-32B-FP8}"
: "${LLM_MODEL_DIR:=${HOME}/models/Qwen3-32B-FP8}"
: "${LLM_SERVED_NAME:=qwen3-32b}"
# The officially documented parser for Qwen3's <tool_call>{json}</tool_call> format.
: "${LLM_TOOL_PARSER:=hermes}"
: "${LLM_REASONING_PARSER:=}"
: "${LLM_TP_SIZE:=1}"
: "${LLM_MAX_LEN:=32768}"
# Thinking is on by default and interleaves <think> text with tool calls; CEST_Scout disables it
# per request via chat_template_kwargs when LLM_DISABLE_THINKING=true.
: "${LLM_DISABLE_THINKING:=true}"
LLM_EXTRA_ARGS=()
