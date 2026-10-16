# NVIDIA Nemotron 3 Nano 30B-A3B (Dec 2025), FP8 checkpoint: hybrid Mamba-2 / attention
# mixture-of-experts, 30B total / ~3.5B active, FP8 weights AND an FP8 KV cache (attention layers
# and the Mamba layers feeding them kept in bf16), 32.7 GB. Built for agentic tool use, 262144
# native context. NVIDIA Open Model License.
: "${LLM_HF_REPO:=nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8}"
: "${LLM_MODEL_DIR:=${HOME}/models/Nemotron-3-Nano-30B-A3B-FP8}"
: "${LLM_SERVED_NAME:=nemotron-3-nano-30b-a3b}"
# Model card: --tool-call-parser qwen3_coder (same XML tool format as Qwen3.5+). The card ships a
# nano_v3 reasoning-parser plugin for older vLLM; vLLM 0.29 has it built in as nemotron_v3.
: "${LLM_TOOL_PARSER:=qwen3_coder}"
: "${LLM_REASONING_PARSER:=nemotron_v3}"
: "${LLM_TP_SIZE:=1}"
: "${LLM_MAX_LEN:=131072}"
# Reasoning is on by default; the chat template honours enable_thinking=false.
: "${LLM_DISABLE_THINKING:=true}"
# --trust-remote-code: the repo's config carries an auto_map to its own modelling/tokenizer code
# and the model card's own vllm serve command passes it. --kv-cache-dtype fp8: the checkpoint was
# quantised with an FP8 KV cache. --max-num-seqs 8: the model card's value; Mamba state is per
# sequence (see qwen3.8-27b-fp8.sh) and the agent loop is sequential.
LLM_EXTRA_ARGS=(--trust-remote-code --kv-cache-dtype fp8 --max-num-seqs "${LLM_MAX_NUM_SEQS:-8}")
