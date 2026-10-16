"""The no-tools baseline: ask the same served model the tissue question directly, once per
replicate, with no PubMed, no PubChem, no reference table and no simulation -- what a user gets
from the vanilla LLM. Everything the model claims is kept verbatim; afterwards, deterministically,
its PMIDs are checked against PubMed and each claimed proton group is looked up in the reference
table, so cest_scout.compare_vanilla can score the claims against the pipeline's runs.

    CEST_SCOUT_MODULE=cest_scout.vanilla bash hpc/run_with_server.sh <preset> <url> "<tissue>" --replicates 3
    python -m cest_scout.vanilla "myocardium" --run-tag deepseek-v4-flash --replicates 3 [--output-dir outputs]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from cest_scout.config import PROJECT_ROOT, load_settings
from cest_scout.llm import get_client
from cest_scout.pipeline import _code_version
from cest_scout.skills import exchange_reference, pubmed

SYSTEM_PROMPT = "You are an expert in CEST (chemical exchange saturation transfer) MRI and tissue biochemistry."

USER_PROMPT = """\
For human {tissue} in vivo, which endogenous metabolites or macromolecular pools could be detected \
with CEST MRI? Be specific and quantitative. Answer ONLY with JSON of this shape:
{{"metabolites": [{{
  "name": "...",
  "typical_concentration_mM": number,
  "exchangeable_group": "e.g. amide NH, guanidinium NH, hydroxyl OH, amine NH2",
  "group_class": "one of amide, guanidinium, amine, hydroxyl, imidazole, thiol, phosphate, rnoe, other",
  "chemical_shift_ppm": number (offset from water, negative for rNOE),
  "exchange_rate_per_s": number or null,
  "detectable_3T": true/false,
  "detectable_7T": true/false,
  "expected_cest_effect_percent_3T": number or null,
  "expected_cest_effect_percent_7T": number or null,
  "citations": [{{"pmid": "...", "title": "exact title"}}],
  "reasoning": "at most 40 words"
}}]}}
List 5-15 candidates, including ones you judge NOT detectable (with detectable_3T/7T false)."""


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text.strip().lower()).strip("_")


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)   # a fenced or prefixed answer: take the outermost object
        return json.loads(m.group(0)) if m else {"metabolites": [], "parse_error": True}


def _annotate(answer: dict, tissue: str) -> None:
    """Deterministic checks written next to the model's own claims, never replacing them."""
    mets = answer.get("metabolites") or []
    pairs = [(str(c.get("pmid", "")).strip(), str(c.get("title", ""))) for m in mets for c in (m.get("citations") or [])]
    try:
        verdicts = pubmed.verify_titles([p for p in pairs if p[0]]) if pairs else {}
    except Exception as exc:  # network unavailable: record it rather than fail the run
        verdicts = {}
        answer["citation_check_error"] = str(exc)
    for m in mets:
        for c in m.get("citations") or []:
            v = verdicts.get(str(c.get("pmid", "")).strip())
            c["verified"] = bool(v and v["verified"])
            c["real_title"] = (v or {}).get("real_title")
        cls = exchange_reference.classify_group(str(m.get("exchangeable_group", "")), str(m.get("group_class", "")))
        ref = exchange_reference.lookup(cls, str(m.get("name", "")), [], tissue=tissue)
        m["reference"] = {"group_class": cls, "shift_ppm": ref.shift_ppm if ref else None,
                          "kex_per_s": ref.kex_per_s if ref else None,
                          "kex_low_per_s": ref.kex_low_per_s if ref else None, "kex_high_per_s": ref.kex_high_per_s if ref else None}


def ask(client, model: str, tissue: str, extra_body: dict | None) -> tuple[dict, str, dict, float]:
    t0 = time.monotonic()
    kwargs = {"extra_body": extra_body} if extra_body else {}
    resp = client.chat.completions.create(
        model=model, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": USER_PROMPT.format(tissue=tissue)}],
        **kwargs,
    )
    text = resp.choices[0].message.content or ""
    usage = {"prompt_tokens": getattr(resp.usage, "prompt_tokens", 0), "completion_tokens": getattr(resp.usage, "completion_tokens", 0)}
    return _parse_json(text), text, usage, time.monotonic() - t0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="No-tools baseline: the served model answers the tissue question directly.")
    ap.add_argument("body_part")
    ap.add_argument("--run-tag", default="")
    ap.add_argument("--replicates", type=int, default=3)
    ap.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    settings = load_settings()
    client = get_client(settings.openai_api_key, settings.openai_base_url)
    kw = getattr(settings, "thinking_kwarg", "enable_thinking")
    extra_body = {"chat_template_kwargs": {kw: False}} if settings.disable_thinking else None
    model = settings.model_literature
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"_{_slug(args.run_tag)}" if args.run_tag else ""
    for i in range(args.replicates):
        answer, raw, usage, elapsed = ask(client, model, args.body_part, extra_body)
        _annotate(answer, args.body_part)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / f"vanilla_{_slug(args.body_part)}{tag}_{stamp}.json"
        path.write_text(json.dumps({
            "body_part": args.body_part, "model": model, "endpoint": urlparse(str(client.base_url)).netloc,
            "thinking_disabled": bool(extra_body), "code_version": _code_version(), "system_prompt": SYSTEM_PROMPT,
            "user_prompt": USER_PROMPT.format(tissue=args.body_part), "answer": answer, "raw": raw,
            "usage": usage, "elapsed_s": round(elapsed, 1), "replicate": i + 1,
        }, indent=2))
        if not args.quiet:
            n = len(answer.get("metabolites") or [])
            print(f"replicate {i + 1}/{args.replicates}: {n} candidates in {elapsed:.0f} s -> {path}", file=sys.stderr)
        time.sleep(1)   # distinct timestamps
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
