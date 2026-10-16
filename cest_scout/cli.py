"""CLI: python -m cest_scout.cli "brain" [--output-dir outputs]"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from cest_scout.config import PROJECT_ROOT, load_settings
from cest_scout.pipeline import render_markdown, run_pipeline
from cest_scout.report.html import render_html


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.strip().lower()).strip("_")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find CEST-MRI-detectable metabolites for a body part.")
    parser.add_argument("body_part", help='e.g. "brain", "breast", "liver"')
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs"))
    parser.add_argument("--quiet", action="store_true", help="Suppress tool-call progress logging.")
    parser.add_argument(
        "--run-tag", default="",
        help="Label inserted into the output filenames (e.g. the model preset), so runs of the same "
             "tissue with different models can be told apart and compared with cest_scout.compare.",
    )
    args = parser.parse_args(argv)

    settings = load_settings()

    def log_tool_call(name: str, call_args: dict) -> None:
        if not args.quiet:
            print(f"  [tool] {name}({json.dumps(call_args)[:200]})", file=sys.stderr)

    def log_stage(stage: str) -> None:
        print(f"=== Agent stage: {stage} ===", file=sys.stderr)

    lit, chem, phys, experimentalist, clinical, final = run_pipeline(
        args.body_part, settings=settings, on_tool_call=log_tool_call, on_stage=log_stage,
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = f"_{_slug(args.run_tag)}" if args.run_tag else ""
    base = out_dir / f"{_slug(args.body_part)}{tag}_{stamp}"

    (base.with_suffix(".json")).write_text(json.dumps({
        "literature": lit.model_dump(),
        "chemist": chem.model_dump(),
        "physicist": phys.model_dump(),
        "experimentalist": experimentalist.model_dump(),
        "clinical": clinical.model_dump(),
        "final": final.model_dump(),
    }, indent=2))

    markdown = render_markdown(final)
    (base.with_suffix(".md")).write_text(markdown)

    html_report = render_html(final)
    (base.with_suffix(".html")).write_text(html_report)

    print(markdown)
    if final.provenance:
        print(f"\nRun took {final.provenance.elapsed_s / 60:.1f} min:", file=sys.stderr)
        for s in final.provenance.stages:
            print(
                f"  {s.agent:<16} {s.model:<22} {s.elapsed_s / 60:5.1f} min  {s.llm_calls:3d} LLM calls  "
                f"{s.prompt_tokens + s.completion_tokens:>8,} tokens  {s.tool_calls:3d} tool calls",
                file=sys.stderr,
            )
    print(f"\n(Full JSON + Markdown + HTML saved to {base}.json / {base}.md / {base}.html)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
