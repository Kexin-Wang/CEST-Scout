# CEST Scout

**CEST Scout** is an in-house, agent-based platform designed to streamline the translation of **chemical exchange saturation transfer (CEST)** MRI to new tissues and organs. While CEST is a powerful, noninvasive metabolite imaging technique, adapting it to a new application typically requires extensive literature review, sequence optimization, simulation, and experimental planning.

To address this bottleneck, CEST Scout uses **five specialized large language model agents** to:

- identify potentially detectable metabolites from literature- and textbook-based sources,
- evaluate their detectability using **Bloch–McConnell simulations**,
- suggest promising clinical applications, and
- propose validation experiments with detailed sequence designs.

By combining domain-specific AI agents with physics-based simulation, CEST Scout aims to provide a more structured, transparent, and efficient workflow for CEST study design and exploration.

**Example report:** [brain, DeepSeek-V4-Flash](example/), readable directly on GitHub.

## Quick start

```bash
git clone https://github.com/Kexin-Wang/CEST-Scout.git && cd CEST-Scout
python -m venv .venv && source .venv/bin/activate      # Python 3.11
pip install -r requirements.txt
cp .env.example .env                                   # then edit: LLM_PRESET, OPENAI_BASE_URL, NCBI_EMAIL
python -m cest_scout.cli "liver"                       # -> outputs/liver_<model>_<timestamp>.{json,md,html}
python -m pytest -q                                    # ~4 min; one live PubMed smoke test, skipped offline
```

PubMed and PubChem are queried live, so a run needs network access and an `NCBI_EMAIL` (an
`NCBI_API_KEY` raises the rate limit). The Bloch-McConnell engine is vendored at
`cest_scout/skills/bloch_mcconnell.py` (from [BMsim](https://github.com/Kexin-Wang/BMsim)).

## Models

The agents talk to any OpenAI-compatible chat endpoint with tool calling. `LLM_PRESET` in `.env`
selects one of the three open-weight models the results in `outputs/` were made with, served by
[vLLM](https://docs.vllm.ai) (0.29) on your own GPUs, or OpenAI's API:

| `LLM_PRESET` | Checkpoint | GPUs used here | vLLM tool / reasoning parser |
|---|---|---|---|
| `deepseek-v4-flash` | `deepseek-ai/DeepSeek-V4-Flash` | 4x H100 80 GB (TP 4) or 2x H200 (TP 2); Hopper only | `deepseek_v4` / `deepseek_v4` |
| `qwen3.8-27b` | `Qwen/Qwen3.8-27B-FP8` | 1x H100 80 GB | `qwen3_coder` / `qwen3` |
| `qwen3.6-35b-a3b` | `Qwen/Qwen3.6-35B-A3B-FP8` | 1x H100 80 GB | `qwen3_coder` / `qwen3` |
| `openai` (default) | `gpt-5.5` via api.openai.com | none | - |

A local preset sets all four agents to that model and turns thinking off; `OPENAI_BASE_URL` must
point at the server, and `OPENAI_API_KEY` is its `--api-key` (any non-empty string if none). An
explicit `OPENAI_MODEL_LITERATURE` / `_CHEMIST` / `_PHYSICIST` / `_EXPERIMENTALIST` overrides the
preset for that agent. Serving each model as it was served for the results:

```bash
vllm serve Qwen/Qwen3.8-27B-FP8 --served-model-name qwen3.8-27b --max-model-len 131072 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 --language-model-only
vllm serve Qwen/Qwen3.6-35B-A3B-FP8 --served-model-name qwen3.6-35b-a3b --max-model-len 131072 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 --language-model-only
vllm serve deepseek-ai/DeepSeek-V4-Flash --served-model-name deepseek-v4-flash --tensor-parallel-size 4 \
  --max-model-len 131072 --enable-auto-tool-choice --tool-call-parser deepseek_v4 --reasoning-parser deepseek_v4 \
  --trust-remote-code --kv-cache-dtype fp8_ds_mla
```

DeepSeek-V4-Flash needs Hopper GPUs and the
`fp8_ds_mla` KV cache. On shared nodes, `NCCL_RAS_ENABLE=0` avoids an NCCL start-up failure when
another job holds NCCL's port. `hpc/` holds the SLURM scripts that served these models and ran
the batch rounds (`hpc/README.md`); they are written for one cluster and need their paths and
module names adapted.

A run takes 10-25 min per tissue (one GPU-served model), most of it in the agents' PubMed searches
and the clinical-evidence stage; the deterministic sweeps take a few minutes with
`OMP_NUM_THREADS=4` (unbounded BLAS threads on a shared node can make them 50x slower).

## Outputs in this repository

`outputs/` holds the nine-replicate round: seven tissues (myocardium, intervertebral
disc, breast, liver, pancreas, brain, skeletal muscle) x three models x nine replicates = 189
self-contained HTML reports (`<tissue>_<model>_<timestamp>.html`), plus one `compare_<tissue>.html`
page per tissue (27 runs each); start with the comparison pages. New runs are written to the same
folder. It is 1.8 GB; to clone without it:

```bash
git clone --filter=blob:none --sparse https://github.com/Kexin-Wang/CEST-Scout.git
cd CEST-Scout && git sparse-checkout set --no-cone '/*' '!outputs'
```

GitHub shows `.html` files as source code, so open a report after downloading it (each file is
self-contained), or read [`example/`](example/): the brain report of `outputs/brain_deepseek_v4_flash_20261014T044224Z.html`
as Markdown with its 114 figures.

The pipeline's own `.gitignore` keeps run JSON/Markdown files out of git; only HTML reports are
tracked.

## What a run produces

`outputs/<tissue>_<model>_<timestamp>.{json,md,html}` — the HTML is self-contained. Per candidate:

- concentration with the verbatim literature excerpt it was read from (or an explicit estimate);
- 2D structure in its pH-7.4 protonation state, exchangeable protons labelled with the reference
  shift the simulation used; exchangeable protons (orange) and rNOE C-H protons (blue) marked in a
  colour-blind-safe palette;
- multi-pool Bloch-McConnell Z-spectra with and without the metabolite at 0.5/1.5/3/5/7 T,
  swept B1/Tsat per field and the optimum is reported as such, and a verdict: **detectable** (chemical exchange downfield
  or relayed NOE upfield, ≥ 0.5 % with-vs-without contrast), **not detectable**, or **unknown**
  (no verified reference rate, so nothing was simulated — never reported as "no");
- a robustness grade against the concentration range and the reference-rate spread;
- the physicist's critique and the supporting literature, every PMID verified against PubMed;
- **potential clinical impact**: the diseases this pool's CEST/NOE signal has been applied to and
  those in which the metabolite is shown to change by any method, one verified study per row (species, n, group means ± SD, direction, p value, PMID with the quote),
  counts of human and animal studies and subjects, and a pooled group difference (Hedges' g with
  95 % CI and I²) where three or more verified studies with complete statistics exist;
- an ROI-differential verification scan designed for the tissue (plane, slice, ROIs including the
  target tissue, one compromise pulse train), simulated as written, with predicted per-ROI contrast
  and its range over ±30 % B1;
- the tissue T1/T2/MT parameters used, as a table with per-value provenance, and a run provenance
  table (model, endpoint, tokens, tool calls, retries, completeness checks, deterministic settings).

## Pipeline
<img width="1820" height="1300" alt="Fig1" src="https://github.com/user-attachments/assets/eed4aee7-ede7-4269-91ca-b9166ead4a8d" />

1. **Literature agent** — PubMed search for metabolites > 1 mM in the tissue, with species,
   subject count and setting per citation; concentrations grounded in HMDB/BioNumbers indexes.
2. **Chemist agent** — structure (checked against PubChem and replaced when wrong), exchangeable
   proton groups, and the *class* of each group: amide, guanidinium, amine, hydroxyl, imidazole,
   thiol, phosphate, rnoe (aliphatic C-H relayed NOE, ~−3.5 ppm; glycogen ~−1 ppm), rnoe16
   (−1.6 ppm pool). 
3. **Deterministic simulation** (`cest_scout/pipeline.py`) — for each group, the exchange rate
   (or effective rNOE transfer rate) and offset are looked up by class from
   `cest_scout/data/exchange_reference.json`: numbers quoted from PubMed abstracts (or, labelled
   so, from a PubMed Central open-access full text), PMID and quote verified. The rate prefers
   same-tissue and in-vivo measurements; the offset is the median of the metabolite's own values
   from every setting (an in-vivo paper usually states the offset it saturated at, not a resolved
   shift). Borrowed values (another tissue, species or a phantom) are labelled as such in every
   report; a class with no verified entry is "not simulated: unknown". Concentration from the
   literature agent, B1/Tsat from a sweep, water/MT parameters from
   `cest_scout/data/tissue_reference.json` (abstract-verified, table value, or estimated by scaling law, all labeled as it is).
4. **Physicist agent** — reviews the simulation (cannot change it): literature that probed the
   metabolite, class errors, in-vivo shift discrepancies, overlap and direct-saturation
   diagnostics, quantification method for the swept B1/Tsat.
5. **Experimentalist agent** — designs the verification scan; the pipeline enforces that the target
   tissue is an ROI, that every (ROI, metabolite) pair has a concentration, and that the pulse
   train adds up, then simulates each pair with that ROI's tissue parameters.
6. **Clinical-evidence agent** — for each pool, the diseases its CEST/NOE signal has been applied
   to, and the diseases in which the metabolite itself is shown to change by any method (MRS,
   metabolomics, histology, mechanistic or animal work). 

## Command reference

| Command | What it does |
|---|---|
| `python -m cest_scout.cli "<tissue>" [--output-dir DIR] [--run-tag TAG]` | one full run: six stages, report in `outputs/` |
| `python -m cest_scout.compare <run.json>... --out <path>` | comparison page across runs of one tissue (any number of runs, any models) |
| `python -m cest_scout.resimulate <run.json>... [--out-dir DIR]` | re-run the deterministic stages of saved runs on the current reference tables, no LLM calls |
| `python -m cest_scout.clinical_fulltext <run.json>` | complete a saved run's clinical statistics from PubMed Central full text |
| `python -m cest_scout.vanilla "<tissue>"` / `cest_scout.compare_vanilla` | the no-tools baseline (same model asked directly) and its scoring against pipeline runs |
| `python -m cest_scout.skills.build_exchange_reference records.json` | add abstract-verified exchange-rate records to the table (see the module docstring) |
| `python -m cest_scout.skills.build_tissue_reference records.json` | same for tissue T1/T2/MT records |

`.env` (never committed) holds `NCBI_EMAIL`, optional `NCBI_API_KEY`, and the endpoint/model
settings; `hpc/README.md` documents the vLLM presets, servers and the batch drivers used for the
rounds in `outputs/`.

Comparing models on one tissue:

```bash
python -m cest_scout.compare outputs/liver_<a>_*.json outputs/liver_<b>_*.json --out outputs/compare_liver
```

The comparison page reports cost and loop reliability, citation-verification drop rates, structure
checks, per-candidate verdicts and contrasts, within-model replicate spread, a cross-run consensus
table, a coloured pairwise-agreement heatmap (candidate Jaccard and verdict agreement), the tissue
parameters each run used, and every simulation input with its source. Old runs can be re-simulated
on the current reference table with `python -m cest_scout.resimulate <run.json>`.

## Limits

- Only numbers quoted **verbatim** from a PubMed abstract, or from a PubMed Central open-access
  full text (then labelled "from the full text, not the abstract"), enter the reference tables.
- The simulation is a first-pass detectability estimate: water + semisolid MT + target + up to
  three background pools with generic solute T1/T2; the field-strength sweep is continuous-wave
  from equilibrium (an upper bound), the verification-scan predictions use the actual pulse
  train, readout and TR in steady state.
- Always needs human-in-the-loop, as these findings need to be tested in the real world. Next step could be generating Pulseq sequence and seamlessly implement to the scanner.

