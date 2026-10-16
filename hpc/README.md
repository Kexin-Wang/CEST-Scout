# Serving local models for CEST_Scout on the HPC

Everything in this directory runs open-weight models on your own GPU allocation behind an
OpenAI-compatible endpoint, so the four agents never touch a paid API. One preset per model in
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
- **Added 2026-09-23** for the second comparison round (2xH100, Kimi excluded): four open-weight
  models that fit the allocation, have a tool-call parser vLLM 0.29 actually implements, and
  are marketed for agentic tool use. **GLM-4.7-Flash** (30B-A3B, MLA attention, so its KV cache
  is small enough for a 131K window on one GPU next to 63 GB of bf16 weights); **gpt-oss-120b**
  (OpenAI's 117B-A5B MXFP4 checkpoint, designed for one 80 GB GPU -- the only model here that
  cannot switch reasoning off, so it thinks before every tool call while the others run in
  non-thinking mode; the comparison page flags this via `thinking_disabled`); **Gemma 4 31B-it**
  (dense; its 16-head x 256-dim full-attention layers make the KV cache ~164 KB/token, so the
  131K window needs TP=2 -- both GPUs); and **Nemotron 3 Nano 30B-A3B FP8** (hybrid Mamba-2/MoE,
  FP8 weights and KV cache). Checked and not run: GLM-4.5-Air-FP8 (113 GB, needs both GPUs and
  is superseded by GLM-4.7-Flash), Seed-OSS-36B (72 GB bf16 leaves no KV room on one GPU),
  Mistral-Small-3.2-24B (older; weaker agentic record), Llama 4 Scout (109B; FP8 needs both
  GPUs for a mid-2025 model), MiniMax-M2 (230B; does not fit).
- Every preset has a tool-call parser vLLM actually implements for that model's chat template.
  This is the hard requirement: the agents rely entirely on OpenAI-style `tools=` calls, and a
  model served without a matching parser silently stops using PubMed, PubChem, the frequency
  calculator and the Bloch-McConnell bridge, and hallucinates the structured answers instead.
  Qwen3.5+ emits an XML tool format (`<tool_call><function=...>`) that the Qwen3-era `hermes`
  parser does not parse -- the model cards name `qwen3_coder`.
- The Qwen3.5+ and Kimi-Linear presets pass `--max-num-seqs 32`: their linear-attention layers
  pin one state block per in-flight sequence, and vLLM's default of 1024 does not fit next to
  the weights ("max_num_seqs (1024) exceeds available Mamba cache blocks"). The agent loop is
  strictly sequential, so this costs nothing.
- All models run in **non-thinking mode** (the pipeline sends
  `chat_template_kwargs={"enable_thinking": false}` when `LLM_DISABLE_THINKING=true`), so the
  comparison is like-for-like with the original Qwen3-32B setup, whose thinking output corrupted
  tool-call parsing. Kimi-Linear-Instruct has no thinking mode.
- Sampling uses each model's shipped `generation_config.json` defaults (vLLM's default), i.e. the
  model as its authors ship it; the pipeline sets no temperature.

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

### The Qwen3-32B baseline's two-server layout

Qwen3-32B's native window is 32768 tokens, too short for the physicist and experimentalist
loops, so the original setup runs two YaRN-scaled instances of the same preset:

```bash
LLM_MAX_LEN=65536  LLM_ROPE_SCALING='{"rope_type":"yarn","factor":2.0,"original_max_position_embeddings":32768}' \
  bash hpc/start_server.sh qwen3-32b-fp8 <jobid> 0 8000     # literature + chemist
LLM_MAX_LEN=131072 LLM_ROPE_SCALING='{"rope_type":"yarn","factor":4.0,"original_max_position_embeddings":32768}' \
  bash hpc/start_server.sh qwen3-32b-fp8 <jobid> 1 8001     # physicist + experimentalist
```

and `.env` points `OPENAI_BASE_URL` at :8000 and `PHYSICIST_OPENAI_BASE_URL` at :8001. (The
`--export` in `start_server.sh` passes `LLM_*` variables through to the compute node.) The
Qwen3.5+/Kimi presets have 256K+ native windows and need none of this.

## Re-simulating old runs on a newer reference table

The exchange-rate table (`cest_scout/data/exchange_reference.json`) grows as more abstracts are
verified. To compare runs made at different table versions on equal footing, recompute only the
deterministic stage from each run's saved agent outputs:

```bash
python -m cest_scout.resimulate outputs/liver_qwen3_8_27b_fp8_<stamp>.json outputs/liver_qwen3_6_35b_a3b_fp8_<stamp>.json
#   -> outputs/liver_qwen3_8_27b_fp8_resim_<stamp>.{json,md,html} ...
```

The agents' candidates, classes, concentrations and critiques are unchanged; k_ex/shift, sweeps,
detectability and organ predictions are recomputed, and the report says it was re-simulated.

## Comparing models

```bash
python -m cest_scout.compare outputs/liver_qwen3_32b_fp8_*.json outputs/liver_qwen3_8_27b_fp8_*.json \
    outputs/liver_qwen3_6_35b_a3b_fp8_*.json outputs/liver_kimi_linear_48b_a3b_*.json \
    --out outputs/compare_liver
```

writes `outputs/compare_liver.{md,html}`. The committed `outputs/compare_liver.html` is the
2026-09-22 liver comparison of the four presets (Qwen3.6 run twice) on identical pipeline code,
and `outputs/compare_liver_postfix.html` re-runs Qwen3-32B and Kimi-Linear on the code after the
checks that comparison motivated were added (see the main README's Architecture section). The
tables cover cost and loop reliability, how well each model's
candidates are sourced (citations dropped by PubMed verification, quotes not found in the cited
abstract, eligibility flags), structure/coverage checks, a per-metabolite overlap table with each
model's detectability verdict, best contrast (with its range over the concentration x k_ex
uncertainty corners and the robust/fragile grade) and the concentration it used, and pairwise
agreement. With replicate runs it adds a per-model summary that pools them -- citation drop rate,
verified quotes, wrong structures, completeness failures, and the within-model replicate
candidate Jaccard and detectability agreement that between-model differences must be read
against -- and a cross-run consensus table saying how many models and runs converge on each
candidate and on its detectability. Nothing in it is re-judged by an LLM; the Bloch-McConnell
physics is identical across runs, so a contrast difference is a difference in the inputs a model
researched. Runs made on uncommitted code are named in the caveats (their provenance carries
`+dirty`).

### 2026-09-23: seven models on liver, 2xH100, Kimi-Linear excluded

`outputs/compare_liver_batch20260923.html` compares one liver run each of Qwen3-32B, Qwen3.8-27B,
Qwen3.6-35B-A3B, GLM-4.7-Flash, gpt-oss-120b, Gemma 4 31B-it and Nemotron 3 Nano 30B-A3B-FP8,
all made by `hpc/compare_batch_local.sh` on GPUs 4 and 7 of a shared node (the two H100s of SLURM
job 6038525), every server smoke-tested first, all in non-thinking mode except gpt-oss (which has
none). In the terms the page measures, one run per model, so single-candidate differences are
noise:

- **gpt-oss-120b and Qwen3-32B agree most** (Jaccard 0.75, 6/6 detectability verdicts); both
  land on glycogen and the mobile protein amide pool as detectable. gpt-oss used tools in every
  stage (41 calls) and finished in 8 min on one GPU; its one tool error was a harmony channel
  token leaking into a function name (`search_pubmed</channel/>commentary`), fed back and
  corrected.
- **Gemma 4 31B-it** was the only model with zero citations dropped by PubMed verification and
  the cheapest run (14 LLM calls, 208K prompt tokens, 7 min at TP=2) with tools in every stage
  and no completeness failure -- but it quoted no concentration verbatim (all six estimated) and
  all four structures it proposed were wrong and replaced from PubChem.
- **GLM-4.7-Flash** (non-thinking) was the weakest: 27 of 38 citations fabricated, three
  candidates at implausible concentrations (glycogen 8 mM, protein amide 1.5 mM, so nothing
  detectable), five JSON repairs, literature and experimentalist completeness checks failed, and
  a scan protocol whose pulse train does not add up to its saturation time.
- **Nemotron 3 Nano FP8** was the fastest (3 min, tools in every stage) but its chemist proposed
  no exchangeable protons for two of four candidates, two concentration quotes were not in the
  cited abstracts, and literature and chemist completeness checks failed. Its server needs ~15
  min to start (Mamba-2 Triton kernel warm-up) -- longer than its run.
- **Qwen3.6-35B-A3B** proposed the most candidates (9) but 18 of 31 citations were dropped;
  **Qwen3.8-27B** proposed 5 with 3 verbatim quotes and 3 dropped citations, at 17 min the
  slowest single-GPU run.

The two Qwen3.x runs were re-made at a84e995 after commit 34a121f changed the experimentalist
agent (it now designs the verification scan for the tissue at hand instead of a fixed abdominal
set) so that all seven runs share that agent; the other five are at 8c7bdc0, and the only code
difference between the two is the tissue T1/T2/MT lookup table (1d804d2, a84e995), which touches nothing
before the ROI-prediction table. `outputs/compare_liver_batch20260923_resim.html` re-simulates
those five on the current tables (`cest_scout.resimulate`) so that table is on equal footing too.

### 2026-09-27: seven models on pancreas, two replicates each, 2xH200

`outputs/compare_pancreas_panc20260927.html` compares 14 runs (two per model) made by
`hpc/run_batch_in_allocation.sh` inside SLURM job 6050416 on esplhpc-cp097 (two H200s), on code
8b03900 -- the first comparison with the rNOE classes, the three-way verdict and the
repeating-unit structures. The 8xH100 nodes (cp087-cp089) were unusable that day: their NVIDIA
fabric manager had been down since 2026-09-25 and every CUDA context failed with error 802, so
the watcher fell through to the H200 request. Headlines, read against each model's own
replicate spread:

- **gpt-oss-120b** is the most reproducible (replicate Jaccard 0.83, 80 % verdict agreement),
  7 min per run, 26 % of citations dropped, and the only model whose detectable candidates were
  all robust in both runs.
- **Gemma 4 31B-it** had no completeness failures and 100 % verdict agreement on shared
  candidates, but its two runs shared few candidates (Jaccard 0.29), it quoted no concentration
  verbatim and 4 of 7 structures were wrong.
- **Qwen3.8-27B** proposed the most candidates (7) with the lowest citation drop rate (11 %),
  but took 17-113 min and got 5 of 7 structures wrong.
- **Qwen3.6-35B-A3B** (70 % dropped), **Qwen3-32B** (57 %), **GLM-4.7-Flash** (47 %, no
  candidate shared between its own two runs) and **Nemotron 3 Nano** (82 %, five completeness
  failures, every structure wrong) fabricated most of their citations.
- Consensus: the mobile-protein amide pool was proposed by all seven models and detectable and
  robust in 10 of 11 runs; glycogen by all seven, detectable in 7 of 11 but robust in only 2
  (the concentrations used span 0.1-150 mM); glucose detectable in all 4 runs proposing it.
  Amine-only candidates (glutamate, GABA, lactate, ...) are reported as unknown, not as absent.
  27 rNOE groups were proposed across the runs and simulated at the table's upfield offsets.

### 2026-09-28: liver, seven models x two replicates, twice, on 4xL40S

With the 8xH100 nodes still unusable (fabric manager failed since 2026-09-25) and no H200 free,
the liver comparison ran on four L40S (Ada, 48 GB) with every model served as a tensor-parallel
pair (`LLM_TP_SIZE=2`, GPUs 0,1 and 2,3) by `hpc/run_batch_in_allocation.sh` inside SLURM jobs:
batch 1 on code e9f0316 (`outputs/compare_liver_l40s_20260928.html`, 13 runs), batch 2 on the
field-scaling code 7193dc0 (`outputs/compare_liver_l40s_fs_20260928.html`, 13 runs; GLM-4.7-Flash
completed one run in each batch, failing the chemist's structured output in the other).
`outputs/compare_liver_l40s_all26_20260928.html` compares all 26 runs after re-simulating batch
1 on the newer physics (`cest_scout.resimulate`), i.e. four replicates per model with identical
tables. Headlines: glycogen was detectable in 26 of 26 runs (robust in 19) and the mobile-protein
amide pool in 20 of 20 runs proposing it (19 robust); glycosaminoglycans were proposed in 14 runs
but detectable in 3; amine-only candidates read "unknown". Gemma 4 dropped no citations in four
runs (0 of 56) with 96 % replicate verdict agreement; Nemotron dropped 68 % with seven
completeness failures; the dense Qwen models took 45-154 min per run on the L40S pairs against
17-113 min on H200. Between-batch shifts for the same model (gpt-oss replicate overlap 0.75 vs
0.17, Qwen3.8 citation drops 25 % vs 5 %) are the run-to-run noise that single-batch rankings
must be read against.

### 2026-09-29/30: Qwen3.8-27B vs Qwen3.6-35B-A3B on liver, L40S vs H100

The same two models, three liver replicates each, on the review-fixes code (b06efa2/c9b8fe3,
identical physics): first on four L40S as tensor-parallel pairs
(`outputs/compare_liver_l40s_qwen_20260929.html`), then -- once the 8xH100 nodes' fabric manager
was restarted on 2026-09-30 -- on two H100 of esplhpc-cp088, one GPU per model
(`outputs/compare_liver_h100_qwen_20260930.html`). `outputs/compare_liver_qwen_l40s_vs_h100_20260930.html`
puts all twelve runs side by side, grouped by hardware (`cest_scout.compare --group-by-label`).
The physics and verdicts do not depend on the GPU: glycogen was detectable in 12 of 12 runs and
the protein amide pool in 11 of 11 runs proposing it, on both machines; glutathione, glutamine and
ATP stayed below threshold everywhere. What the hardware changes is time: Qwen3.8 took 14-18 min
per run on an H100 against 29-128 min on an L40S pair, Qwen3.6 4-11 min against 51-109 min. The
between-hardware differences in citation drop rate (Qwen3.8 4 % vs 7 %, Qwen3.6 68 % vs 72 %) and
replicate overlap (0.54 vs 0.36-0.43) are within the run-to-run spread of the same model on one
machine. Qwen3.8 is the more reliable researcher of the two (4-7 % of citations fabricated against
68-72 %), Qwen3.6 the faster one.

### 2026-09-30: DeepSeek-V4-Flash vs Qwen3.8-27B vs Qwen3.6-35B-A3B on liver, 2x/4xH100

DeepSeek-V4-Flash (block-FP8, TP=4 on four H100 of esplhpc-cp088, FP8 KV cache, non-thinking
mode) ran liver three times (9-15 min per run after a 12 min model load); the comparison with the
same morning's H100 Qwen runs is `outputs/compare_liver_deepseek_vs_qwen_h100_20260930.html`.
DeepSeek proposed the most candidates (6-10 per run), got only 1 of 20 structures wrong (Qwen: 8
of 11 and 9 of 15), dropped 10 % of citations (Qwen3.8 7 %, Qwen3.6 72 %), passed every
completeness check, and agreed with itself on every shared verdict across replicates (Qwen3.8
78 %, Qwen3.6 67 %). Its extra candidates are mostly amine-only small metabolites that the
reference table cannot rate, so it also has the most "unknown" verdicts (3-5 per run). All three
models converge on the same physics: glycogen detectable in 9 of 9 runs and the mobile-protein
amide pool in 8 of 8 runs proposing it; glutathione, glutamine, creatine and lactate below
threshold everywhere.

### 2026-09-30: DeepSeek-V4-Flash vs Qwen3.8-27B vs Qwen3.6-35B-A3B on brain, pancreas and disc

One 4xH100 allocation (esplhpc-cp088, `hpc/logs/three_models.sh`): DeepSeek served TP=4 ran each
tissue three times, then Qwen3.8 and Qwen3.6 on one GPU each ran the same 18 runs in parallel;
27 reports in 4.5 h. One comparison page per tissue:
`outputs/compare_brain_deepseek_qwen_20260930.html`, `outputs/compare_pancreas_deepseek_qwen_20260930.html`,
`outputs/compare_intervertebral_disc_deepseek_qwen_20260930.html`.
- Physics consensus holds across models: the mobile-protein amide pool is detectable and robust
  in every run proposing it (brain 9/9, pancreas 7/7, disc 9/9); in brain myo-inositol (9/9) and
  creatine (7/8) are detectable too; in the disc glycosaminoglycans are detectable and robust in
  8/8 runs (median 11.9 %); glycogen is below threshold in brain and pancreas (absent or < 25 mM).
  Amine-only candidates (GABA, taurine, glutamate, glutamine) stay "unknown".
- Model behaviour: DeepSeek-V4-Flash and Qwen3.8 fabricate few citations (brain 4 % each; disc
  9 % vs 6 %); Qwen3.6 fabricates 51-87 %. DeepSeek gets most structures right (3-6 wrong of
  10-24) where Qwen3.8 gets most wrong (6-15 of 9-20); DeepSeek's verdicts agree across replicates
  in 75-100 % of shared candidates. DeepSeek is also the only one that failed completeness checks in
  every tissue (2-3 stage-runs each), mostly in the literature stage with its long candidate lists.
  Per run: DeepSeek 9-15 min, Qwen3.8 15-23 min, Qwen3.6 4-18 min.

### 2026-10-06: disc again after the glucose / phosphocreatine reference fixes, 2xH200

One 2xH200 allocation (esplhpc-cp097, `hpc/logs/disc_20261006_2gpu.sh`; every H100 on the cluster
was held by week-long jobs): DeepSeek at its preset TP=2 ran the disc three times, then Qwen3.8 and
Qwen3.6 on one GPU each -- nine reports in 1 h 55 min -- plus one fresh Qwen3.8 replicate on 1xH200
(see caveats). Comparison: `outputs/compare_intervertebral_disc_deepseek_qwen_20261006.html`.
- Why rerun: glucose OH had been simulated at 2 ppm (two 3 T brain papers stating the offset they
  saturated at outranked seven resonances of 0.9-1.6 ppm) with a 3-O-methyl-glucose rate measured at
  pH 6.1 and 4 C, and phosphocreatine inherited creatine's 2 ppm. Now the offset is the median of the
  metabolite's own values across settings, analogues/polymers are their own table entries, the four
  glucose OH sites of Zaiss 2019 (PMID 31313865; 0.66/1.28/2.08/2.88 ppm, 2900-14000 s^-1 at pH 7.2,
  37 C) enter from the PMC full text and are labelled so, and PCr sits at 2.6 ppm. Glucose OH is now
  1.6 ppm / 5850 s^-1; the Simulation-inputs table prints the chemist's own ppm beside it as "not used".
- Physics consensus: glycosaminoglycans are detectable in 8/9 (median best contrast 5.4 % at 7 T; the one miss put GAG at 2 mM where the others used 25-100 mM of disaccharide units); the mobile-protein amide pool is detectable and robust in 9/9; glucose
  (7/9 runs, 1-1.5 mM) stays below threshold at every field, as does lactate in 8/9 (the exception used
  20 mM instead of 2-5 mM); glycogen follows the literature agent's number -- detectable in the three
  runs that put it at 30-80 mM glucosyl units, not in the four at 0.3-3 mM. Amine-only and ionic candidates (urea, Na+) stay "unknown".
- Model behaviour: citations dropped per run DeepSeek 0-4, Qwen3.8 1-9, Qwen3.6 10-28; wrong
  structures DeepSeek 2 of 6, Qwen3.6 1 of 7, Qwen3.8 7 of 12; completeness checks failed in the
  literature stage for DeepSeek 3/3, Qwen3.6 1/3, Qwen3.8 1/3. Qwen3.6 #3's chemist quoted
  TMS-scale shifts (amide +7.0, hydroxyl +5.0 ppm): shown as "not used", simulation unaffected.
  Per run: DeepSeek 11-16 min, Qwen3.6 7-14 min, Qwen3.8 15-19 min.
- Record caveats: seven runs are stamped `4583f27+dirty` because README.md was edited during the
  batch -- no code or data differed; the stamp is now scoped to `cest_scout/`. Qwen3.8's first
  replicate aliased glycogen "polyglucose", which pooled glucose's hydroxyl entries into glycogen
  (1.28 ppm / 6500 s^-1); fixed in fa2974c (a name that matches the table is never widened by its
  aliases), and that run is superseded by a fresh replicate (kept under
  `outputs/hopper_disc_20261006/superseded/`, excluded from the comparison).

### 2026-10-07: DeepSeek-V4-Flash vs Qwen3.8-27B vs Qwen3.6-35B-A3B on myocardium, 2xH200, grounded water parameters

One 2xH200 allocation (esplhpc-cp097, `hpc/logs/myocardium_20261007_2gpu.sh`): DeepSeek at TP=2 ran
the heart three times, then Qwen3.8 and Qwen3.6 one GPU each -- nine reports in 2 h 03 min.
The tissue table had no myocardium entry, so the batch simulated on the labelled generic defaults
(T1 1.30 s, T2 80 ms, MT 0.05). Healthy human LV myocardium was then grounded from five abstracts
(T1 1005 ms at 1.5 T / 1174 ms at 3 T, T2 56 / 52 ms, MT pool-size ratio 4.3 %; scaled to 0.5/5/7 T
with the tissue's own dispersion and labelled estimated) and the nine runs were re-simulated on it
with `cest_scout.resimulate`; the comparison is built from those `_resim` reports:
`outputs/compare_myocardium_deepseek_qwen_20261007.html` (the agents' review text predates the
re-simulation, and each report says so).
- Physics consensus (grounded water): the mobile-protein amide pool is detectable in 8/8 runs proposing it (median best contrast 10.1 %; 11.1 % on the generic 80 ms water T2), phosphocreatine in 5/6 (median 1.2 %) and creatine in 4/6 (median 1.5 %; the 'unknown' in each is Qwen3.8 #2's chemist error below, the one creatine 'no' is a 2.8 mM run); glycogen in 7/9 (median 0.76 %, detectable only where the agent put it at 25-75 mM glucosyl units); ATP (0/4), glutathione (0/3), carnosine (0/2), lactate (0/2) and 1-3 mM GAG (1/4, the yes at 27 mM) stay below threshold; taurine (8/8) and glutamate (4/4) are 'unknown'.
- Model behaviour: candidates per run DeepSeek 7-11, Qwen3.8 5-7, Qwen3.6 5-7; citations dropped
  DeepSeek 1-15, Qwen3.8 5-12, Qwen3.6 7-18; wrong structures DeepSeek 4 of 17, Qwen3.8 8 of 11,
  Qwen3.6 5 of 13; literature completeness check failed DeepSeek 2/3, Qwen3.6 2/3, Qwen3.8 1/3.
  Qwen3.8 #2's chemist judged the creatine and phosphocreatine guanidinium NH non-exchangeable, so
  both were left unsimulated ("unknown"): a wrong input reported as such, not simulated wrongly.
  Qwen3.6 #3 put the mobile-protein amide pool at 1600 mM (others 70-250 mM) and
  Qwen3.8 #3 gave creatine a spurious amide NH group. Taurine (amine) and glutamate stay "unknown"
  in every run: no abstract-verified amine exchange rate. Per run: DeepSeek 14-18 min, Qwen3.6
  6-28 min, Qwen3.8 13-36 min.
- Known gap: side-chain/primary amides (glutamine, carnosine's peptide amide) are classed "amide"
  and simulated with the backbone-amide reference (3.5 ppm, ~50-65 s^-1); a primary amide needs its
  own class and reference entries.

### 2026-10-08: vanilla Qwen3.8-27B / Qwen3.6-35B-A3B vs the pipeline on myocardium, disc and pancreas

The no-tools baseline (`cest_scout.vanilla`): the same served model asked the tissue question once,
JSON answer, thinking off as in the pipeline, no PubMed, PubChem, reference table or simulation;
three answers per model per tissue on two L40S (job 6128970, `hpc/logs/vanilla_l40s_20261008.sh`).
Scored with `cest_scout.compare_vanilla` against the pipeline's nine runs per tissue (myocardium and
disc as above; pancreas = the 2026-09-30 runs re-simulated on the current table, which took 3 min on
a 48-core `defq` node with `OMP_NUM_THREADS=4` after failing inside the GPU allocation):
`outputs/compare_{myocardium,intervertebral_disc,pancreas}_vanilla_20261008.html`. DeepSeek-V4-Flash
was added on 2026-10-12 on the two-week 4xH100 hold (job 6134081, cp088, TP=4; `hpc/logs/
deepseek_h100_hold_20261009.sh`): its first start on 2026-10-10 died in `ncclCommInitRank`
("unhandled cuda error") because NCCL's RAS client could not bind its port next to another
user's NCCL job on the shared node -- `NCCL_RAS_ENABLE=0` fixed it. Vanilla DeepSeek: 0 of 116
PMIDs real (pipeline DeepSeek keeps 46-79 of 67-98), shift error 0.1 ppm, k_ex within 3x in
16-25 of 19-27 claims, 4-10 of its 17-28 candidates shared with the pipeline, verdicts agreeing
6/8 (heart), 3/4 (disc) and 1/7 (pancreas), never missing a consensus target. Before that, six
attempts on 4xL40S
(`hpc/logs/vanilla_deepseek_l40s_try3_20261008.sh`) showed it cannot run there -- with
`VLLM_USE_DEEP_GEMM=0` the weights load (37.8 GiB/GPU at TP=4), but vLLM 0.29's DeepSeek-V4 model
calls DeepGEMM's hyper-connection kernel unconditionally and it asserts "Unsupported architecture"
on Ada (sm89); V4 also requires the fp8 MLA KV cache. Two incidental lessons: SLURM does not
isolate GPU memory on the shared L40S nodes (probe `nvidia-smi memory.used` before serving) and
port 8000 may be in use (probe `ss -ltn`).
- Citations: the vanilla models cite real-looking PMIDs that resolve to unrelated papers -- 0 of 157
  verified across the six model x tissue cells (0/30, 0/26, 0/20, 0/24, 0/27, 0/30) -- where the same
  models inside the pipeline keep 24-99 of 64-117 (Qwen3.8) and 10-33 of 64-87 (Qwen3.6).
- Candidates: 3-7 of each vanilla list of 15-20 are candidates the pipeline also proposed; the rest
  are non-targets (plasma albumin, ascorbate, free cysteine, bilirubin, citrate, "water rNOE" at 55 M).
  Qwen3.6 never named phosphocreatine in the heart or the protein amide pool in the disc.
- Physics: the vanilla models know the textbook offsets (median shift error 0-0.25 ppm vs the
  reference table) but class creatine as an amine, give exchange rates within 3x of the reference
  in 7-13 of 14-19 claims, and state CEST-effect percentages they have no way to compute; their
  detectability verdicts match the simulation in 2/6-5/6 of shared candidates per cell.

### 2026-10-12: myocardium, one run per model with the clinical-evidence stage, on the 4xH100 hold

DeepSeek-V4-Flash (TP=4), Qwen3.8-27B and Qwen3.6-35B-A3B once each on the two-week hold (job
6134081, `hpc/logs/myocardium_clinical_20261012.sh`; 15-26 min per run), with the new
clinical-evidence stage and full-text mode: `outputs/compare_myocardium_clinical_20261012.html`
and `outputs/myocardium_h100_*_20261012T2*.html`.
- Phosphocreatine and creatine are now distinct in the evidence: PCr rests on 31P-MRS PCr/ATP
  studies (Qwen3.8 four, incl. hypertrophic cardiomyopathy n 26/37; DeepSeek two; Qwen3.6 one),
  creatine on CrCEST in inflammatory myopathy and 1H-MRS total creatine labelled "(Cr + PCr)".
- Verified clinical studies per run: DeepSeek 12 (1 CEST, 11 metabolite-change; 159 humans),
  Qwen3.8 19 (3 + 16; 228 humans, 27 animals), Qwen3.6 1. Full text completed the statistics of
  one study; no pool reached three complete studies, so no pooled effect was claimed.
- Physics: PCr detectable in the two runs that simulated it (2.9 % at 7 T; DeepSeek's chemist
  left it unsimulated), glutamine and glycogen marginal, taurine and glutamate unknown (no amine rate).

### 2026-10-13: full round -- seven tissues x three models x three replicates with clinical evidence, on the 4xH100 hold

63 runs (myocardium, intervertebral disc, breast, liver, pancreas, brain, skeletal muscle; DeepSeek-V4-Flash
TP=4 then Qwen3.8-27B and Qwen3.6-35B-A3B in parallel; `hpc/logs/round_20261012.sh`, 2026-10-12 15:40 to
2026-10-13 06:43 on job 6134081), all in `outputs/round_20261012/`: 63 individual reports and one
`compare_<tissue>.html` each. Code `1631a73` (31P-MRS = PCr-only evidence, 1H-MRS = total creatine only).
- Clinical evidence: 730 verified studies across the 63 runs after re-verification with the final
  rules (79 reviews dropped as not primary). DeepSeek finds most (23-80 per tissue over three runs),
  Qwen3.8 12-59, Qwen3.6 0-32; Qwen3.6's three skeletal-muscle runs made no PubMed search at all
  yet wrote "Searched for ..." notes -- the agent now nudges a model that answered without searching
  every candidate. Full text completed the statistics of 0-19 studies per model and tissue. The
  first pooled effect appeared (DeepSeek, breast, APT in tumours: 3 studies, n 119/65, Hedges' g
  +0.75 [-0.93, +2.43], I^2 96 %, reported as no consistent difference).
- Physics consensus (runs proposing it): protein amide pool detectable in every tissue (7-9 of 9);
  glycogen in liver (9/9) and muscle (9/9), marginal in heart, below threshold elsewhere;
  phosphocreatine 9/9 in muscle and 6/8 in heart, creatine 9/9 in muscle and 6/7 in heart (the
  two never pooled); GAG/keratan sulfate 9/9 in the disc; myo-inositol 9/9 in brain; glutamate,
  taurine unknown everywhere (no amine rate); GSH, lactate, glucose, ATP, carnosine below threshold.
- Comparison-page fix found by this round: parenthetical descriptors such as "(free)" had acted as
  aliases and merged creatine, glutamine and glutamate into one muscle row; descriptors are ignored
  now and glutamate, glutamine and Glx are distinct pools. The driver's replicate loop had an
  off-by-one that stopped after one replicate per tissue (first pass: 20 runs); fixed and resumed.

### 2026-10-13: the 63-run round re-simulated on per-tissue MT pools and fibroglandular breast (defq, 12 min)

- Trigger: the user asked whether the MT pool and the direct water saturation were the same for
  every tissue. They were not fixed: the MT pool was the tissue's own where the table had qMT values
  (brain, liver, skeletal muscle; myocardium fraction only) and the generic 0.05 / 30 s^-1 / 10 us
  pool elsewhere; direct saturation already followed each tissue's water T1/T2. Found while
  checking: "breast" resolved to the table's only breast entries, adipose tissue (T1 423 ms,
  T2 154 ms at 3 T), so the round's breast runs were simulated in fat.
- Fix (commit 62ac3f9): fibroglandular T1 1680 / T2 71 ms (PMID 20882630) and PSR 9.5-16.7 %
  (PMID 28090588) added, myocardium BPF 8.2 % (PMID 42381548) added, fat entries only match fat
  ROIs; each per-field Z-spectrum now reports direct saturation, MT + background and the
  metabolite's own contrast at the offset. Disc and pancreas keep the labelled generic MT pool
  (no qMT abstract for normal tissue).
- `hpc/logs/resim_round_20261013.sbatch`: all 63 runs re-simulated in 12 min on a 48-core defq
  node (12 workers x OMP_NUM_THREADS=4; job 6166246); `_resim_` reports and
  `compare_<tissue>_resim.html` in outputs/round_20261012/ are the current physics, the originals
  stay beside them. Only breast and myocardium changed: breast APT median 5.5 -> 8.7 % (9/9),
  creatine 3/6 unchanged, glucose 2/3 -> 0/3, myo-inositol 1/3 -> 0/3, GAG 2/3 (1.1 -> 0.7 %);
  myocardium APT 10.1 -> 9.4 %, PCr 6/8 (1.0 -> 0.9 %), creatine 6/7 (1.4 -> 1.2 %), glycogen
  8/9 (0.76 -> 0.74 %), ATP 2/6 -> 1/6.

### 2026-10-14: nine-replicate round -- seven tissues x three models x nine, 189 runs in 20 h on the 4xH100 hold

- `hpc/logs/round9_20261013.sh 6134081` (nohup, 2026-10-13 16:34 -> 2026-10-14 12:32), code `a13365b`
  (per-tissue MT pool, fibroglandular breast, DS/MT/CEST split), outputs in `outputs/round_20261013_x9/`
  (189 reports + `compare_<tissue>.html`, 27 runs per page; HTML committed in three chunks, 1.8 GB).
- Throughput: DeepSeek TP=4 with TWO pipeline processes against the one server (63 runs in 12 h,
  18-22 min per run each); then Qwen3.8 on two servers (GPU 0 / GPU 2, two processes each) and
  Qwen3.6 on GPU 1 (two processes), 126 runs in 8 h. The first two DeepSeek runs took 2 h each:
  31 BLAS threads per process on the 16-CPU step; `hpc/run_with_server.sh` now caps OMP/OpenBLAS/MKL
  at 4 (commit f5ce33a), the two runs were killed and the loop's retry produced them in 18-22 min.
  One Qwen3.6 disc run failed once (log overwritten by the successful retry); nothing abandoned.
- Per model over 63 runs (comparison-page aggregates): DeepSeek 21 min/run, 8.0 candidates, 86 % of
  citations kept (244/1783 dropped), 14 % wrong structures (49/355), 34 runs with a failed
  completeness check; Qwen3.8 25 min, 6.7 candidates, 84 % kept (360/2327), 68 % wrong structures
  (203/297), 29 runs; Qwen3.6 10 min, 6.2 candidates, 39 % kept (1024/1680), 36 % wrong structures
  (102/287), 27 runs.
- Consensus over 27 runs per tissue (detectable / proposing, median best contrast): myocardium APT
  21/22 (8.9 %), creatine 19/22 (2.0 %), PCr 14/15 (1.7 %), glycogen 21/26 (0.9 %); disc GAG 27/27
  (11.8 %), APT 25/26 (8.8 %); breast APT 23/23 (7.9 %), GAG 10/13 (0.7 %), creatine 1/9; liver
  glycogen 26/27 (5.4 %), APT 23/24 (6.8 %), GSH 6/23; pancreas APT 25/25 (7.8 %), glutamine 2/18;
  brain myo-inositol 26/27 (0.85 %), APT 23/23 (11.3 %), creatine 17/18 (0.9 %; proposed in 18 of 27
  runs now), NAA 7/27; skeletal muscle glycogen 27/27 (2.8 %), PCr 16/16 (2.2 %), creatine 18/20
  (1.6 %), APT 20/21 (8.0 %). Glutamate and taurine stay unknown (no amine rate) in every tissue.
