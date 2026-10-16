"""Re-run the deterministic simulation stage on a saved run with the CURRENT reference table.

    python -m cest_scout.resimulate outputs/liver_qwen3_8_27b_fp8_<stamp>.json [--out-dir outputs]

The agents' work -- candidates, concentrations, proton-group classes, organ concentrations, scan
protocol, critiques -- is taken from the saved JSON unchanged; only what the pipeline itself
computes is recomputed: reference-table k_ex/shift per group, the B1 x Tsat sweeps, detectability,
Z-spectra, and the organ-differential predictions. This is how runs made while the reference
table was still growing are compared on equal footing, and how a future table revision can be
applied to old runs without paying for the LLM stages again. The physicist's and
experimentalist's review TEXT is not updated and may refer to the earlier numbers; the report says
so. Output files carry a "_resim" tag and a resimulated_at_utc provenance stamp.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from cest_scout.agents import experimentalist_agent
from cest_scout.pipeline import (
    _apply_reference_simulations,
    _merge,
    _simulate_organ_predictions,
    pipeline_settings,
    render_markdown,
    run_reference_simulations,
    tissue_parameter_rows,
    tissue_parameters_note,
)
from cest_scout.report.html import render_html
from cest_scout.schemas import ChemistReport, ClinicalReport, InVivoVerificationPlan, LiteratureReport, PhysicistReport, RunProvenance
from cest_scout.skills import exchange_reference

_NOTE = ("[Re-simulated] The exchange rates, shifts, sweeps, detectability verdicts and organ predictions in "
         "this report were recomputed offline from the saved agent outputs with a newer reference table; the "
         "review text written by the physicist and experimentalist agents predates that and may refer to the "
         "earlier numbers.")


def resimulate(path: Path, out_dir: Path) -> Path:
    data = json.loads(path.read_text())
    lit = LiteratureReport.model_validate(data["literature"])
    chem = ChemistReport.model_validate(data["chemist"])
    phys = PhysicistReport.model_validate(data["physicist"])
    plan = InVivoVerificationPlan.model_validate(data["experimentalist"]) if data.get("experimentalist") else None
    clinical = ClinicalReport.model_validate(data["clinical"]) if data.get("clinical") else None
    old_final = data.get("final") or {}

    sims = run_reference_simulations(lit, chem, lit.body_part)
    _apply_reference_simulations(phys, sims)
    if plan is not None:
        # Strip the earlier "[Not simulated]" caveat so it is not duplicated by the new run.
        plan.caveats = plan.caveats.split(" [Not simulated]")[0].strip()
        _simulate_organ_predictions(plan, sims, experimentalist_agent.SCAN_FIELD_STRENGTH_T)
        plan.caveats = f"{plan.caveats} {_NOTE}".strip()
    final = _merge(lit, chem, phys, sims, plan, clinical)
    final.tissue_parameters_note = tissue_parameters_note(lit.body_part)
    final.tissue_parameters = tissue_parameter_rows(lit.body_part, plan)
    if old_final.get("provenance"):
        prov = RunProvenance.model_validate(old_final["provenance"])
        prov.resimulated_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        prov.reference_table = exchange_reference.table_version()
        prov.settings = pipeline_settings()   # the settings of the RE-simulation, which produced these numbers
        final.provenance = prov

    stem = path.stem
    tag_pos = stem.rfind("_")
    base = out_dir / (f"{stem[:tag_pos]}_resim{stem[tag_pos:]}" if tag_pos > 0 else f"{stem}_resim")
    out_dir.mkdir(parents=True, exist_ok=True)
    base.with_suffix(".json").write_text(json.dumps({
        "literature": lit.model_dump(), "chemist": chem.model_dump(), "physicist": phys.model_dump(),
        "experimentalist": plan.model_dump() if plan else None, "clinical": clinical.model_dump() if clinical else None,
        "final": final.model_dump(),
    }, indent=2))
    base.with_suffix(".md").write_text(render_markdown(final))
    base.with_suffix(".html").write_text(render_html(final))
    return base


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Re-run the deterministic simulation stage on saved runs.")
    ap.add_argument("runs", nargs="+", help="Run JSON files written by cest_scout.cli")
    ap.add_argument("--out-dir", default="outputs")
    args = ap.parse_args(argv)
    for p in args.runs:
        base = resimulate(Path(p), Path(args.out_dir))
        print(f"{p} -> {base}.json / .md / .html", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
