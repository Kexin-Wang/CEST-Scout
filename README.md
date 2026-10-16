# CEST_Scout

Five LLM agents plus a deterministic physics stage that, for a body part or tissue (brain, liver,
intervertebral disc, ...), propose which endogenous metabolites are realistic CEST (Chemical Exchange Saturation Transfer) MRI targets
there and design an in vivo scan to check them. Every physical number in the simulation comes
from a cited reference table or a sweep, never from hallucination. If there are lots of relevant clinical papers, add the meta-analysis for the relative disease as well.

## Quick start

```bash
git clone https://github.com/Kexin-Wang/CEST-Scout.git && cd CEST-Scout
python -m venv .venv && source .venv/bin/activate      # Python 3.11
pip install -r requirements.txt
cp .env.example .env                                   # then edit: endpoint, model names, NCBI_EMAIL
python -m cest_scout.cli "liver"                       # -> outputs/liver_<model>_<timestamp>.{json,md,html}
python -m pytest -q                                    # ~4 min; one live PubMed smoke test, skipped offline
```

The agents talk to any OpenAI-compatible chat endpoint that supports tool calling: set
`OPENAI_BASE_URL`, `OPENAI_API_KEY` and the four `OPENAI_MODEL_*` names in `.env`. The results in
`outputs/` were made with open-weight models (DeepSeek-V4-Flash, Qwen3.8-27B, Qwen3.6-35B-A3B)
served locally with vLLM, thinking disabled (`LLM_DISABLE_THINKING=true`); the serving and batch
scripts in `hpc/` are written for one SLURM cluster and need their paths and module names
adapted. PubMed and PubChem are queried live, so a run needs network access and an `NCBI_EMAIL`
(an `NCBI_API_KEY` raises the rate limit). The Bloch-McConnell engine is vendored at
`cest_scout/skills/bloch_mcconnell.py` (from [CEST_simulator](https://github.com/Kexin-Wang/CEST_simulator), MIT).

A run takes 10-25 min per tissue (one GPU-served model), most of it in the agents' PubMed searches
and the clinical-evidence stage; the deterministic sweeps take a few minutes with
`OMP_NUM_THREADS=4` (unbounded BLAS threads on a shared node can make them 50x slower).

## Outputs in this repository

`outputs/round_20261013_x9/` holds the nine-replicate round of 2026-10-13: seven tissues
(myocardium, intervertebral disc, breast, liver, pancreas, brain, skeletal muscle) x three models
x nine replicates = 189 self-contained HTML reports, plus one `compare_<tissue>.html` page per
tissue (27 runs each). The folder is 1.8 GB; to clone without it:

```bash
git clone --filter=blob:none --sparse https://github.com/Kexin-Wang/CEST-Scout.git
cd CEST-Scout && git sparse-checkout set --no-cone '/*' '!outputs'
```

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

1. **Literature agent** — PubMed search for metabolites > 1 mM in the tissue, with species,
   subject count and setting per citation; concentrations grounded in HMDB/BioNumbers indexes.
2. **Chemist agent** — structure (checked against PubChem and replaced when wrong), exchangeable
   proton groups, and the *class* of each group: amide, guanidinium, amine, hydroxyl, imidazole,
   thiol, phosphate, rnoe (aliphatic C-H relayed NOE, ~−3.5 ppm; glycogen ~−1 ppm), rnoe16
   (−1.6 ppm pool). The class is the only model choice that selects physics.
3. **Deterministic simulation** (`cest_scout/pipeline.py`) — for each group, the exchange rate
   (or effective rNOE transfer rate) and offset are looked up by class from
   `cest_scout/data/exchange_reference.json`: numbers quoted from PubMed abstracts (or, labelled
   so, from a PubMed Central open-access full text), PMID and quote verified. The rate prefers
   same-tissue and in-vivo measurements; the offset is the median of the metabolite's own values
   from every setting (an in-vivo paper usually states the offset it saturated at, not a resolved
   shift). Borrowed values (another tissue, species or a phantom) are labelled as such in every
   report; a class with no verified entry is "not simulated: unknown". Concentration from the
   literature agent, B1/Tsat from a sweep (no SAR ceiling: the earlier cap was an assumption, not a
   calculation, and was removed), water/MT parameters from
   `cest_scout/data/tissue_reference.json` (abstract-verified, table value, or generic default).
4. **Physicist agent** — reviews the simulation (cannot change it): literature that probed the
   metabolite, class errors, in-vivo shift discrepancies, overlap and direct-saturation
   diagnostics, quantification method for the swept B1/Tsat.
5. **Experimentalist agent** — designs the verification scan; the pipeline enforces that the target
   tissue is an ROI, that every (ROI, metabolite) pair has a concentration, and that the pulse
   train adds up, then simulates each pair with that ROI's tissue parameters.
6. **Clinical-evidence agent** — for each pool, the diseases its CEST/NOE signal has been applied
   to, and the diseases in which the metabolite itself is shown to change by any method (MRS,
   metabolomics, histology, mechanistic or animal work); per abstract: species, group sizes, group
   means ± SD, direction, p value, with the verbatim sentence. The pipeline verifies every PMID and quote, discards any number not in
   its quote, counts humans and animals, and pools the group difference (Hedges' g, random
   effects, 95 % CI, I²) where at least three verified studies with complete statistics exist for
   one pool and disease category. Statistics an abstract omits are read from the PubMed Central
   open-access full text (one extraction call per study, quote and numbers verified against that
   text, filled only where missing, marked "full text"; `CLINICAL_FULLTEXT=false` disables it;
   `python -m cest_scout.clinical_fulltext <run.json>` upgrades a saved run). Reported as
   "Potential clinical impact".

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
  full text (then labelled "from the full text, not the abstract"), enter the reference tables;
  the report's Simulation-inputs table shows the chemist's own ppm estimate beside the reference
  offset when they differ, marked "not used". No abstract states an
  amine exchange rate, so amine groups are "unknown"; the rNOE transfer rate is a single
  approximate phantom value (~5 s⁻¹, PMID 25996515) and the −1.6 ppm pool has an offset but no
  rate; the amide and creatine rates are borrowed from brain and muscle. Extend the tables by
  adding verified records (`cest_scout/skills/build_exchange_reference.py`), not by typing numbers.
- The simulation is a first-pass detectability estimate: water + semisolid MT + target + up to
  three background pools with generic solute T1/T2; the field-strength sweep is continuous-wave
  from equilibrium (an upper bound), the verification-scan predictions use the actual pulse
  train, readout and TR in steady state.
- The semisolid MT pool is the tissue's own where the table has it (fraction, k_ex, T2b from qMT
  abstracts: brain, liver, skeletal muscle, myocardium, breast fibroglandular tissue) and the
  stated generic pool (0.05 of water, 30 s^-1, 10 us) where it does not (disc, pancreas), labelled
  as such in the tissue-parameter table. Direct water saturation follows each tissue's own water
  T1/T2. Each per-field Z-spectrum caption splits the drop at the offset into direct saturation,
  MT + background pools, and the metabolite's own contrast.
- Uncertainty corners are only as wide as their inputs; a class with one verified measurement
  has an unknown rate spread, and the report says so.
- Water T1/T2 at a field with no measurement are estimated by a power law in B0 (T1 rising with
  field, T2 nearly flat; form from Bottomley 1984, PMID 6482839) using the tissue's own
  multi-field data or the table-median exponent, and are labelled "estimated" in the tissue table.
- Citation species/setting fields are model-extracted from MeSH hints, not checked against full
  texts. Structure labels only mark groups whose wording matches a fixed set of SMARTS patterns.
- Always needs human-in-the-loop, as these findings need to be tested in the real world. Next step could be generating Pulseq sequence and seamlessly implement to the scanner.

