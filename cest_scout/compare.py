"""Compare CEST_Scout runs of one tissue made with different models, on equal terms.

    python -m cest_scout.compare outputs/liver_qwen3-32b-fp8_*.json outputs/liver_kimi-linear-48b-a3b_*.json \
        --out outputs/compare_liver

Reads the JSON files the CLI writes and produces <out>.md and <out>.html. Every number here comes
either from the run's provenance record (model, time, tokens, retries) or from the deterministic
checks each agent stage already ran on its own output -- citation/quote/structure verification,
eligibility flags, simulation coverage, completeness checks. Nothing is re-judged by an LLM, and the
Bloch-McConnell physics is identical across runs, so a contrast difference between two models is a
difference in the INPUTS they researched (concentration, chemical shift, exchange rate), never in
the simulator.
"""
from __future__ import annotations

import argparse
import base64
import html as _html
import json
import re
import sys
from collections import Counter, defaultdict
from io import BytesIO
from itertools import combinations
from statistics import mean, median
from dataclasses import dataclass, field
from pathlib import Path

from cest_scout.agents.experimentalist_agent import protocol_problems
from cest_scout.config import CONTRAST_THRESHOLD_PERCENT, FIELD_STRENGTHS_T
from cest_scout.pipeline import detectability_of, tissue_parameter_rows
from cest_scout.schemas import ClinicalReport, ChemistReport, FinalReport, InVivoVerificationPlan, LiteratureReport, PhysicistReport

# Sequential blue ramp (steps 100..700 of the validated default palette): one hue, light -> dark,
# for the agreement heatmap and the matching table-cell colours.
_SEQ = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf",
        "#1c5cab", "#184f95", "#104281", "#0d366b"]


_UNIT_SUBS = (
    (re.compile(r"\bs\^-1\b"), "s<sup>&minus;1</sup>"), (re.compile(r"\bk_ex\b"), "k<sub>ex</sub>"),
    (re.compile(r"\bB1_rms\b"), "B<sub>1,rms</sub>"), (re.compile(r"\bB([01])\b"), r"B<sub>\1</sub>"),
    (re.compile(r"\bT2b\b"), "T<sub>2b</sub>"), (re.compile(r"\bT([12])\b"), r"T<sub>\1</sub>"),
    (re.compile(r"\buT\b"), "&micro;T"), (re.compile(r"(\d)x(\d)"), r"\1&times;\2"),
)


def _units_html(text: str) -> str:
    """Real super/subscripts for units in already-escaped HTML text: s^-1 -> s<sup>-1</sup>,
    k_ex -> k<sub>ex</sub>, B0/B1/T1/T2 subscripts, uT -> µT."""
    for pat, rep in _UNIT_SUBS:
        text = pat.sub(rep, text)
    return text


def _seq_color(x: float) -> tuple[str, str]:
    """(background, text colour) for a 0..1 value on the sequential ramp."""
    x = min(max(float(x), 0.0), 1.0)
    bg = _SEQ[round(x * (len(_SEQ) - 1))]
    return bg, ("#ffffff" if x > 0.55 else "#0b0b0b")

# "[Eligibility] ...", "[Citation verification] ...", "[Quote verification skipped: ...]"
_TAG_RE = re.compile(r"\[([A-Z][A-Za-z]*(?: [a-z]+)*)(?:\]|:)")
_DROPPED_RE = re.compile(r"\bdropped --")


@dataclass
class RunSummary:
    path: Path
    label: str
    models: str = ""
    code_version: str = ""
    elapsed_min: float | None = None
    stage_minutes: dict[str, float] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    nudges: int = 0
    json_repairs: int = 0
    output_retries: int = 0
    checks_failed: list[str] = field(default_factory=list)
    n_candidates: int = 0
    basis_counts: Counter = field(default_factory=Counter)
    n_reference_grounded: int = 0
    tag_counts: Counter = field(default_factory=Counter)
    n_citations_kept: int = 0
    n_citations_dropped: int = 0
    # Clinical evidence (stage 5): verified studies per run, by evidence type, how many had their
    # statistics completed from open-access full text, how many pooled effects, how many dropped.
    n_clinical_studies: int = 0
    n_clinical_cest: int = 0
    n_clinical_change: int = 0
    n_clinical_fulltext: int = 0
    n_clinical_pooled: int = 0
    n_clinical_dropped: int = 0
    n_clinical_humans: int = 0
    n_clinical_animals: int = 0
    n_structures_with_smiles: int = 0
    n_structures_replaced: int = 0
    n_structures_unverifiable: int = 0
    n_verdicts: int = 0
    coverage_fraction: float = 0.0
    n_coverage_gaps: int = 0
    n_detectable: int = 0
    n_unknown: int = 0
    n_robust: int = 0
    n_fragile: int = 0
    n_sar_limited: int = 0
    n_organ_predictions: int = 0
    n_organs: int = 0
    undifferentiated_flag: bool = False
    protocol: str = ""
    protocol_problems: list[str] = field(default_factory=list)
    tool_error_samples: list[str] = field(default_factory=list)
    final: FinalReport | None = None
    lit: LiteratureReport | None = None
    plan: InVivoVerificationPlan | None = None


def _label_from_filename(path: Path) -> str:
    # liver_<tag>_<stamp>.json -> <tag>; liver_<stamp>.json -> the stem
    parts = path.stem.split("_")
    if len(parts) >= 3 and re.fullmatch(r"\d{8}T\d{6}Z", parts[-1]):
        return "_".join(parts[1:-1])
    return path.stem


def load_run(path: Path, label: str | None = None) -> RunSummary:
    data = json.loads(path.read_text())
    final = FinalReport.model_validate(data["final"])
    lit = LiteratureReport.model_validate(data["literature"])
    chem = ChemistReport.model_validate(data["chemist"])
    phys = PhysicistReport.model_validate(data["physicist"])
    plan = InVivoVerificationPlan.model_validate(data["experimentalist"]) if data.get("experimentalist") else None
    clinical = ClinicalReport.model_validate(data["clinical"]) if data.get("clinical") else None

    s = RunSummary(path=path, label=label or _label_from_filename(path), final=final, lit=lit, plan=plan)

    if final.provenance:
        p = final.provenance
        s.code_version = p.code_version
        s.elapsed_min = p.elapsed_s / 60
        models = sorted({st.model for st in p.stages})
        s.models = ", ".join(models)
        for st in p.stages:
            s.stage_minutes[st.agent] = st.elapsed_s / 60
            s.prompt_tokens += st.prompt_tokens
            s.completion_tokens += st.completion_tokens
            s.llm_calls += st.llm_calls
            s.tool_calls += st.tool_calls
            s.tool_errors += st.tool_errors
            s.nudges += st.nudges
            s.json_repairs += st.json_repairs
            s.output_retries += st.output_retries
            s.tool_error_samples += [f"[{st.agent}] {e}" for e in st.tool_error_samples]
            if st.output_check_passed is False:
                s.checks_failed.append(st.agent)
        if not label and len(models) == 1:
            s.label = models[0]

    s.n_candidates = len(lit.metabolites)
    for m in lit.metabolites:
        s.basis_counts[m.concentration_basis or "unknown"] += 1
        if m.reference_concentrations:
            s.n_reference_grounded += 1
        s.n_citations_kept += len(m.citations)
        s.n_citations_dropped += len(_DROPPED_RE.findall(m.notes))
        s.tag_counts.update(_TAG_RE.findall(m.notes))

    for f in chem.findings:
        note = f.structure_verification_note.lower()
        # A "rejected" structure was wrong too, but PubChem had nothing to replace it with, so the
        # finding now carries no SMILES at all -- count the original proposal, not the cleared field.
        if f.smiles or "rejected" in note:
            s.n_structures_with_smiles += 1
        if "replaced" in note or "rejected" in note:
            s.n_structures_replaced += 1
        elif note:
            s.n_structures_unverifiable += 1

    for e in (clinical.entries if clinical else []):
        s.n_clinical_studies += len(e.studies)
        s.n_clinical_cest += e.n_cest_studies
        s.n_clinical_change += e.n_change_studies
        s.n_clinical_fulltext += sum(1 for st in e.studies if st.stats_source.startswith("full text"))
        s.n_clinical_pooled += len(e.pooled_effects)
        s.n_clinical_dropped += e.n_studies_dropped
        s.n_clinical_humans += e.n_humans
        s.n_clinical_animals += e.n_animals

    s.n_verdicts = len(phys.verdicts)
    covered = sum(len(v.simulated_contrast_percent) for v in phys.verdicts)
    denominator = len(phys.verdicts) * len(FIELD_STRENGTHS_T)
    s.coverage_fraction = covered / denominator if denominator else 0.0
    # From the data, not from simulation_coverage_note: that field is in the schema the model
    # fills, so it can carry the model's own prose even when nothing is missing.
    s.n_coverage_gaps = sum(1 for v in phys.verdicts if len(v.simulated_contrast_percent) < len(FIELD_STRENGTHS_T))
    for v in phys.verdicts:
        s.n_citations_kept += len(v.verifying_citations) + len(v.extraction_method_citations)
        s.n_citations_dropped += len(_DROPPED_RE.findall(v.citation_verification_note))
        s.n_sar_limited += sum(1 for seq in v.sequence_recommendations if seq.sar_limited)
    s.n_detectable = sum(1 for m in final.metabolites if m.detectable_by_cest)
    s.n_unknown = sum(1 for m in final.metabolites if _status(m) == "unknown")
    s.n_robust = sum(1 for m in final.metabolites if m.detectability_robustness == "robust")
    s.n_fragile = sum(1 for m in final.metabolites if m.detectability_robustness == "fragile")

    if plan:
        s.n_organ_predictions = len(plan.organ_predictions)
        s.n_organs = len({p.organ.strip().lower() for p in plan.organ_predictions})
        s.undifferentiated_flag = "identical across every organ" in plan.caveats or "identical across every ROI" in plan.caveats
        s.protocol = ""  # filled below; prefixed with the plane so the design is visible at a glance
        s.n_citations_dropped += len(_DROPPED_RE.findall(plan.caveats))
        sp = plan.scan_protocol
        plane = f"{plan.imaging_plane}; " if plan.imaging_plane else ""
        target = [r.name for r in plan.roi_set if r.is_target_tissue]
        target_bit = f"target ROI {', '.join(target)}; " if target else ""
        s.protocol = (f"{plane}{target_bit}{sp.b1_uT:g} uT, {sp.n_pulses}x{sp.pulse_duration_ms:g} ms, "
                      f"Tsat {sp.total_saturation_time_ms:g} ms, TR {sp.tr_ms:g} ms")
        s.protocol_problems = protocol_problems(sp)
    return s


# --- matching the same metabolite across runs -------------------------------------------------

def _norm(name: str) -> str:
    s = name.lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"\b(l|d|dl)-", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


# The macromolecular CEST pools have no single chemical name, so different runs describe the same
# pool in different words ("Mobile Protein Amide Protons", "Amide proton transfer (APT) pool
# (mobile proteins/peptides)"). Each pattern maps such names onto one shared key.
_CANONICAL_POOLS = (
    (re.compile(r"amide|\bapt\b|mobileprotein|mobilepeptide"), "pool:apt"),
    (re.compile(r"glycogen|glycocest"), "pool:glycogen"),
    (re.compile(r"glycosaminoglycan|gagcest|\bgags?\b|chondroitin|keratan|aggrecan|proteoglycan"), "pool:gag"),
)
# Creatine (2.0 ppm) and phosphocreatine (2.6 ppm, its own rate) are different pools, so a name is
# creatine only where "creatine" stands on its own; "Creatine + phosphocreatine" is the combined
# candidate (simulated as the median of both) and stays with creatine.
_PCR = re.compile(r"phosphocreatine|creatine ?phosphate|\bpcr\b|pcrcest")
_CREATINE = re.compile(r"creatine|crcest")
# Glutamate, glutamine and the MRS "Glx" sum are distinct pools too; a head naming both is Glx.
_GLUTAMATE = re.compile(r"glutamate|glutamic|\bglu\b|glucest")
_GLUTAMINE = re.compile(r"glutamine|\bgln\b")
_GLX = re.compile(r"\bglx\b")
# Parenthesized descriptors and generic aliases must not act as names: "(free)" on creatine,
# glutamine and glutamate once merged the three into one row.
_DESCRIPTOR_KEYS = frozenset("free total bound pool pools unit units form forms species target targets reduced oxidized "
                             "oxidised mobile intracellular extracellular cytosolic mitochondrial monomer polymer repeating "
                             "unphosphorylated phosphorylated unbound endogenous exogenous".split())


_APT_HEAD = re.compile(r"amide proton transfer|\bapt\b|mobile[- ]?protein|mobile[- ]?peptide|protein[ /-]*amide|peptide[ /-]*amide")
_APT_HINT = re.compile(r"protein|peptide|mobile|\bapt")


def _canonical_pool(name: str) -> str | None:
    """Decided from the name's HEAD (before any parenthesis, semicolon or colon): a run that
    describes ATP as "(...; amide/imidazole -NH ...)" or glutathione as "(...; thiol + amide +
    -COOH pool)" must not be filed under the protein amide pool -- that joined ATP, glutathione
    and the APT pool into one row. The amide pool needs a protein/peptide/APT mention."""
    head = re.split(r"[(;:]", name, maxsplit=1)[0]
    lowered = re.sub(r"[^a-z0-9 ]+", " ", head.lower())
    squashed = _norm(head)
    full = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    if _APT_HEAD.search(lowered) or (re.search(r"\bamide", lowered) and _APT_HINT.search(full)):
        return "pool:apt"
    for pattern, key in _CANONICAL_POOLS:
        if key == "pool:apt":
            continue
        if pattern.search(lowered) or pattern.search(squashed):
            return key
    # Creatine is decided on the squashed form ("Phospho-creatine" squashes to phosphocreatine),
    # plus the standalone abbreviation on the spaced form ("Cr/PCr" is the combined pool).
    if _CREATINE.search(_PCR.sub("", squashed)) or re.search(r"\bcr\b", _PCR.sub(" ", lowered)):
        return "pool:creatine"
    if _PCR.search(lowered) or _PCR.search(squashed):
        return "pool:phosphocreatine"
    glu, gln = bool(_GLUTAMATE.search(lowered) or _GLUTAMATE.search(squashed)), bool(_GLUTAMINE.search(lowered) or _GLUTAMINE.search(squashed))
    if _GLX.search(lowered) or (glu and gln):
        return "pool:glx"
    if glu:
        return "pool:glutamate"
    if gln:
        return "pool:glutamine"
    return None


def _usable_alias(text: str) -> bool:
    """A parenthetical or alias that is a descriptor, a sentence or a list is not a name."""
    t = text.strip().lower()
    if not t or ":" in t or "+" in t or t.startswith(("as ", "free ", "total ")):
        return False
    return _norm(t) not in _DESCRIPTOR_KEYS


class _Groups:
    """Union-find over normalized names, joined through each run's own alias lists, so 'Creatine'
    in one run and 'creatine (Cr)' with alias 'Cr' in another land in the same row."""

    def __init__(self, runs: list[RunSummary]):
        self.parent: dict[str, str] = {}
        self.display: Counter = Counter()
        for run in runs:
            for m in run.lit.metabolites:
                main = _norm(m.name)
                pool = _canonical_pool(m.name)
                # Parenthesized parts of the name act as aliases too: "Cr (creatine)" must meet
                # "Creatine". Two-letter keys (Cr, Pi) are too ambiguous to join on unless they
                # are the whole name. An alias may file an un-pooled name into a pool ("Protein
                # (Amide Pool)" aliased "APT"), but never moves a pooled name into a DIFFERENT pool:
                # "Creatine and phosphocreatine" aliased "PCr" joined every creatine row to every
                # phosphocreatine row.
                raw_extra = [x for x in re.findall(r"\(([^)]*)\)", m.name) + list(m.aliases) if _usable_alias(x)]
                extra = {_norm(x) for x in raw_extra if pool is None or _canonical_pool(x) in (None, pool)}
                keys = [main] + [k for k in extra if len(k) > 2 and k != main]
                if pool:
                    keys.append(pool)
                keys = [k for k in keys if k]
                for k in keys:
                    self.parent.setdefault(k, k)
                for k in keys[1:]:
                    self._union(keys[0], k)
                self.display[(self.key(m.name), m.name.strip())] += 1

    def _find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def _union(self, a: str, b: str) -> None:
        ra, rb = self._find(a), self._find(b)
        if ra != rb:
            self.parent[rb] = ra

    def key(self, name: str) -> str:
        n = _canonical_pool(name) or _norm(name)
        self.parent.setdefault(n, n)
        return self._find(n)

    def name_for(self, key: str) -> str:
        candidates = [(cnt, -len(n), n) for (k, n), cnt in self.display.items() if k == key]
        return max(candidates)[2] if candidates else key


def _status(m) -> str:
    return detectability_of(m)


def _best_field(m) -> tuple[str | None, float]:
    if not m.simulated_contrast_percent:
        return None, 0.0
    fld, val = max(m.simulated_contrast_percent.items(), key=lambda kv: kv[1])
    return fld, val


def _simulated_pool_bit(m) -> str:
    """Show the pool the physicist actually simulated when it disagrees with the literature
    agent's concentration or uses an implausible proton count -- e.g. glycogen 'at 2 mM' simulated
    as 2 mM x 1000 protons. Runs made before this was recorded have no data and show nothing."""
    sims = [z for z in m.zspectra_by_field if z.simulated_concentration_mM]
    if not sims:
        return ""
    z = sims[0]
    lit = m.typical_concentration_mM
    off = abs(z.simulated_concentration_mM - lit) > 0.25 * max(lit, 1e-9)
    if off or z.n_exchangeable_protons > 12:
        return f" [simulated as {z.simulated_concentration_mM:g} mM x {z.n_exchangeable_protons} H]"
    return ""


def _overlap_rows(runs: list[RunSummary], groups: _Groups) -> list[list[str]]:
    # A run can list the same pool twice under two names (seen: "Glycogen" and "GlycoCEST
    # (Glycogen)"); keep every entry under its group key so the duplicate stays visible.
    per_run: list[dict[str, list]] = []
    for run in runs:
        d: dict[str, list] = defaultdict(list)
        for m in run.final.metabolites:
            d[groups.key(m.name)].append(m)
        per_run.append(d)
    all_keys = Counter()
    for d in per_run:
        all_keys.update(d.keys())
    rows = []
    for key, n_found in sorted(all_keys.items(), key=lambda kv: (-kv[1], groups.name_for(kv[0]).lower())):
        row = [groups.name_for(key), f"{n_found}/{len(runs)}"]
        for d in per_run:
            entries = d.get(key)
            if not entries:
                row.append("—")
                continue
            cells = []
            for m in entries:
                fld, val = _best_field(m)
                flag = {"detectable": "YES", "unknown": "unknown"}.get(_status(m), "no")
                mech = getattr(m, "detection_mechanism", "")
                if flag == "YES" and mech:
                    flag += f" ({mech})"
                at = f" @ {float(fld):g} T" if fld else ""
                seq = next((s for s in m.best_sequence_by_field if fld and abs(s.field_strength_T - float(fld)) < 1e-6), None)
                seq_bit = f", B1 {seq.b1_uT:g} uT / Tsat {seq.saturation_time_ms:g} ms" if seq else ""
                name_bit = f" [as '{m.name}']" if len(entries) > 1 else ""
                rng = m.contrast_range_percent_by_field.get(fld) if fld else None
                range_bit = f" (range {rng[0]:.2f}-{rng[1]:.2f}%; {m.detectability_robustness})" if rng else ""
                per_field = ""
                if m.simulated_contrast_percent:
                    items = sorted(m.simulated_contrast_percent.items(), key=lambda kv: float(kv[0]))
                    per_field = (" [all fields " + "/".join(f"{float(k):g}" for k, _ in items) + " T: "
                                 + "/".join(f"{v:.2f}" for _, v in items) + " %]")
                cells.append(f"{flag} {val:.2f}%{range_bit}{at}{per_field}; {m.typical_concentration_mM:g} mM{seq_bit}"
                             f"{_simulated_pool_bit(m)}{name_bit}")
            row.append(" / ".join(cells))
        rows.append(row)
    return rows


def _agreement_matrices(runs: list[RunSummary], groups: _Groups) -> tuple[list[list[float | None]], list[list[float | None]], list[list[str]]]:
    """(Jaccard matrix, detectability-agreement fraction matrix, 'agree/shared' strings); the
    diagonal is None. A verdict of 'unknown' agrees only with 'unknown', never with 'no'."""
    sets = [{groups.key(m.name): _status(m) for m in run.final.metabolites} for run in runs]
    n = len(runs)
    jac = [[None] * n for _ in range(n)]
    agr = [[None] * n for _ in range(n)]
    txt = [[""] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            shared = set(sets[i]) & set(sets[j])
            union = set(sets[i]) | set(sets[j])
            jac[i][j] = len(shared) / len(union) if union else 0.0
            agree = sum(1 for k in shared if sets[i][k] == sets[j][k])
            agr[i][j] = agree / len(shared) if shared else None
            txt[i][j] = f"{agree}/{len(shared)}" if shared else "n/a"
    return jac, agr, txt


def _agreement(runs: list[RunSummary], groups: _Groups) -> list[list[str]]:
    jac, agr, txt = _agreement_matrices(runs, groups)
    rows = []
    for i, a in enumerate(runs):
        row = [a.label]
        for j, b in enumerate(runs):
            if i == j:
                row.append("—")
                continue
            row.append(f"Jaccard {jac[i][j]:.2f}; verdicts agree {txt[i][j]}")
        rows.append(row)
    return rows


def render_agreement_heatmap_png_base64(runs: list[RunSummary], groups: _Groups) -> str | None:
    """Two annotated heatmaps (candidate Jaccard, verdict agreement) on one sequential ramp."""
    if len(runs) < 2:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap

    jac, agr, txt = _agreement_matrices(runs, groups)
    labels = [r.label for r in runs]
    n = len(runs)
    cmap = LinearSegmentedColormap.from_list("seq_blue", _SEQ)
    fig, axes = plt.subplots(1, 2, figsize=(max(4.2, 0.95 * n + 2.2) * 2, max(3.6, 0.85 * n + 1.6)))
    fig.subplots_adjust(wspace=0.12)
    for k, (ax, matrix, title, fmt) in enumerate(((axes[0], jac, "Candidate overlap (Jaccard)", "{:.2f}"),
                                                  (axes[1], agr, "Verdict agreement (fraction of shared candidates)", None))):
        data = [[(v if v is not None else float("nan")) for v in row] for row in matrix]
        im = ax.imshow(data, cmap=cmap, vmin=0, vmax=1)
        ax.set_xticks(range(n)); ax.set_yticks(range(n))
        ax.set_xticklabels(labels, rotation=35, ha="right", fontsize=7.5)
        # Row labels once, on the left panel only: the same runs index both matrices.
        ax.set_yticklabels(labels if k == 0 else [], fontsize=7.5)
        ax.set_title(title, fontsize=9.5)
        for i in range(n):
            for j in range(n):
                v = matrix[i][j]
                if i == j:
                    ax.text(j, i, "—", ha="center", va="center", fontsize=8, color="#888")
                    continue
                label = fmt.format(v) if fmt else txt[i][j]
                if v is None:
                    ax.text(j, i, "n/a", ha="center", va="center", fontsize=7.5, color="#888")
                    continue
                ax.text(j, i, label, ha="center", va="center", fontsize=7.5, color="#ffffff" if v > 0.55 else "#0b0b0b")
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_xticks([x - 0.5 for x in range(1, n)], minor=True); ax.set_yticks([y - 0.5 for y in range(1, n)], minor=True)
        ax.grid(which="minor", color="#ffffff", linewidth=2); ax.tick_params(which="minor", length=0)
    fig.colorbar(im, ax=axes, fraction=0.025, pad=0.02, label="0 = none, 1 = identical")
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _simulation_input_rows(runs: list[RunSummary]) -> list[list[str]]:
    rows = []
    for run in runs:
        for m in run.final.metabolites:
            for p in m.simulation_parameters:
                kex = f"{p.kex_per_s:g}" if p.kex_per_s is not None else "unknown"
                shift = f"{p.shift_ppm:g}" if p.shift_ppm is not None else "unknown"
                source = p.provenance
                if p.status == "simulated":
                    source = ("borrowed: " if p.borrowed_from_other_tissue else "") + (
                        f"PMID {', '.join(p.source_pmids)}" if p.source_pmids else "reference table")
                rows.append([run.label, m.name, p.group_text, p.group_class, p.status, kex, shift, source])
    return rows


def _organ_rows(runs: list[RunSummary]) -> list[list[str]]:
    rows = []
    for run in runs:
        if not run.plan:
            continue
        by_met: dict[str, list[str]] = defaultdict(list)
        for p in run.plan.organ_predictions:
            by_met[p.metabolite_name].append(f"{p.organ}: {p.organ_concentration_mM:g} mM → {p.predicted_contrast_percent:.2f}%")
        for met, cells in by_met.items():
            rows.append([run.label, met, "; ".join(cells)])
    return rows


# When set (--group-by-label), replicate pooling and the consensus table group runs by their
# label stem instead of the served model name, so the same model run on two kinds of hardware
# (e.g. "qwen3.8-27b L40S" vs "qwen3.8-27b H100") is compared as two groups.
GROUP_BY_LABEL = False


def _model_key(run: RunSummary) -> str:
    if GROUP_BY_LABEL:
        return re.sub(r" #\d+$", "", run.label)
    return run.models or re.sub(r" #\d+$", "", run.label)


def _candidate_keys(run: RunSummary, groups: _Groups) -> set[str]:
    return {groups.key(m.name) for m in run.final.metabolites}


def _detectable_map(run: RunSummary, groups: _Groups) -> dict[str, str]:
    return {groups.key(m.name): _status(m) for m in run.final.metabolites}


def _tissue_rows(runs: list[RunSummary]) -> tuple[list[list[str]], bool]:
    """One row per (run, tissue) at 3 T: T1, T2, MT fraction, MT k_ex, MT T2b, each tagged with its
    provenance letter (a = abstract-verified, t = paper table, d = generic default). Runs without a
    stored record get the rows recomputed from the current tissue table, and the footnote says so."""
    rows, recomputed = [], False
    order = ["water T1", "water T2", "MT fraction", "MT k_ex", "MT T2b"]
    for r in runs:
        stored = getattr(r.final, "tissue_parameters", None) or []
        if not stored:
            stored = tissue_parameter_rows(r.final.body_part, r.plan)
            recomputed = True
        by_key: dict[tuple, dict] = {}
        for t in stored:
            if abs(t.field_T - 3.0) < 1e-6:
                by_key.setdefault((t.tissue, t.role), {})[t.parameter] = t
        for (tissue, role), d in by_key.items():
            cells = []
            for pname in order:
                t = d.get(pname)
                cells.append(f"{t.value} ({t.verified[0]})" if t else "—")
            rows.append([r.label, tissue, role] + cells)
    return rows, recomputed


def _fmt_range(values: list[float], digits: int = 0) -> str:
    if not values:
        return "n/a"
    lo, hi = min(values), max(values)
    m = mean(values)
    if len(values) == 1:
        return f"{m:.{digits}f}"
    return f"{m:.{digits}f}; {lo:.{digits}f}-{hi:.{digits}f}"


def _model_summary_rows(runs: list[RunSummary], groups: _Groups) -> list[list[str]]:
    by_model: dict[str, list[RunSummary]] = defaultdict(list)
    for r in runs:
        by_model[_model_key(r)].append(r)
    rows = []
    for model, rs in by_model.items():
        kept = sum(r.n_citations_kept for r in rs)
        dropped = sum(r.n_citations_dropped for r in rs)
        drop_rate = f"{dropped / (kept + dropped):.0%} ({dropped}/{kept + dropped})" if kept + dropped else "n/a"
        quotes = sum(r.basis_counts.get("direct_literature_quote", 0) for r in rs)
        cands = sum(r.n_candidates for r in rs)
        structures = sum(r.n_structures_with_smiles for r in rs)
        wrong = sum(r.n_structures_replaced for r in rs)
        failures = sum(len(r.checks_failed) for r in rs)
        if len(rs) > 1:
            jaccards, agreements = [], []
            for a, b in combinations(rs, 2):
                ka, kb = _candidate_keys(a, groups), _candidate_keys(b, groups)
                union = ka | kb
                jaccards.append(len(ka & kb) / len(union) if union else 0.0)
                da, db = _detectable_map(a, groups), _detectable_map(b, groups)
                shared = set(da) & set(db)
                if shared:
                    agreements.append(sum(1 for k in shared if da[k] == db[k]) / len(shared))
            jacc = f"{mean(jaccards):.2f}"
            agree = f"{mean(agreements):.0%} of shared candidates" if agreements else "no shared candidates"
        else:
            jacc = agree = "n/a (1 run)"
        rows.append([
            model, str(len(rs)),
            _fmt_range([r.elapsed_min for r in rs if r.elapsed_min is not None]),
            _fmt_range([float(r.n_candidates) for r in rs], 1),
            drop_rate,
            f"{quotes}/{cands}" if cands else "n/a",
            f"{wrong}/{structures}" if structures else "n/a",
            str(failures),
            _fmt_range([float(r.n_detectable) for r in rs], 1),
            _fmt_range([float(r.n_unknown) for r in rs], 1),
            _fmt_range([float(r.n_robust) for r in rs], 1),
            jacc, agree,
            str(sum(1 for r in rs if r.undifferentiated_flag)),
        ])
    return rows


def _consensus_rows(runs: list[RunSummary], groups: _Groups) -> list[list[str]]:
    n_models = len({_model_key(r) for r in runs})
    per_key: dict[str, dict] = defaultdict(lambda: {"models": set(), "runs": 0, "det": 0, "unk": 0, "robust": 0, "best": [], "conc": []})
    for r in runs:
        seen_keys = set()
        for m in r.final.metabolites:
            k = groups.key(m.name)
            d = per_key[k]
            d["models"].add(_model_key(r))
            if k not in seen_keys:
                d["runs"] += 1
                seen_keys.add(k)
            # Count each RUN once per candidate row: a run listing two members of one pool
            # (chondroitin and keratan sulfate under GAG) is still one run.
            d.setdefault("_runs_det", set()); d.setdefault("_runs_unk", set()); d.setdefault("_runs_rob", set())
            if m.detectable_by_cest:
                d["_runs_det"].add(id(r))
            if _status(m) == "unknown":
                d["_runs_unk"].add(id(r))
            if m.detectability_robustness == "robust":
                d["_runs_rob"].add(id(r))
            d["det"], d["unk"], d["robust"] = len(d["_runs_det"]), len(d["_runs_unk"]), len(d["_runs_rob"])
            _, val = _best_field(m)
            d["best"].append(val)
            if m.typical_concentration_mM == m.typical_concentration_mM:  # not NaN
                d["conc"].append(m.typical_concentration_mM)
    rows = []
    for k, d in sorted(per_key.items(), key=lambda kv: (-len(kv[1]["models"]), -kv[1]["runs"], groups.name_for(kv[0]).lower())):
        conc = f"{min(d['conc']):g}-{max(d['conc']):g}" if len(d["conc"]) > 1 and min(d["conc"]) != max(d["conc"]) else (f"{d['conc'][0]:g}" if d["conc"] else "n/a")
        rows.append([groups.name_for(k), f"{len(d['models'])}/{n_models}", f"{d['runs']}/{len(runs)}",
                     f"{d['det']}/{d['runs']}", f"{d['unk']}/{d['runs']}", f"{d['robust']}/{d['runs']}", f"{median(d['best']):.2f}%", conc])
    return rows


def _fmt_min(x: float | None) -> str:
    return f"{x:.0f}" if x is not None else "n/a"


def build_tables(runs: list[RunSummary]) -> list[tuple[str, list[str], list[list[str]], str]]:
    """(title, headers, rows, footnote) for each comparison table."""
    groups = _Groups(runs)
    tables = []

    if any(r.n_clinical_studies or r.n_clinical_dropped for r in runs):
        tables.append((
            "Clinical evidence (potential clinical impact)",
            ["Run", "Verified studies", "CEST/NOE applications", "Metabolite-change studies", "Humans / animals",
             "Statistics completed from full text", "Pooled effects", "Dropped by verification"],
            [[r.label, str(r.n_clinical_studies), str(r.n_clinical_cest), str(r.n_clinical_change),
              f"{r.n_clinical_humans} / {r.n_clinical_animals}", str(r.n_clinical_fulltext), str(r.n_clinical_pooled),
              str(r.n_clinical_dropped)] for r in runs],
            "Studies the clinical-evidence agent found per run that survived verification (PMID, quote and every number "
            "checked against the abstract or the open-access full text). A pooled effect needs three verified studies "
            "with complete statistics for one pool, evidence type and disease category.",
        ))

    tables.append((
        "Cost and loop reliability",
        ["Run", "Model(s)", "Code", "Minutes", "LLM calls", "Prompt tok", "Completion tok",
         "Tool calls (errors)", "Nudges", "JSON repairs", "Output retries", "Completeness checks FAILED"],
        [[r.label, r.models or "n/a (no provenance)", r.code_version or "n/a", _fmt_min(r.elapsed_min), str(r.llm_calls),
          f"{r.prompt_tokens:,}", f"{r.completion_tokens:,}", f"{r.tool_calls} ({r.tool_errors})", str(r.nudges),
          str(r.json_repairs), str(r.output_retries), ", ".join(r.checks_failed) or "none"] for r in runs],
        "Nudges: times the coverage check had to send a model back to work after it stopped early. JSON repairs: "
        "structured outputs that failed schema validation. Output retries: schema-valid outputs rejected as incomplete. "
        "Runs without a provenance record predate this instrumentation.",
    ))

    tables.append((
        "Stage wall-clock minutes",
        ["Run", "literature", "chemist", "physicist", "experimentalist"],
        [[r.label] + [_fmt_min(r.stage_minutes.get(a)) for a in ("literature", "chemist", "physicist", "experimentalist")]
         for r in runs],
        "",
    ))

    tables.append((
        "Literature agent: candidates and how well they are sourced",
        ["Run", "Candidates", "Direct quote verified", "Estimated", "Other basis", "HMDB/BioNumbers grounded",
         "Citations kept", "Citations dropped (all agents)", "Quote flags", "Eligibility flags", "Detectable (final)",
         "Unknown (not simulated)", "Robust / fragile"],
        [[r.label, str(r.n_candidates), str(r.basis_counts.get("direct_literature_quote", 0)),
          str(r.basis_counts.get("estimated", 0)),
          str(sum(v for k, v in r.basis_counts.items() if k not in ("direct_literature_quote", "estimated"))),
          str(r.n_reference_grounded), str(r.n_citations_kept), str(r.n_citations_dropped),
          str(r.tag_counts.get("Quote verification", 0)), str(r.tag_counts.get("Eligibility", 0)),
          f"{r.n_detectable}/{r.n_candidates}", str(r.n_unknown), f"{r.n_robust} / {r.n_fragile}"] for r in runs],
        "'Citations dropped' counts PMIDs whose real PubMed title did not match the title the model claimed -- i.e. "
        "fabricated or mis-attributed references caught by verification. 'Quote flags' are concentration quotes the "
        "model claimed were verbatim but that were not found in the cited abstract. 'Eligibility flags' are "
        "spectroscopic category labels, macromolecules at whole-molecule molarity, or duplicates that survived the "
        "retry loop. 'Robust' detectable candidates still clear the threshold at the LOW corner of the literature "
        "concentration range x reference k_ex spread; 'fragile' ones clear it only at the point estimate.",
    ))

    tables.append((
        "Chemist, physicist and experimentalist checks",
        ["Run", "Structures proposed", "Wrong structure (replaced or rejected)", "Structure unverifiable", "Verdicts",
         "Field-strength coverage", "Coverage gaps", "ROI predictions (ROIs)",
         "Undifferentiated ROI conc.", "Compromise protocol", "Protocol arithmetic problems"],
        [[r.label, str(r.n_structures_with_smiles), str(r.n_structures_replaced), str(r.n_structures_unverifiable),
          str(r.n_verdicts), f"{r.coverage_fraction:.0%}", str(r.n_coverage_gaps),
          f"{r.n_organ_predictions} ({r.n_organs})", "YES" if r.undifferentiated_flag else "no", r.protocol or "n/a",
          "; ".join(r.protocol_problems) or ("none" if r.protocol else "n/a")]
         for r in runs],
        "'Field-strength coverage' is the share of (metabolite x field strength) pairs with a simulation result; since "
        "the deterministic stage took over, a gap means the metabolite had no exchangeable group with a verified "
        "reference exchange rate (reported as unknown), not a skipped tool call. 'Undifferentiated' means the experimentalist gave a "
        "metabolite the same concentration in every ROI, which defeats the ROI-differential scan design. "
        "'Protocol arithmetic problems' checks that the pulse train adds up to the stated saturation time, that "
        "saturation fits inside the TR, and that the stated duty cycle matches the pulse timing.",
    ))

    error_rows = [[r.label, e] for r in runs for e in r.tool_error_samples]
    if error_rows:
        tables.append((
            "Tool errors (up to five per stage): how each model misused a tool",
            ["Run", "Error"],
            error_rows,
            "Errors are fed back to the model, which usually corrects the call; they still show which models "
            "struggle with the tool schemas (e.g. a Bloch-McConnell call with a non-numeric field strength).",
        ))

    tables.append((
        "Candidate overlap: detectability, best contrast, concentration used",
        ["Metabolite", "Found in"] + [r.label for r in runs],
        _overlap_rows(runs, groups),
        "How the fields are chosen: no model decides what to test. Every proton group with a verified reference rate is "
        f"swept over B1 x Tsat at ALL of {', '.join(f'{f:g}' for f in FIELD_STRENGTHS_T)} T, and the metabolite is "
        f"'detectable' when its best group reaches >= {CONTRAST_THRESHOLD_PERCENT}% with-vs-without contrast at any of "
        "them. A cell's headline ('@ 5 T') is only the field with the highest contrast; the bracket lists the contrast "
        "at every field so a 'no @ 5 T' means below threshold everywhere, not untested elsewhere. 'unknown' means no "
        "group of that candidate had a verified reference rate, so nothing was simulated at any field. Because the "
        "simulator is shared, a different contrast for the same metabolite means the models fed it different "
        "concentrations, shifts or group classes.",
    ))

    model_rows = _model_summary_rows(runs, groups)
    if model_rows:
        tables.append((
            "Per-model summary across replicate runs",
            ["Model", "Runs", "Minutes (mean; range)", "Candidates (mean; range)", "Citation drop rate (pooled)",
             "Verified quotes (pooled)", "Wrong structures (pooled)", "Completeness failures (stage-runs)",
             "Detectable (mean)", "Unknown (mean)", "Robust (mean)", "Replicate candidate Jaccard (mean)",
             "Replicate detectability agreement", "Undifferentiated ROI conc. (runs)"],
            model_rows,
            "One row per model, pooling its replicate runs. 'Citation drop rate' is dropped / (kept + dropped) over "
            "all agents and runs. 'Replicate candidate Jaccard' is the overlap of candidate sets between two runs of "
            "the SAME model (1.0 = identical candidate lists), the yardstick against which between-model Jaccard "
            "values in the pairwise table should be read; 'n/a' with one run. A between-model difference smaller "
            "than the within-model spread is noise.",
        ))

    tables.append((
        "Cross-run consensus per candidate",
        ["Metabolite", "Models proposing", "Runs proposing", "Runs detectable", "Runs unknown", "Runs robust",
         "Median best contrast", "Concentration used (mM)"],
        _consensus_rows(runs, groups),
        "How many independent runs and models converge on each candidate, and on its detectability. Agreement "
        "across models and replicates is stronger evidence than any single run; a candidate one run alone "
        "proposes, or that is detectable in one of several replicates of the same model, should be read as unstable.",
    ))

    tables.append((
        "Pairwise agreement",
        ["Run"] + [r.label for r in runs],
        _agreement(runs, groups),
        "Jaccard: overlap of candidate sets. Verdict agreement: among metabolites both runs proposed, how many got the "
        "same verdict (detectable / not detectable / unknown -- 'unknown' agrees only with 'unknown').",
    ))

    tissue_rows, recomputed = _tissue_rows(runs)
    if tissue_rows:
        tables.append((
            "Tissue parameters used in each run's simulations (3 T)",
            ["Run", "Tissue", "Role", "water T1", "water T2", "MT fraction", "MT k_ex", "MT T2b"],
            tissue_rows,
            "Each value is tagged with its provenance: (a) abstract-verified, (t) taken from a paper's table and not "
            "abstract-verified, (e) estimated by power-law field scaling from another measured field, (d) the simulator's "
            "generic default because the tissue is not in the table."
            + (" Runs without a stored tissue-parameter record predate it; their rows were recomputed from the current "
               "tissue table." if recomputed else ""),
        ))

    sim_rows = _simulation_input_rows(runs)
    if sim_rows:
        tables.append((
            "Simulation inputs: proton groups, their reference k_ex/shift, and where each came from",
            ["Run", "Metabolite", "Group (chemist)", "Class", "Status", "k_ex (s^-1)", "Shift (ppm)", "Source"],
            sim_rows,
            "k_ex and shift come from the reference table keyed by the chemist's group class; the model chooses "
            "only the class. 'Borrowed' means no measurement in the requested tissue exists in the table and the "
            "provenance names the tissue/species/phantom it was taken from. A status other than 'simulated' means "
            "the pipeline said 'unknown' rather than inventing a value.",
        ))

    organ_rows = _organ_rows(runs)
    if organ_rows:
        tables.append((
            "Experimentalist: organ concentrations and predicted 3 T contrast under each run's compromise protocol",
            ["Run", "Metabolite", "Organ: concentration → predicted contrast"],
            organ_rows,
            "",
        ))
    return tables


def caveats(runs: list[RunSummary]) -> list[str]:
    notes = []
    versions = {r.code_version for r in runs if r.code_version}
    if len(versions) > 1 or any(not r.code_version for r in runs):
        notes.append("Not every run was made with the same cest_scout code version (or one predates version "
                     "recording), so part of any difference may be pipeline changes rather than the model.")
    for r in runs:
        if r.checks_failed:
            notes.append(f"{r.label}: the {', '.join(r.checks_failed)} stage(s) never satisfied their completeness "
                         "check; that run's coverage numbers are the honest, incomplete state.")
        if r.elapsed_min is None:
            notes.append(f"{r.label}: no provenance record, so cost columns are blank.")
    per_model = Counter(_model_key(r) for r in runs)
    if per_model and min(per_model.values()) >= 2:
        notes.append(f"{min(per_model.values())}-{max(per_model.values())} replicate runs per model. Local-model runs are "
                     "stochastic (sampling at the model's default temperature); read the between-model differences "
                     "against the within-model spread in the per-model summary (replicate Jaccard and detectability "
                     "agreement), and treat a candidate proposed in only one replicate as unstable.")
    else:
        notes.append("One run per model (at least for some models). Local-model runs are stochastic (sampling at the "
                     "model's default temperature), so single-run differences of a candidate or two are within "
                     "run-to-run noise; only consistent patterns (e.g. many dropped citations, failed completeness "
                     "checks) should be read as model traits. Run with REPLICATES=2 or more for a within-model spread.")
    versions = {r.code_version for r in runs if r.code_version}
    dirty = [r.label for r in runs if r.code_version.endswith("+dirty")]
    if dirty:
        notes.append(f"Runs made on uncommitted code (+dirty): {', '.join(dirty)}; their exact code state is not recoverable "
                     "from the commit hash alone.")
    return notes


def render_markdown(runs: list[RunSummary]) -> str:
    body_part = runs[0].final.body_part if runs and runs[0].final else ""
    lines = [f"# Model comparison: CEST_Scout on {body_part}", ""]
    lines.append("Runs compared: " + "; ".join(f"**{r.label}** (`{r.path.name}`)" for r in runs))
    lines.append("")
    for title, headers, rows, note in build_tables(runs):
        lines += [f"## {title}", "", "| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
        for row in rows:
            lines.append("| " + " | ".join(str(c).replace("|", "/") for c in row) + " |")
        lines.append("")
        if note:
            lines += [f"_{note}_", ""]
    lines += ["## Caveats", ""] + [f"- {c}" for c in caveats(runs)] + [""]
    return "\n".join(lines)


_STYLE = """<style>
body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; max-width: 1400px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; line-height: 1.45; }
h1 { border-bottom: 2px solid #333; padding-bottom: .3rem; }
h2 { margin-top: 2rem; border-bottom: 1px solid #ccc; padding-bottom: .2rem; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; margin: .5rem 0; }
td, th { border: 1px solid #ddd; padding: .3rem .5rem; text-align: left; vertical-align: top; }
th { background: #f3f3f3; }
td:first-child { font-weight: 600; white-space: nowrap; }
p.note { color: #444; font-size: .85rem; }
.yes { color: #0072B2; font-weight: 600; } .no { color: #D55E00; font-weight: 600; } .unknown { color: #E69F00; font-weight: 600; }
</style>"""


# ---- figures that replace the tables on the HTML page (tables stay one click away) ----------
# Okabe-Ito colour-blind-safe categorical palette, in fixed order per model.
_MODEL_COLORS = ("#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#000000")


def _png(fig) -> str:
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _models_in_order(runs: list[RunSummary]) -> list[str]:
    out = []
    for r in runs:
        k = _model_key(r)
        if k not in out:
            out.append(k)
    return out


def render_model_summary_png(runs: list[RunSummary], groups: "_Groups") -> str | None:
    """Small multiples, one panel per metric: bar = mean over the model's replicates, dots = each
    run. Replaces the cost/reliability and per-model summary tables."""
    plt = _plt()
    models = _models_in_order(runs)
    if not models:
        return None
    by_model = {m: [r for r in runs if _model_key(r) == m] for m in models}

    def frac(a, b):
        return 100.0 * a / b if b else float("nan")

    def agreement(rs):
        vals = []
        for a, b in combinations(rs, 2):
            da, db = _detectable_map(a, groups), _detectable_map(b, groups)
            sh = set(da) & set(db)
            if sh:
                vals.append(100.0 * sum(da[k] == db[k] for k in sh) / len(sh))
        return vals

    metrics = [
        ("Minutes per run", lambda r: r.elapsed_min if r.elapsed_min is not None else float("nan"), False),
        ("Candidates per run", lambda r: float(r.n_candidates), False),
        ("Citations dropped (%)\nfabricated / mis-attributed", lambda r: frac(r.n_citations_dropped, r.n_citations_kept + r.n_citations_dropped), False),
        ("Wrong structures (%)\nreplaced from PubChem", lambda r: frac(r.n_structures_replaced, r.n_structures_with_smiles), False),
        ("Detectable / unknown\ncandidates per run", None, False),
        ("Replicate verdict\nagreement (%)", None, True),
    ]
    fig, axes = plt.subplots(2, 3, figsize=(11.5, 6.2))
    x = list(range(len(models)))
    for ax, (title, fn, pairwise) in zip(axes.flat, metrics):
        for i, m in enumerate(models):
            col = _MODEL_COLORS[i % len(_MODEL_COLORS)]
            rs = by_model[m]
            if title.startswith("Detectable"):
                det = [float(r.n_detectable) for r in rs]; unk = [float(r.n_unknown) for r in rs]
                ax.bar(i - 0.18, mean(det), 0.34, color=col)
                ax.bar(i + 0.18, mean(unk), 0.34, color=col, alpha=0.35, hatch="//", edgecolor=col)
                ax.scatter([i - 0.18] * len(det), det, s=12, color="#333", zorder=3)
                ax.scatter([i + 0.18] * len(unk), unk, s=12, color="#333", zorder=3)
                continue
            vals = agreement(rs) if pairwise else [fn(r) for r in rs]
            vals = [v for v in vals if v == v]
            if not vals:
                ax.text(i, 0, "n/a", ha="center", va="bottom", fontsize=7, color="#666")
                continue
            ax.bar(i, mean(vals), 0.6, color=col)
            ax.scatter([i] * len(vals), vals, s=12, color="#333", zorder=3)
        ax.set_title(title, fontsize=9)
        ax.set_xticks(x)
        ax.set_xticklabels(models, rotation=25, ha="right", fontsize=7.5)
        ax.grid(axis="y", alpha=0.25)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    axes.flat[4].set_title("Detectable (solid) / unknown (hatched)\ncandidates per run", fontsize=9)
    fig.suptitle("Per-model summary: bars = mean over replicates, dots = individual runs", fontsize=10)
    fig.tight_layout()
    return _png(fig)


def _candidate_matrix(runs: list[RunSummary], groups: "_Groups", max_rows: int = 18):
    """(candidate keys in order, {(key, run index): (status, best contrast)})."""
    cells: dict[tuple, tuple] = {}
    count: Counter = Counter()
    for j, r in enumerate(runs):
        seen = set()
        for m in r.final.metabolites:
            k = groups.key(m.name)
            _, val = _best_field(m)
            prev = cells.get((k, j))
            if prev is None or val > prev[1]:
                cells[(k, j)] = (_status(m), val)
            if k not in seen:
                count[k] += 1; seen.add(k)
    keys = [k for k, _ in sorted(count.items(), key=lambda kv: (-kv[1], groups.name_for(kv[0]).lower()))][:max_rows]
    return keys, cells


def render_candidate_heatmap_png(runs: list[RunSummary], groups: "_Groups") -> str | None:
    """Candidate x run: colour = best simulated contrast (log scale), mark = verdict
    (✓ detectable, ✗ below threshold, ? unknown / not simulated), blank = not proposed.
    Replaces the candidate-overlap table."""
    plt = _plt()
    from matplotlib.colors import LinearSegmentedColormap, LogNorm
    keys, cells = _candidate_matrix(runs, groups)
    if not keys:
        return None
    import numpy as np
    data = np.full((len(keys), len(runs)), np.nan)
    for i, k in enumerate(keys):
        for j in range(len(runs)):
            c = cells.get((k, j))
            if c is not None and c[1] > 0:
                data[i, j] = max(c[1], 0.01)
    cmap = LinearSegmentedColormap.from_list("seq_blue", _SEQ)
    cmap.set_bad("#ffffff")
    fig, ax = plt.subplots(figsize=(max(6.0, 0.55 * len(runs) + 4.5), 0.42 * len(keys) + 2.2))
    im = ax.imshow(data, aspect="auto", cmap=cmap, norm=LogNorm(vmin=0.01, vmax=max(20.0, np.nanmax(data) if np.isfinite(np.nanmax(data)) else 20.0)))
    marks = {"detectable": "\u2713", "not detectable": "\u2717", "unknown": "?"}
    for i, k in enumerate(keys):
        for j in range(len(runs)):
            c = cells.get((k, j))
            if c is None:
                continue
            v = data[i, j]
            dark = np.isfinite(v) and v > 2.0
            ax.text(j, i, marks.get(c[0], "?"), ha="center", va="center", fontsize=9,
                    color="#ffffff" if dark else "#222", fontweight="bold" if c[0] == "detectable" else "normal")
    ax.set_yticks(range(len(keys)))
    ax.set_yticklabels([groups.name_for(k)[:42] for k in keys], fontsize=7.5)
    ax.set_xticks(range(len(runs)))
    ax.set_xticklabels([r.label for r in runs], rotation=40, ha="right", fontsize=7.5)
    # model separators
    prev = None
    for j, r in enumerate(runs):
        if prev is not None and _model_key(r) != prev:
            ax.axvline(j - 0.5, color="#333", lw=1.2)
        prev = _model_key(r)
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label("best with-vs-without contrast (%), log scale", fontsize=8)
    ax.set_title(f"Candidates per run: \u2713 detectable (\u2265 {CONTRAST_THRESHOLD_PERCENT}% at some field), "
                 "\u2717 below threshold, ? unknown (not simulated); blank = not proposed", fontsize=9)
    fig.tight_layout()
    return _png(fig)


def render_contrast_strip_png(runs: list[RunSummary], groups: "_Groups") -> str | None:
    """Best contrast per candidate, one dot per run coloured by model, with the detection
    threshold. Replaces the cross-run consensus table's contrast column."""
    plt = _plt()
    keys, cells = _candidate_matrix(runs, groups, max_rows=14)
    if not keys:
        return None
    models = _models_in_order(runs)
    fig, ax = plt.subplots(figsize=(9.5, 0.38 * len(keys) + 1.8))
    n = len(models)
    for mi, m in enumerate(models):
        col = _MODEL_COLORS[mi % len(_MODEL_COLORS)]
        xs, ys = [], []
        for i, k in enumerate(keys):
            for j, r in enumerate(runs):
                if _model_key(r) != m:
                    continue
                c = cells.get((k, j))
                if c is None or c[0] == "unknown":
                    continue
                xs.append(max(c[1], 0.005)); ys.append(i + (mi - (n - 1) / 2) * 0.18)
        ax.scatter(xs, ys, s=22, color=col, label=m, zorder=3, edgecolor="white", linewidth=0.5)
    for i, k in enumerate(keys):
        unk = sum(1 for j in range(len(runs)) if cells.get((k, j), (None,))[0] == "unknown")
        if unk:
            ax.text(0.0055, i, f"{unk}\u00d7 ?", fontsize=7, color="#E69F00", va="center")
    ax.axvline(CONTRAST_THRESHOLD_PERCENT, color="#D55E00", ls="--", lw=1)
    ax.text(CONTRAST_THRESHOLD_PERCENT, len(keys) - 0.4, f" threshold {CONTRAST_THRESHOLD_PERCENT}%", fontsize=7.5, color="#D55E00", va="top")
    ax.set_xscale("log")
    ax.set_xlim(0.005, None)
    ax.set_yticks(range(len(keys)))
    ax.set_yticklabels([groups.name_for(k)[:42] for k in keys], fontsize=7.5)
    ax.invert_yaxis()
    ax.set_xlabel("best simulated with-vs-without contrast over the five fields (%), log scale")
    ax.grid(axis="x", alpha=0.25)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=min(n, 4), frameon=False, fontsize=8)
    ax.set_title("Best contrast per candidate (one dot per run; '? ' = runs where it was unknown)", fontsize=9, pad=22)
    fig.tight_layout()
    return _png(fig)


# Which figure leads each table's section; the table itself is folded under a toggle.
_FIGURE_FOR_TABLE = {
    "Cost and loop reliability": "summary",
    "Candidate overlap: detectability, best contrast, concentration used": "heatmap",
    "Cross-run consensus per candidate": "strip",
    "Pairwise agreement": "agreement",
}


def render_html(runs: list[RunSummary]) -> str:
    esc = lambda s: _html.escape(str(s), quote=True)  # noqa: E731
    body_part = runs[0].final.body_part if runs and runs[0].final else ""
    parts = ["<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
             f"<title>CEST_Scout model comparison: {esc(body_part)}</title>", _STYLE, "</head><body>",
             f"<h1>Model comparison: CEST_Scout on {esc(body_part)}</h1>",
             "<p>Runs compared: " + "; ".join(f"<b>{esc(r.label)}</b> (<code>{esc(r.path.name)}</code>)" for r in runs) + "</p>"]
    groups = _Groups(runs)
    figures = {
        "summary": render_model_summary_png(runs, groups),
        "heatmap": render_candidate_heatmap_png(runs, groups),
        "strip": render_contrast_strip_png(runs, groups),
        "agreement": render_agreement_heatmap_png_base64(runs, groups),
    }
    for title, headers, rows, note in build_tables(runs):
        parts.append(f"<h2>{esc(title)}</h2>")
        fig = figures.get(_FIGURE_FOR_TABLE.get(title, ""))
        if fig:
            parts.append(f"<p><img alt='{esc(title)}' src='data:image/png;base64,{fig}' style='max-width:100%;'></p>")
            if note:
                parts.append(f"<p class='note'>{esc(note)}</p>")
            parts.append("<details><summary>Show table</summary>")
        parts.append("<table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>")
        for row in rows:
            cells = []
            for c in row:
                raw = str(c)
                c = esc(c)
                c = re.sub(r"^YES\b", "<span class='yes'>&#10003; YES</span>", c)
                c = re.sub(r"^no\b", "<span class='no'>&#10007; no</span>", c)
                c = re.sub(r"^unknown\b", "<span class='unknown'>? unknown</span>", c)
                c = _units_html(c)
                style = ""
                if title == "Pairwise agreement":
                    m = re.match(r"Jaccard ([0-9.]+)", raw)
                    if m:
                        bg, fg = _seq_color(float(m.group(1)))
                        style = f" style='background:{bg};color:{fg};'"
                cells.append(f"<td{style}>{c}</td>")
            parts.append("<tr>" + "".join(cells) + "</tr>")
        parts.append("</table>")
        if fig:
            parts.append("</details>")
        elif note:
            parts.append(f"<p class='note'>{esc(note)}</p>")
    parts.append("<h2>Caveats</h2><ul>" + "".join(f"<li>{esc(c)}</li>" for c in caveats(runs)) + "</ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def _dedupe_labels(runs: list[RunSummary]) -> None:
    """Replicate runs of one model get ' #1', ' #2', ... so their columns can be told apart."""
    counts = Counter(r.label for r in runs)
    seen: Counter = Counter()
    for r in runs:
        if counts[r.label] > 1:
            seen[r.label] += 1
            r.label = f"{r.label} #{seen[r.label]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare CEST_Scout runs of one tissue across models.")
    ap.add_argument("runs", nargs="+", help="Run JSON files written by cest_scout.cli")
    ap.add_argument("--labels", default="", help="Comma-separated labels, one per run (default: model name from provenance)")
    ap.add_argument("--out", default="", help="Output path without extension; writes <out>.md and <out>.html")
    ap.add_argument("--group-by-label", action="store_true",
                    help="Pool replicates by label stem (text before ' #') instead of by served model name, "
                         "e.g. to compare the same model on two kinds of GPU.")
    args = ap.parse_args(argv)
    global GROUP_BY_LABEL
    GROUP_BY_LABEL = bool(args.group_by_label)

    labels = [x.strip() for x in args.labels.split(",")] if args.labels else []
    if labels and len(labels) != len(args.runs):
        print("--labels must have one entry per run", file=sys.stderr)
        return 2
    runs = [load_run(Path(p), labels[i] if labels else None) for i, p in enumerate(args.runs)]
    _dedupe_labels(runs)
    parts = {r.final.body_part.strip().lower() for r in runs}
    if len(parts) > 1:
        print(f"warning: runs are for different tissues: {sorted(parts)}", file=sys.stderr)

    md = render_markdown(runs)
    print(md)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.with_suffix(".md").write_text(md)
        out.with_suffix(".html").write_text(render_html(runs))
        print(f"\n(Written to {out}.md and {out}.html)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
