# Serving local models for CEST Scout on the HPC

Everything in this directory runs open-weight models on your own GPU allocation. One preset per model in
`presets/` pins down what must not be guessed; the scripts around them start a server inside an
existing SLURM allocation, point the pipeline at it, and tag the outputs with the model so runs
of different models can be compared with `python -m cest_scout.compare`.

## Presets

| Preset | Model | Params (active) | Weights | GPUs | Tool-call parser | Thinking |
|---|---|---|---|---|---|---|
| `qwen3-32b-fp8` | Qwen/Qwen3-32B-FP8 | 32.8B dense | ~33 GB | 1 (x2 instances, see below) | `hermes` | disabled per request |
| `qwen3.8-27b-fp8` | Qwen/Qwen3.8-27B-FP8 | 27.8B dense | ~28 GB | 1 | `qwen3_coder` + `qwen3` reasoning | disabled per request |
| `qwen3.6-35b-a3b-fp8` | Qwen/Qwen3.6-35B-A3B-FP8 | 35B MoE (3B) | ~36 GB | 1 | `qwen3_coder` + `qwen3` reasoning | disabled per request |
| `kimi-linear-48b-a3b` | moonshotai/Kimi-Linear-48B-A3B-Instruct | 48B MoE (3B) | ~98 GB bf16 | 2 (TP=2) | `kimi_k2` | none (instruct model) |
| `glm-4.7-flash` | zai-org/GLM-4.7-Flash | 30B MoE (3B) | ~63 GB bf16 | 1 | `glm47` + `glm45` reasoning | disabled per request |
| `gpt-oss-120b` | openai/gpt-oss-120b | 117B MoE (5.1B) | ~63 GB MXFP4 | 1 | `openai` + `openai_gptoss` reasoning | always on (harmony; no off switch) |
| `gemma-4-31b-it` | google/gemma-4-31B-it | 31B dense | ~63 GB bf16 | 2 (TP=2, for the KV cache) | `gemma4` + `gemma4` reasoning | off by default |
| `nemotron-3-nano-30b-a3b-fp8` | nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8 | 30B hybrid Mamba-MoE (~3.5B) | ~33 GB FP8 | 1 | `qwen3_coder` + `nemotron_v3` reasoning | disabled per request |
| `deepseek-v4-flash` | deepseek-ai/DeepSeek-V4-Flash | MoE, 256 experts / 6 active, 43 layers, 1M window | ~160 GB block-FP8 | 2 H200 (TP=2; Hopper FP8 format) | `deepseek_v4` + `deepseek_v4` reasoning | see preset |

Please select the models that could fit in your HPC settings. For example, for a 2xH100 (160 GB) allocation:

- **Kimi-K2 / K2.5 / K2.6 / K3** are 1-2.8 trillion-parameter models; even 4-bit they need far
  more than 160 GB. Kimi-Linear-48B-A3B is the only Kimi model that fits.
- **Qwen3.8-Flash-Next** is 180 GB of FP8 weights; does not fit. **Qwen3.5-122B-A10B-FP8**
  (~125 GB) would fit with TP=2 but leaves little headroom and was not run.
- Kimi-Linear-48B-A3B was run (its preset and reports remain) but is **excluded from the
  comparison pages**: across four runs its physicist and experimentalist stages made zero
  simulate/search calls in most runs, its chemist proposed no exchangeable protons for glycogen,
  and 50-80% of its citations failed PubMed verification -- not comparable on equal terms.

## One-time setup (login node -- it has internet, GPU nodes may not)

```bash
bash ~/hpc-llm/setup_env.sh              # conda env `llm-serve` with vLLM + huggingface_hub
bash hpc/download_model.sh qwen3.8-27b-fp8
bash hpc/download_model.sh qwen3.6-35b-a3b-fp8
bash hpc/download_model.sh kimi-linear-48b-a3b
```

Weights go to `~/models/<name>`. 
