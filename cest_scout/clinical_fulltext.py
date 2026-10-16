"""Upgrade a saved run's clinical evidence with the full-text mode, offline (needs the LLM server,
not the whole pipeline): re-verify, read PubMed Central full texts for studies whose statistics
are incomplete, re-count, re-pool, re-attach to the final report and re-render in place.

    python -m cest_scout.clinical_fulltext outputs/<run>.json [...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cest_scout.agents import clinical_agent
from cest_scout.config import load_settings
from cest_scout.llm import get_client, new_run_stats
from cest_scout.pipeline import render_markdown
from cest_scout.report.html import render_html
from cest_scout.schemas import ClinicalReport, FinalReport


def upgrade(path: Path) -> dict:
    settings = load_settings()
    client = get_client(settings.openai_api_key, settings.openai_base_url)
    kw = getattr(settings, "thinking_kwarg", "enable_thinking")
    extra_body = {"chat_template_kwargs": {kw: False}} if settings.disable_thinking else None
    data = json.loads(path.read_text())
    if not data.get("clinical"):
        raise SystemExit(f"{path}: no clinical report in this run")
    clinical = ClinicalReport.model_validate(data["clinical"])
    stats = new_run_stats()
    clinical_agent.verify(clinical)
    clinical_agent.enrich_from_fulltext(clinical, client, settings.model_literature, extra_body=extra_body, stats_sink=stats)
    final = FinalReport.model_validate(data["final"])
    by_key = {e.metabolite_name.strip().lower(): e for e in clinical.entries}
    for m in final.metabolites:
        m.clinical_evidence = by_key.get(m.name.strip().lower())
    data["clinical"], data["final"] = clinical.model_dump(), final.model_dump()
    path.write_text(json.dumps(data, indent=2))
    path.with_suffix(".md").write_text(render_markdown(final))
    path.with_suffix(".html").write_text(render_html(final))
    filled = sum(1 for e in clinical.entries for s in e.studies if s.stats_source.startswith("full text"))
    pooled = sum(len(e.pooled_effects) for e in clinical.entries)
    return {"studies": sum(len(e.studies) for e in clinical.entries), "filled_from_fulltext": filled, "pooled_effects": pooled,
            "llm_calls": stats["llm_calls"], "tokens": stats["prompt_tokens"] + stats["completion_tokens"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Full-text mode for a saved run's clinical evidence.")
    ap.add_argument("runs", nargs="+")
    args = ap.parse_args(argv)
    for r in args.runs:
        print(f"{r}: {upgrade(Path(r))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
