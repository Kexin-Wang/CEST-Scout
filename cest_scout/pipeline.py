"""Orchestrates the four-agent CEST_Scout pipeline for a given body part."""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from cest_scout.agents import chemist_agent, clinical_agent, experimentalist_agent, literature_agent, physicist_agent
from cest_scout.config import CONTRAST_THRESHOLD_PERCENT, FIELD_STRENGTHS_T, PROJECT_ROOT, Settings, load_settings
from cest_scout.llm import ToolCallback, get_client, new_run_stats
from cest_scout.report.prose import bullets, is_long
from cest_scout.skills import bloch_bridge, exchange_reference, tissue_reference
from cest_scout.schemas import (
    AgentRunStats,
    ChemistReport,
    ClinicalReport,
    FinalMetabolite,
    FinalReport,
    InVivoVerificationPlan,
    LiteratureReport,
    PhysicistReport,
    RunProvenance,
    SequenceRecommendation,
    SimulationParameters,
    TissueParameterRow,
    ZSpectrumCurve,
)


def _endpoint_of(client) -> str:
    """Host:port of the client's endpoint -- enough to tell two local servers apart in a report,
    without ever writing the API key anywhere."""
    url = str(getattr(client, "base_url", "") or "")
    return urlparse(url).netloc or url


def _code_version() -> str:
    try:
        rev = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        # Only cest_scout/ (code and reference data) can change a run's numbers; an uncommitted
        # README edit during a batch once stamped seven runs "+dirty" with nothing different.
        dirty = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain", "--untracked-files=no", "--", "cest_scout"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        return f"{rev}+dirty" if dirty else rev
    except Exception:
        return ""


def _agent_stats(agent: str, model: str, client, extra_body: dict | None, counters: dict) -> AgentRunStats:
    thinking_disabled = bool(
        extra_body and any(v is False for v in extra_body.get("chat_template_kwargs", {}).values())
    )
    return AgentRunStats(
        agent=agent, model=model, endpoint=_endpoint_of(client), thinking_disabled=thinking_disabled,
        **{k: v for k, v in counters.items() if k in AgentRunStats.model_fields},
    )


def run_pipeline(
    body_part: str,
    settings: Settings | None = None,
    on_tool_call: ToolCallback | None = None,
    on_stage: "callable | None" = None,
) -> tuple[LiteratureReport, ChemistReport, PhysicistReport, InVivoVerificationPlan, ClinicalReport, FinalReport]:
    settings = settings or load_settings()
    started_at = datetime.now(timezone.utc)
    started_clock = time.monotonic()
    # Read at the start: the code that actually ran is what was on disk when the modules were
    # imported, not whatever the working tree looks like an hour later when the run finishes.
    code_version = _code_version()
    client = get_client(settings.openai_api_key, settings.openai_base_url)
    kw = getattr(settings, "thinking_kwarg", "enable_thinking")
    extra_body = {"chat_template_kwargs": {kw: False}} if settings.disable_thinking else None

    # The physicist and experimentalist prompts carry every metabolite's simulation summary plus
    # their own PubMed research, so they need more context than the literature/chemist stages. If
    # PHYSICIST_OPENAI_API_KEY is set, route those two stages through that separate
    # client/model -- literature and chemist keep using `client` above.
    if settings.physicist_api_key:
        phys_client = get_client(settings.physicist_api_key, settings.physicist_base_url)
        phys_extra_body = (
            {"chat_template_kwargs": {kw: False}} if settings.physicist_disable_thinking else None
        )
    else:
        phys_client = client
        phys_extra_body = extra_body

    lit_stats, chem_stats, phys_stats, exp_stats, clin_stats = (new_run_stats(), new_run_stats(), new_run_stats(),
                                                               new_run_stats(), new_run_stats())

    if on_stage:
        on_stage("literature")
    lit_report = literature_agent.run(
        client, settings.model_literature, body_part, on_tool_call, extra_body=extra_body, stats_sink=lit_stats,
    )

    if on_stage:
        on_stage("chemist")
    chem_report = chemist_agent.run(
        client, settings.model_chemist, lit_report, on_tool_call, extra_body=extra_body, stats_sink=chem_stats,
    )

    # Every simulation input is fixed here, before any reviewer sees it: reference-table k_ex and
    # shift per proton class, the literature concentration, a B1 x Tsat sweep per field strength.
    if on_stage:
        on_stage("simulation")
    sims = run_reference_simulations(lit_report, chem_report, body_part)

    if on_stage:
        on_stage("physicist")
    phys_report = physicist_agent.run(
        phys_client, settings.model_physicist, lit_report, chem_report,
        simulation_summary(sims) + [{"name": "(tissue parameters)", "note": tissue_parameters_note(body_part)}],
        on_tool_call, extra_body=phys_extra_body, stats_sink=phys_stats,
    )
    _apply_reference_simulations(phys_report, sims)

    if on_stage:
        on_stage("experimentalist")
    # Reuses the physicist's endpoint (phys_client/phys_extra_body): both "heavy" agents share the
    # larger-context endpoint so there is no third override knob.
    experimentalist_plan = experimentalist_agent.run(
        phys_client, settings.model_experimentalist, lit_report, chem_report, phys_report,
        on_tool_call, extra_body=phys_extra_body, stats_sink=exp_stats,
    )
    _simulate_organ_predictions(experimentalist_plan, sims, experimentalist_agent.SCAN_FIELD_STRENGTH_T)

    # Potential clinical impact: the diseases each pool's CEST/NOE signal has been applied to,
    # with verified counts and, where the abstracts allow, a pooled group difference.
    if on_stage:
        on_stage("clinical")
    clinical_report = clinical_agent.run(
        client, settings.model_literature, lit_report, phys_report, on_tool_call, extra_body=extra_body,
        stats_sink=clin_stats, fulltext=getattr(settings, "clinical_fulltext", True),
    )

    final_report = _merge(lit_report, chem_report, phys_report, sims, experimentalist_plan, clinical_report)
    final_report.tissue_parameters_note = tissue_parameters_note(body_part)
    final_report.tissue_parameters = tissue_parameter_rows(body_part, experimentalist_plan)
    final_report.provenance = RunProvenance(
        started_at_utc=started_at.isoformat(timespec="seconds"),
        finished_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        elapsed_s=round(time.monotonic() - started_clock, 1),
        code_version=code_version,
        reference_table=exchange_reference.table_version(),
        settings=pipeline_settings(),
        stages=[
            _agent_stats("literature", settings.model_literature, client, extra_body, lit_stats),
            _agent_stats("chemist", settings.model_chemist, client, extra_body, chem_stats),
            _agent_stats("physicist", settings.model_physicist, phys_client, phys_extra_body, phys_stats),
            _agent_stats("experimentalist", settings.model_experimentalist, phys_client, phys_extra_body, exp_stats),
            _agent_stats("clinical", settings.model_literature, client, extra_body, clin_stats),
        ],
    )
    return lit_report, chem_report, phys_report, experimentalist_plan, clinical_report, final_report


# --- Deterministic simulation stage --------------------------------------------------------
#
# Every physical input to the Bloch-McConnell simulation is chosen here, not by a model: the
# exchange rate and chemical shift come from the cited reference table keyed by the chemist's
# group class, the concentration from the literature agent, B1/Tsat from a sweep. A group with no
# verified reference value is not simulated and is reported as unknown.

# Relaxation of a small dissolved metabolite pool. Not measured per metabolite; every provenance
# line says so, so nobody mistakes them for a literature value.
GENERIC_POOL_T1_S = 1.0
GENERIC_POOL_T2_S = 0.01
_POOL_RELAXATION_NOTE = "pool T1 1.0 s / T2 10 ms are generic small-solute values, not measured for this metabolite"
_MAX_BACKGROUND_METABOLITES = 3


@dataclass
class MetaboliteSimulation:
    name: str
    parameters: list[SimulationParameters] = field(default_factory=list)
    recommendations: list[SequenceRecommendation] = field(default_factory=list)
    contrast_by_field: dict[str, float] = field(default_factory=dict)
    zspectra: list[ZSpectrumCurve] = field(default_factory=list)
    best_group_by_field: dict[str, SimulationParameters] = field(default_factory=dict)
    diagnostics_by_field: dict[str, dict] = field(default_factory=dict)
    # [low, high] contrast of the best group per field over the concentration x k_ex corners.
    contrast_range_by_field: dict[str, list[float]] = field(default_factory=dict)
    note: str = ""

    @property
    def simulated(self) -> list[SimulationParameters]:
        return [p for p in self.parameters if p.status == "simulated"]


def pipeline_settings() -> dict:
    """Every deterministic knob that shapes a run's numbers, recorded in its provenance."""
    return {
        "contrast_threshold_percent": CONTRAST_THRESHOLD_PERCENT,
        "field_strengths_T": list(FIELD_STRENGTHS_T),
        "sweep_b1_grid_uT": list(bloch_bridge.B1_GRID_UT),
        "sweep_tsat_grid_ms": list(bloch_bridge.TSAT_GRID_MS),
        "sweep_saturation_model": "continuous wave, single shot from thermal equilibrium (CW-equivalent B1_rms)",
        "b1_ceiling": bloch_bridge.NO_SAR_MODEL_NOTE,
        "tsat_choice": "grid Tsat with the highest contrast at the best B1 (no scan-time shortening)",
        "pool_t1_s": GENERIC_POOL_T1_S,
        "pool_t2_s": GENERIC_POOL_T2_S,
        "max_background_metabolites": _MAX_BACKGROUND_METABOLITES,
        "verification_scan_field_T": experimentalist_agent.SCAN_FIELD_STRENGTH_T,
        "verification_scan_model": ("the experimentalist's pulse train (rectangular pulses at its B1), 90 degree "
                                    "single-shot readout, exact periodic steady state at its TR, Z normalized to the "
                                    "same cycle without saturation"),
        "b1_inhomogeneity_fraction": bloch_bridge.B1_INHOMOGENEITY_FRACTION,
        "uncertainty_corners": ("contrast re-simulated at the swept B1/Tsat at every corner of [concentration "
                                "range from the literature agent] x [k_ex spread of the reference tier]"),
        "llm_sampling": "each model's served defaults; the pipeline sends no temperature or top_p",
    }


# concentration_range_mM as the literature agent writes it: "1-5 mM", "200-400 mM", "0.5 to 2 mM",
# "~1-7 mM intracellular (hepatocyte)". The first two numbers are the bounds; anything else in the
# string is ignored. A string with fewer than two numbers, a non-positive upper bound, or an
# implausible thousand-fold span yields None -- "no usable range", never a guess.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_concentration_range(text: str, typical: float | None = None) -> tuple[float, float] | None:
    if not text:
        return None
    nums = [float(x) for x in _NUMBER_RE.findall(text.replace(",", ""))]
    if len(nums) < 2:
        return None
    low, high = sorted(nums[:2])
    if high <= 0 or low < 0:
        return None
    if low > 0 and high / low > 1000:
        return None
    if typical is not None and typical > 0:
        low, high = min(low, typical), max(high, typical)
    if high <= low:
        return None
    return low, high


def build_simulation_parameters(
    lit: LiteratureReport, chem: ChemistReport, body_part: str, entries=None,
) -> dict[str, list[SimulationParameters]]:
    """One SimulationParameters per exchangeable-proton group the chemist proposed, resolved
    against the reference table. Groups that cannot be simulated keep a status saying why."""
    lit_by_name = {m.name: m for m in lit.metabolites}
    out: dict[str, list[SimulationParameters]] = {}
    for finding in chem.findings:
        lit_m = lit_by_name.get(finding.metabolite_name)
        conc = float(lit_m.typical_concentration_mM) if lit_m else 0.0
        aliases = list(lit_m.aliases) if lit_m else []
        params: list[SimulationParameters] = []
        for ep in finding.exchangeable_protons:
            cls = exchange_reference.classify_group(ep.group, ep.group_class)
            rnoe = cls in exchange_reference.RNOE_CLASSES
            sp = SimulationParameters(
                metabolite_name=finding.metabolite_name, group_class=cls, group_text=ep.group,
                n_exchangeable_protons=int(ep.n_protons), concentration_mM=conc, status="simulated",
                mechanism=exchange_reference.mechanism_of(cls), chemist_shift_ppm=ep.ppm_shift_estimate,
            )
            # An rNOE pool is not exchangeable by definition (the chemist marks it so); it is
            # simulated anyway, as an upfield pool with the table's effective transfer rate.
            if not ep.is_exchangeable and not rnoe:
                sp.status = "not_exchangeable"
                sp.provenance = "the chemist judged this group not exchangeable with water; not simulated"
            elif cls == "other":
                sp.status = "unknown_group_class"
                sp.provenance = (f"'{ep.group}' could not be classed as amide/guanidinium/amine/hydroxyl/imidazole/"
                                 "thiol/phosphate/rnoe, so no reference exchange rate can be looked up -- we don't know "
                                 "its k_ex; not simulated")
            else:
                ref = exchange_reference.lookup(cls, finding.metabolite_name, aliases, tissue=body_part, entries=entries)
                if ref is None:
                    sp.status = "no_reference_for_group"
                    what = ("effective rNOE transfer rate and offset for this upfield pool" if rnoe
                            else f"exchange rate and shift for {cls} protons")
                    sp.provenance = f"the reference table has no verified {what} -- we don't know; not simulated"
                elif conc <= 0 or ep.n_protons <= 0:
                    sp.status = "no_concentration"
                    sp.provenance = "no positive concentration or proton count to simulate with; not simulated"
                else:
                    try:
                        bloch_bridge._check_pool_args(conc, ep.n_protons, finding.metabolite_name)
                    except ValueError as exc:
                        sp.status = "implausible_pool"
                        sp.provenance = f"not simulated: {exc}"
                    else:
                        sp.shift_ppm = ref.shift_ppm
                        sp.kex_per_s = ref.kex_per_s
                        sp.kex_low_per_s = ref.kex_low_per_s
                        sp.kex_high_per_s = ref.kex_high_per_s
                        sp.borrowed_from_other_tissue = ref.borrowed_from_other_tissue
                        sp.source_pmids = ref.source_pmids
                        sp.provenance = f"{ref.provenance} {_POOL_RELAXATION_NOTE}."
                        bounds = parse_concentration_range(lit_m.concentration_range_mM if lit_m else "", conc)
                        if bounds:
                            sp.concentration_low_mM, sp.concentration_high_mM = bounds
            params.append(sp)
        if not finding.exchangeable_protons:
            params.append(SimulationParameters(
                metabolite_name=finding.metabolite_name, group_class="other", group_text="(none proposed)",
                concentration_mM=conc, status="not_exchangeable",
                provenance="the chemist proposed no exchangeable protons; not simulated",
            ))
        out[finding.metabolite_name] = params
    return out


def _pool_args(sp: SimulationParameters) -> dict:
    return {
        "shift_ppm": sp.shift_ppm, "exchange_rate_per_s": sp.kex_per_s, "concentration_mM": sp.concentration_mM,
        "n_exchangeable_protons": sp.n_exchangeable_protons, "pool_t1_s": GENERIC_POOL_T1_S, "pool_t2_s": GENERIC_POOL_T2_S,
    }


def _background_pools(name: str, params_by_name: dict[str, list[SimulationParameters]]) -> list[dict]:
    """The other candidates' simulated groups, highest-concentration metabolites first, so the
    overlap diagnostics and the with/without difference reflect real pool competition."""
    others = [(n, ps) for n, ps in params_by_name.items()
              if n != name and any(p.status == "simulated" for p in ps)]
    others.sort(key=lambda item: -max(p.concentration_mM for p in item[1]))
    pools = []
    for other, ps in others[:_MAX_BACKGROUND_METABOLITES]:
        for p in ps:
            if p.status == "simulated":
                pools.append({"label": f"{other} [{p.group_class}]", **_pool_args(p)})
    return pools


def run_reference_simulations(
    lit: LiteratureReport, chem: ChemistReport, body_part: str, entries=None,
) -> dict[str, MetaboliteSimulation]:
    """Sweep B1 x Tsat for every simulable group of every metabolite at every field strength, keep
    the best group per field, and record the Z-spectrum at that optimum."""
    params_by_name = build_simulation_parameters(lit, chem, body_part, entries)
    sims: dict[str, MetaboliteSimulation] = {}
    for name, params in params_by_name.items():
        sim = MetaboliteSimulation(name=name, parameters=params)
        sims[name] = sim
        if not sim.simulated:
            sim.note = "Not simulated: " + "; ".join(f"{p.group_text} [{p.group_class}] -- {p.provenance}" for p in params)
            continue
        background = _background_pools(name, params_by_name)
        best_by_field: dict[float, tuple[SimulationParameters, dict]] = {}
        for p in sim.simulated:
            for f in FIELD_STRENGTHS_T:
                try:
                    best = bloch_bridge.optimize_saturation(
                        field_strength_T=f, background_metabolites=background, **_pool_args(p),
                        **bloch_bridge.organ_tissue_overrides(body_part, f),
                    )
                except Exception as exc:  # one group failing must not sink the metabolite
                    p.provenance = f"{p.provenance} Sweep failed at {f:g} T: {exc}."
                    continue
                p.contrast_percent_by_field[str(f)] = best["max_achievable_contrast_percent"]
                if f not in best_by_field or best["max_achievable_contrast_percent"] > best_by_field[f][1]["max_achievable_contrast_percent"]:
                    best_by_field[f] = (p, best)
        for f in sorted(best_by_field):
            p, best = best_by_field[f]
            rec = _recommendation_from_sweep(f, best, rationale="")
            # The "<tissue>::<metabolite>" label makes call() apply the target tissue's tabulated
            # water T1/T2/MT, exactly as it does for the verification scan's ROIs.
            args = {**_pool_args(p), "field_strength_T": f, "b1_uT": rec.b1_uT,
                    "saturation_time_s": rec.saturation_time_ms / 1000.0,
                    "background_metabolites": background, "metabolite_label": f"{body_part}::{name}", "decompose": True}
            result = bloch_bridge.call(args)
            sim.recommendations.append(rec)
            sim.contrast_by_field[str(f)] = best["max_achievable_contrast_percent"]
            sim.best_group_by_field[str(f)] = p
            corners = _uncertainty_corners(p, f, rec, background, body_part, best["max_achievable_contrast_percent"])
            if corners:
                p.contrast_range_percent_by_field[str(f)] = corners
                sim.contrast_range_by_field[str(f)] = corners
            sim.zspectra.append(ZSpectrumCurve(
                field_strength_T=f, offsets_ppm=result["offsets_ppm"],
                z_with_metabolite=result["z_with_metabolite"], z_without_metabolite=result["z_without_metabolite"],
                contrast_diff_percent=result["contrast_diff_percent"], overlap_warnings=result.get("overlap_warnings", []),
                direct_saturation_percent=result.get("direct_saturation_percent"),
                mt_background_percent=result.get("mt_background_percent"), mt_fraction_of_water=result.get("mt_fraction_of_water"),
                simulated_concentration_mM=p.concentration_mM, n_exchangeable_protons=p.n_exchangeable_protons,
                exchange_rate_per_s=p.kex_per_s or 0.0, shift_ppm=p.shift_ppm or 0.0,
            ))
            sim.diagnostics_by_field[str(f)] = {
                "group": f"{p.group_text} [{p.group_class}]",
                "overlap_warnings": result.get("overlap_warnings", []),
                "direct_sat_bandwidth_hz": result.get("direct_sat_bandwidth_hz"),
                "exchange_regime": result.get("exchange_regime"),
            }
        skipped = [p for p in params if p.status != "simulated"]
        if skipped:
            sim.note = "Groups not simulated: " + "; ".join(f"{p.group_text} [{p.group_class}] -- {p.provenance}" for p in skipped)
    return sims


def _uncertainty_corners(
    p: SimulationParameters, field_strength_T: float, rec: SequenceRecommendation, background: list[dict],
    body_part: str, point_contrast: float,
) -> list[float] | None:
    """[low, high] contrast at the swept B1/Tsat over every corner of the concentration range x k_ex
    spread box (the point estimate included), or None when neither input has a range. Contrast is
    monotonic in concentration but not in k_ex (a pool can be too slow or too fast for a given
    B1), which is why all corners are evaluated rather than just the two extremes."""
    concs = sorted({c for c in (p.concentration_low_mM, p.concentration_high_mM) if c is not None})
    kexs = sorted({k for k in (p.kex_low_per_s, p.kex_high_per_s) if k is not None and k > 0})
    if not concs and len(kexs) < 2:
        return None
    concs = concs or [p.concentration_mM]
    kexs = kexs or [p.kex_per_s]
    values = [point_contrast]
    overrides = bloch_bridge.organ_tissue_overrides(body_part, field_strength_T)
    for c in concs:
        for k in kexs:
            if c <= 0:
                values.append(0.0)
                continue
            try:
                bloch_bridge._check_pool_args(c, p.n_exchangeable_protons, p.metabolite_name)
                r = bloch_bridge.simulate_with_without_metabolite(
                    **{**_pool_args(p), "concentration_mM": c, "exchange_rate_per_s": k},
                    field_strength_T=field_strength_T, b1_uT=rec.b1_uT, saturation_time_s=rec.saturation_time_ms / 1000.0,
                    background_metabolites=background, **overrides,
                )
            except Exception:
                continue
            values.append(abs(r["contrast_diff_percent"]))
    return [round(min(values), 4), round(max(values), 4)]


def tissue_parameter_rows(body_part: str, plan: "InVivoVerificationPlan | None" = None) -> list[TissueParameterRow]:
    """Every water/MT parameter the simulations used, as table rows: the target tissue at each
    swept field strength, then each verification-scan ROI at the scan field."""
    rows: list[TissueParameterRow] = []
    for f in FIELD_STRENGTHS_T:
        for r in tissue_reference.parameter_rows(body_part, f):
            rows.append(TissueParameterRow(tissue=body_part, role="target tissue", field_T=f, parameter=r["parameter"],
                                           value=r["value"], source=r["source"] + (f" -- {r['note']}" if r.get("note") else ""),
                                           verified=r["verified"]))
    if plan is not None:
        scan_f = experimentalist_agent.SCAN_FIELD_STRENGTH_T
        seen = set()
        for roi in [r.name for r in plan.roi_set] or sorted({p.organ for p in plan.organ_predictions}):
            if roi.strip().lower() in seen:
                continue
            seen.add(roi.strip().lower())
            for r in tissue_reference.parameter_rows(roi, scan_f):
                rows.append(TissueParameterRow(tissue=roi, role="verification ROI", field_T=scan_f, parameter=r["parameter"],
                                               value=r["value"], source=r["source"] + (f" -- {r['note']}" if r.get("note") else ""),
                                               verified=r["verified"]))
    return rows


def tissue_parameters_note(body_part: str, field_strength_T: float = 3.0) -> str:
    """Where the target tissue's water T1/T2 and MT pool came from, for the report and the
    physicist's briefing (stated at 3 T; the same table entries are interpolated per field)."""
    return f"Target tissue '{body_part}': " + tissue_reference.describe(body_part, field_strength_T)


def simulation_summary(sims: dict[str, MetaboliteSimulation]) -> list[dict]:
    """What the physicist agent is shown: every number it must review but may not change."""
    out = []
    for name, sim in sims.items():
        out.append({
            "name": name,
            "groups": [{
                "group": p.group_text, "group_class": p.group_class, "n_protons": p.n_exchangeable_protons,
                "status": p.status, "shift_ppm": p.shift_ppm, "kex_per_s": p.kex_per_s,
                "concentration_mM": p.concentration_mM, "provenance": p.provenance,
                "max_contrast_percent_by_field": p.contrast_percent_by_field,
                "concentration_range_mM": ([p.concentration_low_mM, p.concentration_high_mM]
                                           if p.concentration_low_mM is not None else None),
                "kex_spread_per_s": ([p.kex_low_per_s, p.kex_high_per_s]
                                     if p.kex_low_per_s is not None and p.kex_high_per_s != p.kex_low_per_s else None),
                "contrast_range_percent_by_field_over_uncertainty_corners": p.contrast_range_percent_by_field,
            } for p in sim.parameters],
            "best_contrast_percent_by_field": sim.contrast_by_field,
            "swept_saturation_by_field": {
                str(r.field_strength_T): {"b1_uT": r.b1_uT, "saturation_time_ms": r.saturation_time_ms}
                for r in sim.recommendations
            },
            "diagnostics_by_field": sim.diagnostics_by_field,
            "note": sim.note,
        })
    return out


def _recommendation_from_sweep(field_strength: float, best: dict, rationale: str) -> SequenceRecommendation:
    return SequenceRecommendation(
        field_strength_T=field_strength,
        b1_uT=best["b1_uT"],
        saturation_time_ms=best["saturation_time_ms"],
        # The sweep simulates continuous-wave saturation, so this is a CW-equivalent B1_rms. A
        # pulsed train delivering the same B1_rms may run a higher peak amplitude
        # (B1_peak = B1_rms / sqrt(duty cycle)); that translation is the experimentalist's job.
        duty_cycle_or_pulses="continuous wave (CW-equivalent B1_rms from simulation sweep)",
        rationale=rationale,
        derived_from="bloch_mcconnell_sweep",
        contrast_at_optimum_percent=best["contrast_percent"],
        max_achievable_contrast_percent=best["max_achievable_contrast_percent"],
        sweep_b1_grid_uT=best.get("sweep_b1_grid_uT", []),
        sweep_tsat_grid_ms=best.get("sweep_tsat_grid_ms", []),
        sweep_contrast=best.get("sweep_contrast", {}),
        n_pulses=1,
        pulse_duration_ms=best["saturation_time_ms"],
        interpulse_delay_ms=0.0,
    )


def _sar_note(recommendations: list[SequenceRecommendation]) -> str:
    """No SAR model is applied (see bloch_bridge.NO_SAR_MODEL_NOTE); kept so the schema field
    that used to carry the cap's explanation now says so plainly."""
    return bloch_bridge.NO_SAR_MODEL_NOTE if recommendations else ""


def _apply_reference_simulations(phys_report: PhysicistReport, sims: dict[str, MetaboliteSimulation]) -> None:
    """The physicist's verdict carries the simulation's numbers, never its own: contrast per
    field, swept B1/Tsat (keeping only the model's rationale text), and the note on what could
    not be simulated. Detectability uses max_achievable_contrast_percent -- the true ceiling at
    the swept B1 -- not the knee-shortened Tsat's own value, so the scan-time trade-off never
    flips a verdict."""
    by_key = {n.strip().lower(): s for n, s in sims.items()}
    for verdict in phys_report.verdicts:
        sim = by_key.get(verdict.metabolite_name.strip().lower())
        if sim is None:
            verdict.simulated_contrast_percent = {}
            verdict.sequence_recommendations = []
            verdict.simulation_coverage_note = ("No chemist finding matched this verdict's metabolite name, so nothing "
                                                "was simulated for it.")
            continue
        rationale_by_field = {round(s.field_strength_T, 3): s.rationale for s in verdict.sequence_recommendations}
        verdict.sequence_recommendations = [
            r.model_copy(update={"rationale": rationale_by_field.get(round(r.field_strength_T, 3), "")})
            for r in sim.recommendations
        ]
        verdict.simulated_contrast_percent = dict(sim.contrast_by_field)
        verdict.simulation_coverage_note = sim.note
        verdict.saturation_optimizer_note = _sar_note(verdict.sequence_recommendations)


def _simulate_organ_predictions(
    plan: InVivoVerificationPlan, sims: dict[str, MetaboliteSimulation], field_strength_T: float,
) -> None:
    """Simulate every (organ, metabolite) pair the experimentalist listed, with that organ's
    measured water T1/T2/MT (via the '<organ>::' label), the metabolite's best reference-backed
    group, the experimentalist's organ concentration, and its compromise protocol run as written:
    the pulse train (n_pulses x pulse_duration with interpulse delays, rectangular pulses at its
    B1), a 90 degree single-shot readout and the exact periodic steady state at its TR, with Z
    normalized to the same cycle without saturation. Each pair is then re-simulated at
    (1 -/+ B1_INHOMOGENEITY_FRACTION) x B1 so the prediction carries the range an uncalibrated
    transmit field would produce. Pairs with nothing to simulate say so."""
    by_key = {n.strip().lower(): s for n, s in sims.items()}
    sp = plan.scan_protocol
    field_key = str(float(field_strength_T))

    def best_group(sim: MetaboliteSimulation | None) -> SimulationParameters | None:
        if sim is None:
            return None
        return sim.best_group_by_field.get(field_key) or next(iter(sim.best_group_by_field.values()), None)

    conc_by_organ: dict[str, dict[str, float]] = {}
    for pred in plan.organ_predictions:
        conc_by_organ.setdefault(pred.organ.strip().lower(), {})[pred.metabolite_name.strip().lower()] = pred.organ_concentration_mM

    unsimulated = []
    for pred in plan.organ_predictions:
        key = pred.metabolite_name.strip().lower()
        organ = pred.organ.strip().lower()
        sim = by_key.get(key)
        group = best_group(sim)
        if group is None:
            pred.predicted_contrast_percent = 0.0
            pred.simulation_note = "not simulated: " + (sim.note if sim and sim.note else
                                                        "no reference-backed exchangeable group for this metabolite")
            unsimulated.append(f"{pred.organ}/{pred.metabolite_name}")
            continue
        if pred.organ_concentration_mM <= 0:
            pred.predicted_contrast_percent = 0.0
            pred.simulation_note = "not simulated: the experimentalist gave a non-positive organ concentration"
            unsimulated.append(f"{pred.organ}/{pred.metabolite_name}")
            continue
        background = []
        for other_key, other_sim in by_key.items():
            og = best_group(other_sim) if other_key != key else None
            oconc = conc_by_organ.get(organ, {}).get(other_key, 0.0)
            if og is not None and oconc > 0:
                background.append({"label": f"{other_sim.name} [{og.group_class}]", **_pool_args(og), "concentration_mM": oconc})
        pulsed = bool(sp.n_pulses and sp.n_pulses > 1 and sp.pulse_duration_ms and sp.pulse_duration_ms > 0)
        args = {**_pool_args(group), "concentration_mM": pred.organ_concentration_mM,
                "field_strength_T": float(field_strength_T), "b1_uT": sp.b1_uT,
                "saturation_time_s": sp.total_saturation_time_ms / 1000.0,
                "n_pulses": int(sp.n_pulses) if pulsed else 1,
                "pulse_duration_s": sp.pulse_duration_ms / 1000.0 if pulsed else None,
                "interpulse_delay_s": max(sp.interpulse_delay_ms, 0.0) / 1000.0 if pulsed else 0.0,
                "tr_s": sp.tr_ms / 1000.0 if sp.tr_ms and sp.tr_ms > 0 else None,
                "background_metabolites": background[:2 * _MAX_BACKGROUND_METABOLITES],
                "metabolite_label": f"{pred.organ}::{pred.metabolite_name}"}
        frac = bloch_bridge.B1_INHOMOGENEITY_FRACTION
        scales = [round(1.0 - frac, 3), round(1.0 + frac, 3)]
        try:
            result = bloch_bridge.call(args)
            spread = [result["contrast_diff_percent"]]
            for scale in scales:
                scaled_b1 = sp.b1_uT * scale
                if 0 < scaled_b1 <= bloch_bridge.MAX_B1_UT:
                    spread.append(bloch_bridge.call({**args, "b1_uT": scaled_b1})["contrast_diff_percent"])
        except Exception as exc:
            pred.predicted_contrast_percent = 0.0
            pred.simulation_note = f"not simulated: {exc}"
            unsimulated.append(f"{pred.organ}/{pred.metabolite_name}")
            continue
        pred.predicted_contrast_percent = result["contrast_diff_percent"]
        pred.predicted_contrast_range_percent = [round(min(spread), 4), round(max(spread), 4)]
        pred.b1_scale_range = scales
        tissue = tissue_reference.lookup(pred.organ, float(field_strength_T))
        tissue_bit = (f"ROI water/MT from the tissue table for '{tissue.tissue}' (PMID {', '.join(tissue.source_pmids)})"
                      if tissue else f"no tissue-table entry for '{pred.organ}': generic water/MT defaults")
        rate_word = "rNOE transfer rate" if group.mechanism == "rNOE" else "k_ex"
        pred.simulation_note = (
            f"{group.group_text} [{group.group_class}] pool: {rate_word} {group.kex_per_s:g} s^-1 at {group.shift_ppm:g} ppm, "
            f"{group.n_exchangeable_protons} H per unit, {pred.organ_concentration_mM:g} mM; B1 {sp.b1_uT:g} uT per pulse "
            f"(B1_rms {result['b1_rms_uT']:g} uT), {result['saturation_model']}. Contrast at "
            f"{scales[0]:g}x / {scales[1]:g}x B1: {min(spread):.3f}% to {max(spread):.3f}%. {tissue_bit}; see the tissue "
            f"table. Rate source: PMID {', '.join(group.source_pmids) or 'reference table'}"
            + (" (borrowed from another tissue/setting)" if group.borrowed_from_other_tissue else "") + "."
        )
    if unsimulated:
        plan.caveats = (
            f"{plan.caveats} [Not simulated] No reference-backed simulation was possible for: {', '.join(unsimulated)}. "
            "Their predicted_contrast_percent is reported as 0 (not simulated), not as a prediction."
        ).strip()


def _merge(
    lit: LiteratureReport,
    chem: ChemistReport,
    phys: PhysicistReport,
    sims: dict[str, MetaboliteSimulation] | None = None,
    experimentalist: InVivoVerificationPlan | None = None,
    clinical: ClinicalReport | None = None,
) -> FinalReport:
    sims_by_key = {n.strip().lower(): s for n, s in (sims or {}).items()}
    clinical_by_key = {e.metabolite_name.strip().lower(): e for e in (clinical.entries if clinical else [])}
    lit_by_name = {m.name: m for m in lit.metabolites}
    chem_by_name = {f.metabolite_name: f for f in chem.findings}

    finals: list[FinalMetabolite] = []
    for verdict in phys.verdicts:
        lit_m = lit_by_name.get(verdict.metabolite_name)
        chem_f = chem_by_name.get(verdict.metabolite_name)

        recommended_fields = sorted({
            float(k) for k, v in verdict.simulated_contrast_percent.items() if v >= CONTRAST_THRESHOLD_PERCENT
        })
        detectable = len(recommended_fields) > 0
        sim = sims_by_key.get(verdict.metabolite_name.strip().lower())
        simulated_any = bool(sim and sim.simulated)
        # "unknown" is a different finding from "not detectable": nothing could be simulated
        # (no group had a verified reference rate), so no contrast can be claimed either way.
        detectability = "detectable" if detectable else ("not detectable" if simulated_any else "unknown")
        detection_mechanism = _detection_mechanism(sim, recommended_fields) if detectable else ""
        robustness, robustness_note = _robustness(sim, detectable)
        if detectability == "unknown":
            robustness, robustness_note = "unknown", ("Nothing was simulated for this metabolite (no exchangeable or "
                                                      "rNOE group with a verified reference rate), so detectability "
                                                      "is unknown -- not 'no'.")

        proton_bits = []
        if chem_f:
            for ep in chem_f.exchangeable_protons:
                flag = "exchangeable" if ep.is_exchangeable else "not exchangeable"
                proton_bits.append(f"{ep.group} ({ep.n_protons}H, {ep.ppm_shift_estimate:+.2f} ppm, {ep.exchange_regime} exchange, {flag})")
        proton_summary = "; ".join(proton_bits) if proton_bits else "no exchangeable protons identified"

        citations = list(verdict.verifying_citations)
        if lit_m:
            seen_pmids = {c.pmid for c in citations}
            for c in lit_m.citations:
                if c.pmid not in seen_pmids:
                    citations.append(c)
                    seen_pmids.add(c.pmid)

        caveats_parts = [
            p for p in [
                verdict.critique_of_biologist,
                verdict.critique_of_chemist,
                verdict.in_vivo_frequency_note,
                verdict.neglected_exchangeable_protons,
                verdict.citation_verification_note,
                verdict.simulation_coverage_note,
                verdict.saturation_optimizer_note,
                chem_f.structure_verification_note if chem_f else "",
                lit_m.notes if lit_m else "",
            ] if p
        ]

        finals.append(FinalMetabolite(
            name=verdict.metabolite_name,
            typical_concentration_mM=lit_m.typical_concentration_mM if lit_m else float("nan"),
            concentration_range_mM=lit_m.concentration_range_mM if lit_m else "unknown",
            animal_evidence_only=lit_m.animal_evidence_only if lit_m else False,
            concentration_evidence_quote=lit_m.concentration_evidence_quote if lit_m else "",
            concentration_evidence_pmid=lit_m.concentration_evidence_pmid if lit_m else "",
            concentration_basis=lit_m.concentration_basis if lit_m else "unknown",
            concentration_reasoning=lit_m.concentration_reasoning if lit_m else "",
            reasoning_steps=lit_m.reasoning_steps if lit_m else [],
            reference_concentrations=lit_m.reference_concentrations if lit_m else "",
            detectable_by_cest=detectable,
            detectability=detectability,
            detection_mechanism=detection_mechanism,
            field_strengths_recommended_T=recommended_fields,
            best_sequence_by_field=verdict.sequence_recommendations,
            exchangeable_protons_summary=proton_summary,
            simulated_contrast_percent=verdict.simulated_contrast_percent,
            zspectra_by_field=sorted(
                (sims_by_key[verdict.metabolite_name.strip().lower()].zspectra
                 if verdict.metabolite_name.strip().lower() in sims_by_key else []),
                key=lambda z: z.field_strength_T,
            ),
            simulation_parameters=(sims_by_key[verdict.metabolite_name.strip().lower()].parameters
                                   if verdict.metabolite_name.strip().lower() in sims_by_key else []),
            smiles=chem_f.smiles if chem_f else None,
            structure_verification_note=chem_f.structure_verification_note if chem_f else "",
            exchangeable_protons_detail=chem_f.exchangeable_protons if chem_f else [],
            frequency_collision_analysis=verdict.frequency_collision_analysis,
            direct_water_saturation_note=verdict.direct_water_saturation_note,
            b1_tsat_extraction_method_note=verdict.b1_tsat_extraction_method_note,
            extraction_method_citations=verdict.extraction_method_citations,
            ex_vivo_in_vivo_exchange_caveat=verdict.ex_vivo_in_vivo_exchange_caveat,
            key_citations=citations[:8],
            caveats=" | ".join(caveats_parts),
            detectability_robustness=robustness,
            contrast_range_percent_by_field=dict(sim.contrast_range_by_field) if sim else {},
            robustness_note=robustness_note,
            clinical_evidence=clinical_by_key.get(verdict.metabolite_name.strip().lower()),
        ))

    return FinalReport(body_part=lit.body_part, metabolites=finals, in_vivo_verification_plan=experimentalist,
                       excluded_candidates=list(getattr(lit, "excluded", []) or []))


def _detection_mechanism(sim: "MetaboliteSimulation | None", fields: list[float]) -> str:
    """'chemical exchange', 'rNOE' or 'both', from the best group at each field that clears the
    threshold."""
    if sim is None:
        return ""
    mechs = {sim.best_group_by_field[str(f)].mechanism for f in fields if str(f) in sim.best_group_by_field}
    if not mechs:
        return ""
    return "both" if len(mechs) > 1 else next(iter(mechs))


def _robustness(sim: "MetaboliteSimulation | None", detectable: bool) -> tuple[str, str]:
    """Grade a detectability verdict against the uncertainty corners (see _uncertainty_corners):
    'robust' when the LOW corner still clears the threshold at some field, 'fragile' when only the
    point estimate does, 'upper-bound only' when only the high corner does, 'no' otherwise, and
    'unknown' when no range was available to test with."""
    if sim is None or not sim.contrast_range_by_field:
        if detectable:
            return "unknown", ("No concentration range or k_ex spread was available for this metabolite, so the "
                               "verdict rests on the point estimate alone.")
        return "no", ""
    fields = sorted(sim.contrast_range_by_field, key=float)
    low_max = max(sim.contrast_range_by_field[f][0] for f in fields)
    high_max = max(sim.contrast_range_by_field[f][1] for f in fields)
    ranges = ", ".join(f"{float(f):g} T: {sim.contrast_range_by_field[f][0]:.2f}-{sim.contrast_range_by_field[f][1]:.2f}%"
                       for f in fields)
    inputs = []
    seen = set()
    for p in sim.best_group_by_field.values():
        if id(p) in seen:
            continue
        seen.add(id(p))
        bits = []
        if p.concentration_low_mM is not None:
            bits.append(f"concentration {p.concentration_low_mM:g}-{p.concentration_high_mM:g} mM")
        if p.kex_low_per_s is not None and p.kex_high_per_s is not None and p.kex_high_per_s > p.kex_low_per_s:
            bits.append(f"k_ex {p.kex_low_per_s:g}-{p.kex_high_per_s:g} s^-1")
        elif p.kex_per_s is not None:
            bits.append(f"k_ex fixed at {p.kex_per_s:g} s^-1 (single measurement, spread unknown)")
        inputs.append(f"{p.group_text} [{p.group_class}]: " + ", ".join(bits))
    note = (f"Contrast over the uncertainty corners ({'; '.join(inputs)}) at the swept B1/Tsat: {ranges}. "
            f"Threshold {CONTRAST_THRESHOLD_PERCENT}%.")
    if detectable and low_max >= CONTRAST_THRESHOLD_PERCENT:
        return "robust", note + " Detectable even at the low corner."
    if detectable:
        return "fragile", note + " Only the point estimate clears the threshold; the low corner does not."
    if high_max >= CONTRAST_THRESHOLD_PERCENT:
        return "upper-bound only", note + " The point estimate is below threshold; only the high corner clears it."
    return "no", note


def clinical_markdown(ce) -> list[str]:
    """'Potential clinical impact' bullet block: counts, one row per verified study, pooled effects."""
    if ce is None:
        return []
    if not ce.studies:
        note = f" {ce.search_note}" if ce.search_note else ""
        return [f"- **Potential clinical impact:** no verified disease application of this pool's CEST/NOE signal found.{note}"]
    lines = [f"- **Potential clinical impact** ({ce.pool or ce.metabolite_name}): {ce.n_cest_studies} CEST/NOE application and "
             f"{ce.n_change_studies} metabolite-change studies verified ({ce.n_human_studies} human, {ce.n_animal_studies} animal; "
             f"{ce.n_humans} humans, {ce.n_animals} animals; {ce.n_studies_dropped} dropped by verification). {ce.summary}".rstrip()]
    lines += ["", "  | Evidence | Disease | Species / setting | n disease / control | Metric / method | Disease vs control | Result | PMID |",
              "  |---|---|---|---|---|---|---|---|"]
    for s in ce.studies:
        n = f"{s.n_disease if s.n_disease is not None else '?'} / {s.n_control if s.n_control is not None else '?'}"
        spread = "SEM" if s.spread_is_sem else "SD"
        means = (f"{s.disease_mean:g} ± {s.disease_sd:g} vs {s.control_mean:g} ± {s.control_sd:g} {s.unit} ({spread})"
                 + (" [full text]" if s.stats_source.startswith("full text") else "")
                 if None not in (s.disease_mean, s.disease_sd, s.control_mean, s.control_sd) else "—")
        result = "; ".join(x for x in (s.direction if s.direction != "not stated" else "", s.p_value_text, s.note) if x) or "—"
        evidence = "CEST/NOE" if s.evidence_type == "cest_application" else "metabolite change"
        lines.append(f"  | {evidence} | {s.disease} ({s.disease_category}) | {s.species}, {s.study_setting} | {n} | "
                     f"{(s.cest_metric if s.evidence_type == 'cest_application' else s.measurement) or '—'} | {means} | {result} | {s.pmid} |")
    lines.append("")
    for p in ce.pooled_effects:
        what = "CEST/NOE signal" if p.evidence_type == "cest_application" else "metabolite level"
        lines.append(f"  - Pooled group difference in the {what}, {p.disease_category} ({p.n_studies} studies, {p.n_disease} disease / {p.n_control} control): "
                     f"Hedges' g = {p.hedges_g:+.2f} [{p.ci_low:+.2f}, {p.ci_high:+.2f}], I² = {p.i2_percent:.0f} % — {p.direction}; {p.note}.")
    if not ce.pooled_effects:
        lines.append("  - Pooled group difference: not computed (fewer than 3 verified studies with group means, SDs and sizes for one evidence type and disease category).")
    if ce.verification_note:
        lines.append(f"  - Dropped by verification: {ce.verification_note}")
    return lines


def _citation_tag(c) -> str:
    bits = [c.study_setting]
    if c.species:
        bits.append(c.species)
    if c.n_subjects:
        bits.append(f"n={c.n_subjects}")
    return f" [{', '.join(bits)}]" if bits else ""


def _md_prose(label: str, text: str, indent: str = "") -> list[str]:
    """A labelled field as one line when short, or as bullets of <= 60 words when long, so no
    block of prose exceeds ~200 words (report rule)."""
    if not text:
        return []
    if not is_long(text, 80):
        return [f"{indent}- **{label}:** {text}"]
    return [f"{indent}- **{label}:**"] + [f"{indent}  - {b}" for b in bullets(text)]


def detectability_of(m: FinalMetabolite) -> str:
    """'detectable' / 'not detectable' / 'unknown'. Runs saved before the three-way field existed
    carry only the boolean; for those, no simulated contrast at all means nothing was simulated,
    i.e. 'unknown' -- which is exactly what the boolean used to hide."""
    status = getattr(m, "detectability", "") or ""
    if status:
        return status
    if m.detectable_by_cest:
        return "detectable"
    return "not detectable" if m.simulated_contrast_percent else "unknown"


def verdict_label(m: FinalMetabolite) -> str:
    """'detectable (rNOE)', 'not detectable' or 'unknown (not simulated)'."""
    status = detectability_of(m)
    if status == "detectable":
        mech = getattr(m, "detection_mechanism", "")
        return f"detectable ({mech})" if mech else "detectable"
    if status == "unknown":
        return "unknown (not simulated)"
    return "not detectable"


def chemist_shift_note(p: SimulationParameters) -> str:
    """' (chemist estimate +1.0 ppm, not used)' when the chemist's ppm differs from the simulated
    reference shift, so the two numbers never sit in a report looking like a contradiction."""
    if p.chemist_shift_ppm is None or p.shift_ppm is None or abs(p.chemist_shift_ppm - p.shift_ppm) < 0.05:
        return ""
    return f" (chemist estimate {p.chemist_shift_ppm:+.1f} ppm, not used)"


def _tissue_table_markdown(rows) -> list[str]:
    if not rows:
        return []
    out = ["## Tissue parameters used in the simulations", "",
           "| Tissue | Role | Field (T) | Parameter | Value | Verified | Source |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r.tissue} | {r.role} | {r.field_T:g} | {r.parameter} | {r.value} | {r.verified} | {r.source} |")
    out.append("")
    return out


_MD_UNITS = ((re.compile(r"\bs\^-1\b"), "s⁻¹"), (re.compile(r"\bB0\b"), "B₀"), (re.compile(r"\bB1\b"), "B₁"),
             (re.compile(r"\bT1\b"), "T₁"), (re.compile(r"\bT2\b"), "T₂"), (re.compile(r"\buT\b"), "µT"))


def _md_units(text: str) -> str:
    for pat, rep in _MD_UNITS:
        text = pat.sub(rep, text)
    return text


def render_markdown(report: FinalReport) -> str:
    return _md_units(_render_markdown(report))


def _render_markdown(report: FinalReport) -> str:
    lines = [f"# CEST-detectable metabolite candidates: {report.body_part}", ""]
    detectable = [m for m in report.metabolites if m.detectable_by_cest]
    unknown = [m for m in report.metabolites if detectability_of(m) == "unknown"]
    not_detectable = [m for m in report.metabolites if not m.detectable_by_cest and m not in unknown]

    lines.append(f"{len(detectable)} of {len(report.metabolites)} candidates: simulated with-vs-without contrast >= "
                 f"{CONTRAST_THRESHOLD_PERCENT}% at some field strength (chemical exchange downfield or rNOE upfield). "
                 f"{len(not_detectable)} below threshold; {len(unknown)} unknown (no verified reference rate, nothing "
                 "simulated). The threshold applies to our own simulation, never to a literature MTRasym.")
    lines.append("")

    for x in getattr(report, "excluded_candidates", []) or []:
        lines.append(f"- **Excluded before simulation (not stored in the tissue):** {x}")
    if getattr(report, "excluded_candidates", None):
        lines.append("")

    for m in detectable + not_detectable + unknown:
        lines.append(f"## {m.name} -- {verdict_label(m)}")
        lines.append("")
        animal_flag = " (**animal evidence only**)" if m.animal_evidence_only else ""
        lines.append(f"- **Concentration:** {m.typical_concentration_mM:.2f} mM typical "
                     f"({m.concentration_range_mM}){animal_flag}")
        if m.concentration_evidence_quote:
            pmid_str = f" (PMID {m.concentration_evidence_pmid})" if m.concentration_evidence_pmid else ""
            lines.append(f"  - Source excerpt{pmid_str}: \"{m.concentration_evidence_quote}\"")
        elif m.concentration_reasoning or m.reasoning_steps:
            lines.append(f"  - **Estimated** (no direct literature value found). Reasoning: {m.concentration_reasoning}")
            for step in m.reasoning_steps:
                src = f"{step.source_type}: {step.source_detail}" if step.source_detail else step.source_type
                val = f" = {step.value}" if step.value else ""
                lines.append(f"    - {step.statement}{val}  [{src}]")
        else:
            lines.append("  - *No verified literature excerpt and no estimation reasoning given -- "
                          "treat this number as unsupported.*")
        if m.reference_concentrations:
            lines.append(f"  - Reference database (biofluid, for comparison): {m.reference_concentrations}")
        if m.structure_verification_note:
            lines.append(f"- **Structure check:** {m.structure_verification_note}")
        lines.append(f"- **CEST-detectable (simulated):** {verdict_label(m)}")
        if m.field_strengths_recommended_T:
            fields_str = ", ".join(f"{f:g} T" for f in m.field_strengths_recommended_T)
            lines.append(f"- **Recommended field strength(s):** {fields_str}")
        lines.append(f"- **Exchangeable protons:** {m.exchangeable_protons_summary}")
        if m.simulation_parameters:
            lines.append("- **Simulation inputs** (k_ex and shift from the cited reference table for each group's "
                         "class -- the model chose only the class; concentration from the literature agent):")
            for p in m.simulation_parameters:
                mech = f", {p.mechanism}" if getattr(p, "mechanism", "") == "rNOE" else ""
                if p.status == "simulated":
                    borrowed = " **[borrowed from another tissue/setting]**" if p.borrowed_from_other_tissue else ""
                    contrast = ", ".join(f"{float(k):g} T: {v:.2f}%" for k, v in
                                         sorted(p.contrast_percent_by_field.items(), key=lambda kv: float(kv[0])))
                    rate = "transfer rate" if mech else "k_ex"
                    lines.append(f"  - {p.group_text} [{p.group_class}{mech}], {p.n_exchangeable_protons}H at "
                                 f"{p.concentration_mM:g} mM: {rate} {p.kex_per_s:g} s^-1, offset {p.shift_ppm:g} ppm"
                                 f"{chemist_shift_note(p)}{borrowed}. Max contrast: {contrast}")
                    lines += [f"    - {b}" for b in bullets(p.provenance)]
                else:
                    lines.append(f"  - {p.group_text} [{p.group_class}{mech}]: **NOT SIMULATED** ({p.status}) -- {p.provenance}")

        if m.zspectra_by_field:
            lines.append("- **Simulated with-vs-without-metabolite contrast (panel letters as in the Z-spectrum figure):**")
            for i, c in enumerate(sorted(m.zspectra_by_field, key=lambda z: z.field_strength_T)):
                lines.append(f"  - {chr(ord('A') + i)} — {c.field_strength_T:g} T: {c.contrast_diff_percent:.3f}%"
                             + (f" (at that offset water alone loses {c.direct_saturation_percent:.2f}% to direct saturation, "
                                f"the MT pool ({c.mt_fraction_of_water:g} of water) and background pools {c.mt_background_percent:.2f}%)"
                                if c.direct_saturation_percent is not None else "")
                             + (f"; {len(c.overlap_warnings)} warning(s)" if c.overlap_warnings else ""))
        elif m.simulated_contrast_percent:
            contrast_str = ", ".join(
                f"{k} T: {v:.3f}%" for k, v in sorted(m.simulated_contrast_percent.items(), key=lambda kv: float(kv[0]))
            )
            lines.append(f"- **Simulated with-vs-without-metabolite contrast:** {contrast_str}")
        if m.detectability_robustness != "no" or m.robustness_note:
            lines += _md_prose(f"Robustness to input uncertainty: {m.detectability_robustness}", m.robustness_note)

        lines += _md_prose("Frequency-collision analysis", m.frequency_collision_analysis)
        lines += _md_prose("Direct water saturation note", m.direct_water_saturation_note)
        lines += _md_prose("Ex vivo/in vivo exchange-rate caveat", m.ex_vivo_in_vivo_exchange_caveat)

        if m.best_sequence_by_field:
            swept = any(s.derived_from == "bloch_mcconnell_sweep" for s in m.best_sequence_by_field)
            source = ("simulation-optimized" if swept else "**model-asserted, NOT swept**")
            lines.append(f"- **Optimized saturation sequence by field strength** ({source}):")
            for seq in sorted(m.best_sequence_by_field, key=lambda s: s.field_strength_T):
                lines.append(
                    f"  - {seq.field_strength_T:g} T: B1={seq.b1_uT:g} uT, "
                    f"Tsat={seq.saturation_time_ms:g} ms, {seq.duty_cycle_or_pulses} — {seq.rationale}"
                )
                if seq.derived_from == "bloch_mcconnell_sweep":
                    max_bit = (f" (max achievable at this B1: {seq.max_achievable_contrast_percent:.3f}%, "
                               "at a longer Tsat)" if abs(seq.max_achievable_contrast_percent
                               - seq.contrast_at_optimum_percent) > 1e-6 else "")
                    lines.append(
                        f"    - recommended sequence: {seq.contrast_at_optimum_percent:.3f}% "
                        f"contrast{max_bit}; no SAR ceiling applied"
                    )
        if m.b1_tsat_extraction_method_note:
            lines += _md_prose("B1/Tsat vs. extraction method", m.b1_tsat_extraction_method_note)
            for c in m.extraction_method_citations:
                lines.append(f"  - PMID {c.pmid}: {c.title}{_citation_tag(c)}")

        lines += clinical_markdown(m.clinical_evidence)

        if m.key_citations:
            lines.append("- **Key literature:**")
            for c in m.key_citations:
                year_str = f" ({c.year})" if c.year else ""
                lines.append(f"  - PMID {c.pmid}{year_str}: {c.title}{_citation_tag(c)}")

        if m.caveats:
            lines.append("- **Caveats / critique:**")
            lines += [f"  - {b}" for b in bullets(m.caveats)]
        lines.append("")

    if report.in_vivo_verification_plan:
        lines.extend(_render_verification_plan_markdown(report.in_vivo_verification_plan))

    lines.extend(_tissue_table_markdown(getattr(report, "tissue_parameters", [])))

    if report.provenance:
        p = report.provenance
        lines.extend(["## Run provenance", ""])
        version = f", cest_scout `{p.code_version}`" if p.code_version else ""
        lines.append(f"Started {p.started_at_utc}, finished {p.finished_at_utc} ({p.elapsed_s / 60:.1f} min{version}).")
        if p.settings:
            lines.append("")
            lines.append("Deterministic settings: " + "; ".join(f"{k} = {v}" for k, v in p.settings.items()))
        lines.append("")
        lines.append("| Agent | Model | Endpoint | Min | LLM calls | Prompt tok | Completion tok | Tool calls (err) | Nudges / JSON repairs / output retries | Completeness check |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for s in p.stages:
            check = "n/a" if s.output_check_passed is None else ("passed" if s.output_check_passed else "**FAILED**")
            lines.append(
                f"| {s.agent} | {s.model} | {s.endpoint} | {s.elapsed_s / 60:.1f} | {s.llm_calls} | "
                f"{s.prompt_tokens:,} | {s.completion_tokens:,} | {s.tool_calls} ({s.tool_errors}) | "
                f"{s.nudges} / {s.json_repairs} / {s.output_retries} | {check} |"
            )
        lines.append("")

    return "\n".join(lines)


def _render_verification_plan_markdown(plan: InVivoVerificationPlan) -> list[str]:
    plane = f" ({plan.imaging_plane})" if plan.imaging_plane else ""
    lines = [f"## In vivo verification: ROI-differential CEST scan{plane}", ""]
    lines += [f"- {b}" for b in bullets(plan.scan_rationale)] + [""]
    if plan.roi_set:
        lines.append("- **ROIs:**")
        for r in plan.roi_set:
            tag = " **[target tissue]**" if r.is_target_tissue else ""
            lines.append(f"  - {r.name}{tag}: {r.anatomical_definition}" + (f" ({r.role})" if r.role else ""))

    sp = plan.scan_protocol
    lines.append(
        f"- **Scan protocol:** {sp.field_strength_T:g} T, {sp.pulse_shape}, B1={sp.b1_uT:g} uT, "
        f"{sp.n_pulses} pulses x {sp.pulse_duration_ms:g} ms (interpulse delay "
        f"{sp.interpulse_delay_ms:g} ms, duty cycle {sp.duty_cycle_percent:g}%), total "
        f"Tsat={sp.total_saturation_time_ms:g} ms, TR={sp.tr_ms:g} ms"
    )
    lines += _md_prose("SAR", sp.sar_rationale, indent="  ")
    lines += _md_prose("Compromise rationale", sp.compromise_rationale, indent="  ")
    lines.append(f"- **Fat suppression:** {plan.fat_suppression}")
    lines.append(f"- **Motion mitigation:** {plan.motion_mitigation}")
    lines.append(f"- **Slice coverage:** {plan.slice_coverage}")
    lines.append(f"- **Structures in FOV:** {', '.join(plan.organs_in_fov)}")

    if plan.zspectrum_offsets:
        lines.append("- **Z-spectrum sampling offsets:**")
        for off in sorted(plan.zspectrum_offsets, key=lambda o: o.offset_ppm):
            lines.append(f"  - {off.offset_ppm:+.2f} ppm: {', '.join(off.targets)}")

    if plan.organ_predictions:
        lines.append("- **Predicted ROI-differential contrast (simulated by the pipeline at the chosen "
                      "compromise protocol, with each ROI's tabulated water T1/T2/MT and the reference-table "
                      "exchange rate):**")
        by_metabolite: dict[str, list] = {}
        for pred in plan.organ_predictions:
            by_metabolite.setdefault(pred.metabolite_name, []).append(pred)
        for name, preds in by_metabolite.items():
            contrast_str = ", ".join(
                f"{p.organ}: {p.predicted_contrast_percent:.3f}%"
                + (f" ({p.predicted_contrast_range_percent[0]:.3f}-{p.predicted_contrast_range_percent[1]:.3f}% over B1 "
                   f"{p.b1_scale_range[0]:g}-{p.b1_scale_range[1]:g}x)"
                   if len(p.predicted_contrast_range_percent) == 2 and p.b1_scale_range else "")
                for p in preds
            )
            lines.append(f"  - **{name}:** {contrast_str}")
            notes = {p.simulation_note for p in preds if p.simulation_note}
            for note in sorted(notes):
                head = "simulated as: " if not note.startswith("not simulated") else ""
                pieces = bullets(note, 50)
                lines.append(f"    - {head}{pieces[0]}")
                lines += [f"      - {b}" for b in pieces[1:]]
            for p in preds:
                for c in p.citations:
                    lines.append(f"    - [{p.organ}] PMID {c.pmid}: {c.title}{_citation_tag(c)}")

    lines += _md_prose("Differential analysis plan", plan.differential_analysis_plan)
    lines += _md_prose("Not actively targeted", plan.excluded_metabolites_note)
    if plan.caveats:
        lines.append("- **Caveats:**")
        lines += [f"  - {b}" for b in bullets(plan.caveats)]
    lines.append("")
    return lines
