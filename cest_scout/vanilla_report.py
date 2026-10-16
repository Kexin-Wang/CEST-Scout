"""Individual HTML report for the no-tools baseline: one page per served model and tissue showing
each answer in full -- every candidate with the model's own numbers beside the reference-table
value the pipeline simulates with, each cited PMID checked against PubMed and linked, and the raw
prompt and answer -- so a vanilla answer can be read the way a pipeline report is.

    python -m cest_scout.vanilla_report outputs/vanilla_20261008/vanilla_<tissue>_<model>_*.json --out outputs/vanilla_<tissue>_<model>_20261008
"""
from __future__ import annotations

import argparse
import html as _html
import json
from pathlib import Path

from cest_scout.compare import _STYLE


def _esc(x) -> str:
    return _html.escape("" if x is None else str(x), quote=True)


def _g(x) -> str:
    try:
        return "&mdash;" if x is None else f"{float(x):g}"
    except (TypeError, ValueError):
        return _esc(x)


def render(answers: list[dict]) -> str:
    first = answers[0]
    model, tissue = first.get("model", "?"), first.get("body_part", "?")
    parts = ["<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
             f"<title>Vanilla {_esc(model)} on {_esc(tissue)}</title>", _STYLE, "</head><body>",
             f"<h1>Vanilla {_esc(model)} on {_esc(tissue)}: {len(answers)} direct answers, no tools</h1>",
             "<p>The served model was asked the tissue question once per answer (no PubMed, PubChem, reference table or "
             "simulation; thinking off as in the pipeline). Its claims are shown verbatim; afterwards each PMID was checked "
             "against PubMed and each proton class looked up in the reference table the pipeline simulates with.</p>"]
    for i, a in enumerate(sorted(answers, key=lambda d: d.get("replicate", 0)), start=1):
        mets = (a.get("answer") or {}).get("metabolites") or []
        n_c = sum(len(m.get("citations") or []) for m in mets)
        n_v = sum(bool(c.get("verified")) for m in mets for c in (m.get("citations") or []))
        parts.append(f"<h2>Answer {i}</h2><div class='card'><p>{len(mets)} candidates; PMIDs real / cited: <b>{n_v} / {n_c}</b>; "
                     f"{a.get('elapsed_s', 0):.0f} s; {a.get('usage', {}).get('prompt_tokens', 0) + a.get('usage', {}).get('completion_tokens', 0):,} tokens; "
                     f"code {_esc(a.get('code_version', ''))}" + (" &mdash; <span class='no'>JSON repaired</span>" if (a.get("answer") or {}).get("parse_error") else "") + "</p>")
        parts.append("<table class='citations'><thead><tr><th>Candidate</th><th>mM</th><th>Group (class)</th><th>Claimed ppm &rarr; reference</th>"
                     "<th>Claimed k<sub>ex</sub> &rarr; reference (s<sup>-1</sup>)</th><th>Says detectable 3 T / 7 T</th><th>Claimed effect 3 T / 7 T</th>"
                     "<th>Citations</th><th>Reasoning</th></tr></thead><tbody>")
        for m in mets:
            ref = m.get("reference") or {}
            cls = ref.get("group_class") or m.get("group_class") or "?"
            kex_ref = _g(ref.get("kex_per_s")) + (f" ({_g(ref.get('kex_low_per_s'))}&ndash;{_g(ref.get('kex_high_per_s'))})"
                                                  if ref.get("kex_low_per_s") is not None and ref.get("kex_high_per_s") != ref.get("kex_low_per_s") else "")
            cites = []
            for c in m.get("citations") or []:
                ok = bool(c.get("verified"))
                mark = "<span class='yes'>&#10003;</span>" if ok else "<span class='no'>&#10007;</span>"
                real = f"<br><span class='verify-note'>PubMed: {_esc(c.get('real_title'))}</span>" if (not ok and c.get("real_title")) else ""
                cites.append(f"{mark} <a href='https://pubmed.ncbi.nlm.nih.gov/{_esc(c.get('pmid'))}/' target='_blank' rel='noopener'>PMID {_esc(c.get('pmid'))}</a> "
                             f"<span style='color:#444;'>{_esc(c.get('title'))}</span>{real}")
            det = lambda v: "<span class='yes'>yes</span>" if v else "<span class='no'>no</span>"  # noqa: E731
            parts.append(f"<tr><td>{_esc(m.get('name'))}</td><td>{_g(m.get('typical_concentration_mM'))}</td>"
                         f"<td>{_esc(m.get('exchangeable_group'))} <span style='color:#666;'>({_esc(cls)})</span></td>"
                         f"<td>{_g(m.get('chemical_shift_ppm'))} &rarr; {_g(ref.get('shift_ppm'))}</td>"
                         f"<td>{_g(m.get('exchange_rate_per_s'))} &rarr; {kex_ref}</td>"
                         f"<td>{det(m.get('detectable_3T'))} / {det(m.get('detectable_7T'))}</td>"
                         f"<td>{_g(m.get('expected_cest_effect_percent_3T'))} % / {_g(m.get('expected_cest_effect_percent_7T'))} %</td>"
                         f"<td style='font-size:.82rem;'>{'<br>'.join(cites) or '&mdash;'}</td><td style='font-size:.82rem;'>{_esc(m.get('reasoning'))}</td></tr>")
        parts.append("</tbody></table>")
        parts.append(f"<details><summary>Prompt and raw answer</summary><pre style='white-space:pre-wrap;font-size:.78rem;'>{_esc(a.get('user_prompt'))}</pre>"
                     f"<pre style='white-space:pre-wrap;font-size:.78rem;'>{_esc(a.get('raw'))}</pre></details></div>")
    parts.append("<p class='note'>A reference of &mdash; means the table has no verified value for that class, so the pipeline would report "
                 "it unknown. &#10007; = the PMID resolves to a different paper on PubMed (or none).</p></body></html>")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Individual HTML report for one model x tissue of the no-tools baseline.")
    ap.add_argument("answers", nargs="+", help="vanilla_<tissue>_<model>_<stamp>.json files of one model and tissue")
    ap.add_argument("--out", required=True, help="output path without extension")
    args = ap.parse_args(argv)
    answers = [json.loads(Path(p).read_text()) for p in args.answers]
    out = Path(args.out).with_suffix(".html")
    out.write_text(render(answers))
    print(f"(Written to {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
