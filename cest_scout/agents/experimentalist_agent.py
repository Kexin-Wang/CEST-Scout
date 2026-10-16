"""Agent 4: the experimentalist. Takes Agent 3 (physicist)'s computationally-detectable
candidate metabolites and designs a real CEST scan to verify them in vivo -- for whatever tissue
this report is about.

The experimental idea: one slice or slab, prescribed so that it cuts through the target tissue,
also contains other tissues in the same field of view. A single saturation scheme (B1/Tsat/pulse
train) swept across a shared set of offsets is applied to all of them. Because different tissues
carry different, literature-known concentrations of a candidate metabolite, comparing the same
offset's CEST contrast ACROSS ROIs -- rather than trusting one ROI's Z-spectrum in isolation --
argues that an observed effect is that metabolite's own exchange contrast (which should track the
concentration differences) and not a generic background MT/NOE effect (roughly uniform across
tissues at a given offset). The abdominal axial scan (liver, kidney, spleen, paraspinal muscle) is
one instance of this; a sagittal lumbar scan (nucleus pulposus, annulus fibrosus, vertebral marrow,
paraspinal muscle) is another. The agent chooses the plane, the slice and the ROI set for the
tissue at hand; the pipeline then simulates every (ROI, metabolite) pair with that ROI's tabulated
water T1/T2/MT parameters and checks deterministically that the target tissue is among the ROIs.
"""
from __future__ import annotations

import json
import re

from openai import OpenAI

from cest_scout.config import CONTRAST_THRESHOLD_PERCENT
from cest_scout.llm import ToolCallback, run_research_agent
from cest_scout.schemas import ChemistReport, InVivoVerificationPlan, LiteratureReport, PhysicistReport
from cest_scout.skills import pubmed, tissue_reference

# Clinical CEST is done at 1.5 T or 3 T in practice (7 T body/spine imaging fights B0/B1
# inhomogeneity and tighter SAR limits); 3 T is the higher-SNR, widely available standard, so it is
# fixed rather than re-derived per run -- overwritten deterministically after the model responds.
SCAN_FIELD_STRENGTH_T = 3.0

# How many of the physicist's ranked candidates to actively design the scan around, and how many
# ROIs a plan may have. Both bound the PubMed budget (one search per ROI x metabolite pair).
MAX_CANDIDATES = 6
MIN_ROIS, MAX_ROIS = 2, 5

SYSTEM_PROMPT = f"""\
You are an in vivo MRI experimentalist (radiologist/MR physicist) who is not satisfied with a \
purely computational case for a candidate CEST-detectable metabolite -- you want to design a \
REAL scan that could confirm or refute it. You've been given metabolites that a prior physicist \
agent already simulated as plausibly detectable via a multi-pool Bloch-McConnell model, for one \
particular target tissue, which is named in the prompt.

Your experimental strategy -- ROI-DIFFERENTIAL CEST: prescribe ONE slice or slab that CUTS THROUGH \
THE TARGET TISSUE and also contains other tissues in the same field of view. One saturation scheme \
is applied to all of them. A candidate metabolite's true concentration differs across these \
tissues (grounded in literature, not guessed) -- so if a Z-spectrum offset shows CONTRAST THAT \
DIFFERS ACROSS ROIs in a way that tracks those known concentration differences, that is evidence \
the effect is really this metabolite's exchange contrast, not a generic background MT/NOE effect \
(which looks roughly similar across tissues at a given offset). This mirrors the physicist agent's \
with-vs-without simulation logic, but as something you can actually go measure.

DESIGN IT FOR THIS TISSUE. Choose the imaging plane (imaging_plane) and slice/slab prescription \
(slice_coverage) that best cut into the target tissue -- e.g. a sagittal midline slab through the \
lumbar spine for an intervertebral disc, an axial slab at the portal-vein level for the liver, a \
coronal slab for the kidneys, an axial slice through the basal ganglia for the brain. Then define \
{MIN_ROIS}-{MAX_ROIS} ROIs in roi_set, each with a short standard anatomical name (used verbatim as \
organ_predictions[*].organ), a precise anatomical_definition of where it is drawn on that slice, \
is_target_tissue, and its role:
- the TARGET TISSUE itself is mandatory (is_target_tissue=true) -- if it has sub-regions with \
  different composition (a disc's nucleus pulposus vs. annulus fibrosus; renal cortex vs. \
  medulla; gray vs. white matter) and the slice resolves them, make them separate ROIs;
- 1-4 COMPARISON tissues that the same slice contains and whose concentration of the candidate \
  metabolites is known to differ (vertebral body marrow, spinal cord, cerebrospinal fluid and \
  paraspinal muscle around a disc; liver, kidney, spleen and muscle in an abdominal slab; ...). \
  Prefer tissues whose water T1/T2 and MT parameters are in the pipeline's tissue table (listed \
  in the prompt), because the simulation of an ROI that is not in the table falls back to generic \
  values and says so.
A plan whose ROIs do not include the target tissue is sent back to you.

Field strength: fix everything to {SCAN_FIELD_STRENGTH_T:g} T (the clinical standard for body and \
spine imaging; the field-dependent recommendations for other fields are the physicist's).

For the whole scan (ONE compromise protocol, not one per metabolite -- a real Z-spectrum \
acquisition uses a single saturation scheme swept across many offset frequencies):

1. scan_protocol: choose ONE saturation B1 (uT), pulse shape/train (pulsed, not continuous wave -- \
   SAR limits at 3 T essentially require a duty-cycled pulse train), number of pulses, pulse \
   duration, interpulse delay, duty cycle, total saturation time, and TR. The metabolites you were \
   given may have different individually-optimal B1 values from the physicist agent -- pick a \
   practical single compromise and explain explicitly in compromise_rationale which metabolites it \
   favors and which it under/oversaturates. Justify SAR feasibility for THIS anatomy in \
   sar_rationale. The numbers must add up: n_pulses x pulse_duration_ms plus the interpulse delays \
   equals total_saturation_time_ms, saturation plus readout fits inside tr_ms, and \
   duty_cycle_percent = pulse_duration_ms / (pulse_duration_ms + interpulse_delay_ms). b1_uT is the \
   mean B1 during each pulse. cest_scout simulates the train EXACTLY as you write it -- n_pulses \
   rectangular pulses of pulse_duration_ms at b1_uT with interpulse_delay_ms gaps, a single-shot \
   readout, and the periodic steady state at your tr_ms -- so a short TR costs recovered \
   magnetization and a low duty cycle costs saturation, and both show up in the predicted contrast; \
   every pair is also re-simulated at 0.7x and 1.3x your B1 to show how much of the prediction \
   survives the transmit-field inhomogeneity of a body coil.
2. zspectrum_offsets: list every offset (ppm) you need to sample -- each candidate metabolite's \
   shift (and its mirror negative offset, needed for MTRasym-style analysis), any rNOE pool's \
   own negative offset (e.g. -3.5 ppm, or about -1 ppm for glycogen), 0 ppm (water \
   itself), a few offsets outside all pools (e.g. +/-8 ppm) as far-off baseline reference, and \
   note the need for a separate low-power WASSR-style reference scan for B0 inhomogeneity \
   correction where the anatomy calls for it.
3. fat_suppression: name a concrete method (e.g. SPIR/SPAIR prepulse, water-selective excitation) \
   and say why THIS anatomy needs it (vertebral marrow fat next to a disc, visceral fat in the \
   abdomen, ...) or why it does not.
4. motion_mitigation: appropriate to the anatomy -- respiratory triggering / breath-holds for the \
   abdomen; CSF-pulsation and swallowing considerations for the spine; cardiac gating for the \
   heart -- and the practical constraint this puts on scan time.
5. slice_coverage: the prescription that captures every ROI in roi_set, stated so a technologist \
   could set it up (plane, centre landmark, thickness/number of slices).
6. For EACH candidate metabolite x EACH ROI in your roi_set (that is one PubMed search per pair, \
   not one search reused for every ROI): search PubMed (search_pubmed) for THAT tissue's typical \
   concentration of the metabolite -- do NOT reuse the concentration Agent 1 reported for the \
   target tissue for the comparison ROIs, and do NOT reuse one ROI's concentration for the others. \
   Concentrations genuinely differ for real metabolites (creatine is far higher in muscle than in \
   liver; glycosaminoglycans are far higher in a disc's nucleus than in muscle) -- that difference \
   is the entire point of this design, so identical concentration_mM across ROIs for the same \
   metabolite means you have not done this step and will be flagged as providing no \
   discriminating evidence. Write one organ_predictions entry per (ROI, metabolite) pair, copying \
   the ROI name verbatim from roi_set and the metabolite name verbatim; a missing pair is sent back \
   to you. You do NOT run simulations and you do NOT fill in predicted_contrast_percent: cest_scout \
   simulates every pair itself, with that ROI's tabulated water T1/T2 and MT parameters, the \
   metabolite's reference-table exchange rate and shift, YOUR organ_concentration_mM, and YOUR \
   scan_protocol's B1 and total saturation time -- so the concentration and protocol you write are \
   exactly what gets simulated. Record the citation(s) for that ROI's concentration in that entry's \
   citations -- every PMID is checked against the real PubMed record and dropped if it doesn't \
   match, so never attach a citation you didn't actually read. When you can't find tissue-specific \
   literature for a pair (common, and completely acceptable), leave citations empty and SHOW YOUR \
   WORK in concentration_rationale: state plainly that no direct value was found, then give the \
   explicit reasoning/calculation you used instead. "I don't know, here is my reasoning and its \
   uncertainty" is a valid answer; a confident number with no source and no derivation is not.
7. differential_analysis_plan: the concrete post-scan analysis -- how ROI-averaged Z-spectra (or \
   MTRasym / Lorentzian-difference curves) from each ROI are compared at each metabolite's offset, \
   and what pattern of ROI-to-ROI difference would count as confirming that metabolite's \
   contribution vs. being inconclusive.
8. excluded_metabolites_note: if you were given metabolites you did NOT actively design around \
   (told explicitly in the prompt), say why.

Keep every free-text field under 150 words, in short sentences. \
Be honest in caveats about this approach's real limits for THIS anatomy: ROI partial-volume \
effects (a thin disc, a small structure), B0/B1 inhomogeneity across ROIs even after correction, \
susceptibility from bone or bowel gas, and that one compromise B1/Tsat cannot be simultaneously \
optimal for every candidate metabolite's exchange regime.
"""


def _max_contrast(verdict) -> float:
    return max(verdict.simulated_contrast_percent.values(), default=0.0)


def run(
    client: OpenAI,
    model: str,
    literature_report: LiteratureReport,
    chemist_report: ChemistReport,
    physicist_report: PhysicistReport,
    on_tool_call: ToolCallback | None = None,
    extra_body: dict | None = None,
    stats_sink: dict | None = None,
) -> InVivoVerificationPlan:
    lit_by_name = {m.name: m for m in literature_report.metabolites}
    chem_by_name = {f.metabolite_name: f for f in chemist_report.findings}

    ranked = sorted(physicist_report.verdicts, key=_max_contrast, reverse=True)
    above_threshold = [v for v in ranked if _max_contrast(v) >= CONTRAST_THRESHOLD_PERCENT]
    chosen = (above_threshold or ranked)[:MAX_CANDIDATES]
    chosen_names = {v.metabolite_name for v in chosen}
    excluded_names = [v.metabolite_name for v in ranked if v.metabolite_name not in chosen_names]

    candidates_payload = []
    for verdict in chosen:
        lit = lit_by_name.get(verdict.metabolite_name)
        chem = chem_by_name.get(verdict.metabolite_name)
        candidates_payload.append({
            "name": verdict.metabolite_name,
            "target_tissue_concentration_mM": lit.typical_concentration_mM if lit else None,
            "chemist_exchangeable_protons": [ep.model_dump(exclude={"frequency_table"}) for ep in chem.exchangeable_protons] if chem else [],
            "physicist_simulated_contrast_percent_by_field": verdict.simulated_contrast_percent,
            "physicist_swept_saturation_by_field": [
                {"field_strength_T": s.field_strength_T, "b1_uT": s.b1_uT, "saturation_time_ms": s.saturation_time_ms}
                for s in verdict.sequence_recommendations
            ],
            "physicist_final_confidence": verdict.final_confidence,
        })

    known = tissue_reference.known_tissues()
    user_prompt = (
        f"TARGET TISSUE (the body part Agents 1-3 researched; your scan must cut through it and include it as an "
        f"ROI): {literature_report.body_part}\n\n"
        "Candidate metabolites to design the in vivo verification scan around (ranked by the physicist agent's "
        "simulated with-vs-without contrast):\n"
        f"{json.dumps(candidates_payload, indent=2)}\n\n"
        + (
            "Other candidates from this tissue that you are NOT actively targeting (low/no simulated contrast at "
            f"any field strength, or beyond this scan's budget): {excluded_names}\n\n"
            if excluded_names else ""
        )
        + ("Tissues with tabulated water T1/T2 (and possibly MT) parameters available to the simulation -- "
           f"prefer these as comparison ROIs when they are in the same field of view: {known}\n\n" if known else "")
        + "Produce ONE InVivoVerificationPlan covering all the candidates above."
    )

    report = run_research_agent(
        client=client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        skills=[pubmed],
        schema_model=InVivoVerificationPlan,
        # One PubMed search per (ROI, metabolite) pair plus slack; the simulations are the
        # pipeline's job, not tool calls.
        max_tool_iters=max(20, MAX_ROIS * len(chosen) + 8),
        on_tool_call=on_tool_call,
        extra_body=extra_body,
        output_check=_make_output_check([v.metabolite_name for v in chosen], literature_report.body_part),
        stats_sink=stats_sink,
    )
    report.scan_protocol.field_strength_T = SCAN_FIELD_STRENGTH_T
    if excluded_names and not report.excluded_metabolites_note:
        report.excluded_metabolites_note = (
            f"Not actively targeted (low/no simulated contrast at any field strength, or beyond "
            f"this scan's per-run budget): {', '.join(excluded_names)}."
        )
    _verify_citations(report)
    _flag_undifferentiated_concentrations(report)
    _flag_protocol_problems(report)
    _flag_target_tissue_missing(report, literature_report.body_part)
    return report


def protocol_problems(sp) -> list[str]:
    """Arithmetic the model is responsible for and routinely gets wrong: the pulse train must add
    up to the stated saturation time, saturation must fit inside the TR, and the stated duty cycle
    must match the pulse timing. Every model tried so far produced at least one of these."""
    problems = []
    if sp.tr_ms and sp.total_saturation_time_ms > sp.tr_ms:
        problems.append(f"total_saturation_time_ms {sp.total_saturation_time_ms:g} exceeds tr_ms {sp.tr_ms:g}")
    if sp.n_pulses and sp.pulse_duration_ms:
        train = sp.n_pulses * sp.pulse_duration_ms + max(sp.n_pulses - 1, 0) * sp.interpulse_delay_ms
        if sp.total_saturation_time_ms and abs(train - sp.total_saturation_time_ms) > 0.1 * sp.total_saturation_time_ms:
            problems.append(
                f"n_pulses x pulse_duration_ms (+ interpulse delays) sums to {train:g} ms, but "
                f"total_saturation_time_ms is {sp.total_saturation_time_ms:g}"
            )
    if sp.n_pulses > 1 and sp.pulse_duration_ms and sp.interpulse_delay_ms >= 0:
        duty = 100 * sp.pulse_duration_ms / (sp.pulse_duration_ms + sp.interpulse_delay_ms)
        if sp.duty_cycle_percent and abs(duty - sp.duty_cycle_percent) > 10:
            problems.append(f"duty_cycle_percent {sp.duty_cycle_percent:g} vs {duty:.0f}% implied by the pulse timing")
    return problems


def _check_output(report: InVivoVerificationPlan) -> str | None:
    problems = protocol_problems(report.scan_protocol)
    if not problems:
        return None
    return (
        "scan_protocol is not self-consistent: " + "; ".join(problems) + ". Fix the numbers so that "
        "n_pulses x pulse_duration_ms plus the interpulse delays equals total_saturation_time_ms, "
        "total_saturation_time_ms plus readout fits inside tr_ms, and duty_cycle_percent = "
        "pulse_duration_ms / (pulse_duration_ms + interpulse_delay_ms). Re-emit the whole JSON."
    )


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= 4}


def target_tissue_rois(report: InVivoVerificationPlan, body_part: str) -> list[str]:
    """ROIs that count as cutting into the target tissue: flagged is_target_tissue AND named or
    defined with a word of the body part ("disc" in "nucleus pulposus of the L4/L5 disc"), so a
    plan cannot satisfy the requirement by flagging an unrelated ROI."""
    words = _tokens(body_part)
    out = []
    for roi in report.roi_set:
        if not roi.is_target_tissue:
            continue
        if not words or words & _tokens(f"{roi.name} {roi.anatomical_definition} {roi.role}"):
            out.append(roi.name)
    return out


def _missing_roi_pairs(report: InVivoVerificationPlan, metabolite_names: list[str]) -> list[str]:
    have = {(p.organ.strip().lower(), p.metabolite_name.strip().lower()) for p in report.organ_predictions}
    return [f"{r.name}::{m}" for m in metabolite_names for r in report.roi_set
            if (r.name.strip().lower(), m.strip().lower()) not in have]


def _make_output_check(metabolite_names: list[str], body_part: str):
    """The plan must define a usable ROI set that includes the target tissue, give every (ROI,
    metabolite) pair its own organ_predictions entry -- that is what the pipeline simulates -- and
    its protocol has to add up."""
    def check(report: InVivoVerificationPlan) -> str | None:
        complaints = []
        n = len(report.roi_set)
        if n < MIN_ROIS or n > MAX_ROIS:
            complaints.append(f"roi_set has {n} ROIs; define {MIN_ROIS}-{MAX_ROIS}: the target tissue itself plus "
                              "comparison tissues in the same field of view.")
        elif not target_tissue_rois(report, body_part):
            complaints.append(
                f"No ROI in roi_set cuts into the target tissue '{body_part}'. Add an ROI inside it (is_target_tissue "
                "= true, named/defined as part of that tissue) and prescribe the slice so it contains it -- the "
                "verification scan is worthless if it does not sample the tissue the report is about."
            )
        missing = _missing_roi_pairs(report, metabolite_names)
        if missing:
            complaints.append(
                "organ_predictions is missing these (ROI, metabolite) pairs, which therefore cannot be simulated: "
                f"{', '.join(missing)}. Add one entry per pair with that tissue's concentration (cite it, or show "
                "the reasoning if no value was found)."
            )
        stray = sorted({p.organ for p in report.organ_predictions} - {r.name for r in report.roi_set})
        if stray:
            complaints.append(f"organ_predictions uses ROI names not in roi_set: {stray}. Use the roi_set names verbatim.")
        protocol = _check_output(report)
        if protocol:
            complaints.append(protocol)
        return "\n".join(complaints) if complaints else None
    return check


def _flag_target_tissue_missing(report: InVivoVerificationPlan, body_part: str) -> None:
    if not target_tissue_rois(report, body_part):
        report.caveats = (
            f"{report.caveats} [Target tissue not sampled] None of the ROIs ({', '.join(r.name for r in report.roi_set) or 'none defined'}) "
            f"is inside the target tissue '{body_part}' -- this survived the retry loop, so the plan does not actually "
            "verify anything about that tissue."
        ).strip()


def _flag_protocol_problems(report: InVivoVerificationPlan) -> None:
    problems = protocol_problems(report.scan_protocol)
    if problems:
        report.caveats = (
            f"{report.caveats} [Protocol arithmetic] The stated scan_protocol is not self-consistent "
            f"and survived the retry loop: {'; '.join(problems)}. Treat its timing numbers as unreliable."
        ).strip()


def _flag_undifferentiated_concentrations(report: InVivoVerificationPlan) -> None:
    """The whole point of this design is that a metabolite's concentration -- and therefore its
    predicted contrast -- should differ across the ROI set (that is what a real ROI-differential
    Z-spectrum comparison would detect). A model can satisfy the schema by reusing the target
    tissue's concentration for every ROI instead of researching each one, which silently defeats
    the premise while still looking like a complete plan. Flag it deterministically."""
    by_metabolite: dict[str, list] = {}
    for pred in report.organ_predictions:
        by_metabolite.setdefault(pred.metabolite_name, []).append(pred)

    flagged = []
    for name, preds in by_metabolite.items():
        if len(preds) > 1 and len({p.organ_concentration_mM for p in preds}) == 1:
            flagged.append(name)

    if flagged:
        note = (
            f"{', '.join(flagged)}: organ_concentration_mM was identical across every ROI, so "
            "predicted_contrast_percent is also identical across ROIs -- this metabolite's ROI-differential "
            "comparison provides no discriminating evidence yet and needs tissue-specific concentration "
            "literature before it can support the differential_analysis_plan's logic."
        )
        report.caveats = f"{report.caveats} [Undifferentiated ROI concentrations] {note}".strip()


def _verify_citations(report: InVivoVerificationPlan) -> None:
    """Same rationale as literature_agent/physicist_agent's citation verification: confirm every
    concentration citation against PubMed's actual title rather than trusting the model's
    transcription, and drop any that fail."""
    pairs = [(c.pmid, c.title) for pred in report.organ_predictions for c in pred.citations]
    if not pairs:
        return
    try:
        verdicts_map = pubmed.verify_titles(pairs)
    except Exception as exc:  # network unavailable -- don't fail the whole run over this
        report.caveats = f"{report.caveats} [Citation verification skipped: {exc}]".strip()
        return

    notes = []
    for pred in report.organ_predictions:
        pred.citations, dropped = pubmed.drop_unverified_citations(pred.citations, verdicts_map)
        note = pubmed.format_drop_notes(dropped, f"{pred.organ}/{pred.metabolite_name} citations")
        if note:
            notes.append(note)
    if notes:
        report.caveats = f"{report.caveats} [Citation verification] {' '.join(notes)}".strip()
