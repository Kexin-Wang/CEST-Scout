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

Why these and not others, on a 2xH100 (160 GB) allocation with vLLM 0.29:

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

Weights go to `~/models/<name>` (NFS-shared, so compute nodes read them directly). The login node
caps each process at ~1 GB of RAM: `download_model.sh` limits parallel workers for that reason,
and running several downloads at once gets them OOM-killed (exit 137).
A compute node that can reach huggingface.co (the shared H100 boxes can) has no such cap:
`HF_MAX_WORKERS=8 bash hpc/download_model.sh <preset>` there runs at the link's full speed. A
preset may set `LLM_HF_EXCLUDE=(pattern ...)` to skip repo folders that are other weight formats
(gpt-oss's `original/` and `metal/`), so only the safetensors vLLM loads are fetched.

## Running one model

```bash
# 1. Get GPUs. Ask for H100s explicitly -- the generic gpu partition mixes V100/A100/L40S/H100/H200.
salloc -p gpu --gres=gpu:h100:2 --cpus-per-task=12 --mem=50G --time=08:00:00
# note the job id (squeue -u $USER); the rest runs from the LOGIN node

# 2. Start a server inside that allocation and wait for it (prints the endpoint URL)
bash hpc/start_server.sh qwen3.8-27b-fp8 <jobid> 0 8000      # GPU 0, port 8000
bash hpc/start_server.sh kimi-linear-48b-a3b <jobid> 0,1 8000   # TP=2 needs both GPUs

# 3. Run the pipeline with all four agents on that server; outputs get the preset in their name
bash hpc/run_with_server.sh qwen3.8-27b-fp8 http://<node>:8000/v1 liver
#   -> outputs/liver_qwen3_8_27b_fp8_<timestamp>.{json,md,html}

# 4. Stop it before starting a different model on the same GPUs
bash hpc/stop_server.sh qwen3.8-27b-fp8 8000
```

Two single-GPU presets can run at the same time on GPU 0 and GPU 1 (ports 8000/8001), each with
its own pipeline run. Server logs land in `hpc/logs/` (git-ignored: vLLM echoes its arguments,
including the API key, at startup).

`run_with_server.sh` exports `OPENAI_BASE_URL`, `PHYSICIST_OPENAI_BASE_URL`, the four
`OPENAI_MODEL_*` names and the thinking flags for that one run; python-dotenv never overrides
exported variables, so `.env` keeps describing the default setup. Every report records, per
agent stage, the model, endpoint, minutes, tokens, tool calls, retries and whether the
completeness check passed (see "Run provenance" at the end of a report).

### On a shared GPU node you are logged into directly (no SLURM step)

Some of this cluster's H100 boxes are reached by plain `ssh` and shared between users; `squeue`
and `srun` are not even on the PATH there, and `CUDA_VISIBLE_DEVICES` lists all eight GPUs.
`hpc/start_server_local.sh` replaces `start_server.sh` in that setting: it takes nvidia-smi GPU
indices, translates them to UUIDs, refuses a GPU that already has more than 1 GB in use (someone
else's job), and starts `serve.sh` in its own process group so `stop_server.sh` can end vLLM's
engine subprocesses with the API server:

```bash
nvidia-smi --query-gpu=index,memory.used --format=csv        # pick two idle GPUs
bash hpc/start_server_local.sh glm-4.7-flash 1 8000          # one GPU
bash hpc/start_server_local.sh gemma-4-31b-it 1,2 8000       # TP=2 preset: two GPUs
python hpc/smoke_test.py http://$(hostname):8000/v1 glm-4.7-flash --disable-thinking
bash hpc/run_with_server.sh glm-4.7-flash http://$(hostname):8000/v1 liver
bash hpc/stop_server.sh glm-4.7-flash 8000
```

`hpc/smoke_test.py` is the two-minute check to run before spending a 10-30 minute pipeline run
on a new preset: it asks for a tool call in a situation that plainly needs one, feeds a tool
result back, and requests a `response_format={"type": "json_object"}` answer -- exactly the
three things the agents depend on. A model served with the wrong tool-call parser fails the
first check (the call comes back as text) instead of "completing" a run with zero tool calls.

Inside a SLURM allocation the same batch runs on the allocated node with
`REPLICATES=2 bash hpc/run_batch_in_allocation.sh <jobid> <body part> <tag>` (it wraps
`compare_batch_local.sh` in `srun --overlap`; the job's two GPUs are indices 0 and 1 there).

`hpc/compare_batch_local.sh <gpuA> <gpuB> <body part> <tag>` strings all of this together
unattended for a whole comparison: it serves presets in pairs (one per GPU) or alone (TP=2, or
the Qwen3-32B two-server layout below), waits for any preset whose weights are still
downloading, smoke-tests every server, skips a preset that fails, runs the pipeline for each,
and ends with `cest_scout.compare` over every run of the batch
(`outputs/compare_<body>_<tag>.{md,html}`). Its `PHASES` list is the plan; per-preset outcomes
are in `hpc/logs/batch_<tag>.log`. `REPLICATES=2` (or more) runs the pipeline that many times
per served model, back to back on the same server, so the comparison can report each model's
run-to-run spread; `PORT_A`/`PORT_B` choose the ports when 8000/8001 are taken by someone else
on a shared node:

```bash
PORT_A=8002 PORT_B=8003 REPLICATES=2 nohup bash hpc/compare_batch_local.sh 1 7 pancreas panc20260924 \
    > hpc/logs/batch_panc20260924.out 2>&1 &
```

