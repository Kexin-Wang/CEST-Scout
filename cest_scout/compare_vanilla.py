"""Score the no-tools baseline (cest_scout.vanilla) against the pipeline's runs for one tissue.

    python -m cest_scout.compare_vanilla --pipeline <run.json ...> --vanilla <vanilla.json ...> --out outputs/compare_<slug>_vanilla_<tag>

Candidates are matched across both with the same grouping the model comparison uses. For every
candidate the page shows how many pipeline runs proposed and simulated it detectable, and what
each vanilla model claimed: detectability, CEST effect, chemical shift and exchange rate -- each
claim next to the reference-table value the pipeline simulates with -- plus whether its PMIDs
exist. The vanilla model has no simulation, so every "effect %" it states is a number it could
not have computed.
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace as NS

from cest_scout.compare import _STYLE, _Groups, _status, _units_html, load_run


def _num(x) -> float | None:
    try:
        return None if x is None or isinstance(x, bool) else float(x)
    except (TypeError, ValueError):
        return None


def _median(values) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _g(x, unit="") -> str:
    return "—" if x is None else f"{round(x, 2):g}{unit}"


def load_vanilla(path: Path) -> NS:
    d = json.loads(path.read_text())
    mets = [m for m in (d.get("answer") or {}).get("metabolites") or [] if isinstance(m, dict) and m.get("name")]
    return NS(path=path, model=d.get("model", path.stem), mets=mets, elapsed_s=d.get("elapsed_s") or 0.0,
              usage=d.get("usage") or {}, thinking_disabled=d.get("thinking_disabled"),
              parse_error=bool((d.get("answer") or {}).get("parse_error")),
              lit=NS(metabolites=[NS(name=m["name"], aliases=[]) for m in mets]))


def build_tables(pipeline, vanilla) -> list[tuple[str, list[str], list[list[str]], str]]:
    groups = _Groups(pipeline + vanilla)
    models = sorted({v.model for v in vanilla})
    by_model = {mdl: [v for v in vanilla if v.model == mdl] for mdl in models}

    # --- pipeline per candidate key; a run counts once per key (two GAG candidates in one run
    # are one row entry: detectable if either is, unknown only if both are)
    p_found, p_det, p_unknown, p_best, p_conc = Counter(), Counter(), Counter(), defaultdict(list), defaultdict(list)
    for r in pipeline:
        conc = {m.name: m.typical_concentration_mM for m in r.lit.metabolites}
        per_key: dict[str, list] = defaultdict(list)
        for m in r.final.metabolites:
            per_key[groups.key(m.name)].append(m)
        for k, ms in per_key.items():
            statuses = [_status(m) for m in ms]
            p_found[k] += 1
            p_det[k] += "detectable" in statuses
            p_unknown[k] += all(st == "unknown" for st in statuses)
            best = [max(m.simulated_contrast_percent.values()) for m in ms if m.simulated_contrast_percent]
            if best:
                p_best[k].append(max(best))
            cs = [float(conc[m.name]) for m in ms if conc.get(m.name) is not None]
            if cs:
                p_conc[k].append(max(cs))
    n_p = len(pipeline)
    consensus = {k for k, n in p_found.items() if n >= max(2, n_p / 2) and p_det[k] >= n / 2}

    # --- vanilla per model per key: a replicate counts once per key; every claim is kept as a
    # member record with its class, so shift and rate are compared within the modal class only
    v = {mdl: defaultdict(lambda: {"found": 0, "d3": 0, "d7": 0, "e3": [], "e7": [], "conc": [], "members": []}) for mdl in models}
    cites = {mdl: {"total": 0, "verified": 0} for mdl in models}
    for mdl in models:
        for run in by_model[mdl]:
            per_key: dict[str, list] = defaultdict(list)
            for m in run.mets:
                per_key[groups.key(m["name"])].append(m)
                for c in m.get("citations") or []:
                    cites[mdl]["total"] += 1; cites[mdl]["verified"] += bool(c.get("verified"))
            for k, ms in per_key.items():
                e = v[mdl][k]
                e["found"] += 1; e["d3"] += any(m.get("detectable_3T") for m in ms); e["d7"] += any(m.get("detectable_7T") for m in ms)
                e["e3"].append(_median(_num(m.get("expected_cest_effect_percent_3T")) for m in ms))
                e["e7"].append(_median(_num(m.get("expected_cest_effect_percent_7T")) for m in ms))
                e["conc"].append(_median(_num(m.get("typical_concentration_mM")) for m in ms))
                for m in ms:
                    ref = m.get("reference") or {}
                    e["members"].append({"cls": ref.get("group_class") or m.get("group_class") or "?",
                                         "ppm": _num(m.get("chemical_shift_ppm")), "kex": _num(m.get("exchange_rate_per_s")),
                                         "ref_ppm": _num(ref.get("shift_ppm")), "ref_kex": _num(ref.get("kex_per_s")),
                                         "ref_lo": _num(ref.get("kex_low_per_s")), "ref_hi": _num(ref.get("kex_high_per_s"))})

    def modal(e):
        """The candidate's members in its most frequent proton class, and that class."""
        cls = Counter(m["cls"] for m in e["members"]).most_common(1)[0][0]
        return [m for m in e["members"] if m["cls"] == cls], cls
    tables = []

    # Table 1: the runs
    rows = []
    for mdl in models:
        for run in sorted(by_model[mdl], key=lambda r: r.path.name):
            n_c = sum(len(m.get("citations") or []) for m in run.mets); n_v = sum(bool(c.get("verified")) for m in run.mets for c in (m.get("citations") or []))
            rows.append([f"vanilla {mdl}", run.path.name, str(len(run.mets)), f"{run.elapsed_s / 60:.1f}",
                         f"{run.usage.get('prompt_tokens', 0) + run.usage.get('completion_tokens', 0):,}", f"{n_v}/{n_c}",
                         "JSON repaired" if run.parse_error else ""])
    for r in pipeline:
        rows.append([f"pipeline {r.label}", r.path.name, str(r.n_candidates), f"{r.elapsed_min or 0:.0f}",
                     f"{r.prompt_tokens + r.completion_tokens:,}", f"{r.n_citations_kept}/{r.n_citations_kept + r.n_citations_dropped}", ""])
    tables.append(("Runs compared", ["Run", "File", "Candidates", "Minutes", "Tokens", "PMIDs real/claimed", "Note"], rows,
                   "A vanilla run is one chat completion with no tools; a pipeline run is the four agents with PubMed, PubChem, "
                   "the reference tables and the Bloch-McConnell sweep. 'PMIDs real' = the PMID resolves on PubMed to the claimed title."))

    # Table 2: candidates
    keys = sorted(set(p_found) | {k for mdl in models for k in v[mdl]},
                  key=lambda k: (-p_found[k], -sum(v[mdl][k]["found"] for mdl in models if k in v[mdl]), groups.name_for(k).lower()))
    headers = ["Metabolite", f"Pipeline found /{n_p}", "Pipeline detectable", "Pipeline best contrast (median)", "mM pipeline"]
    for mdl in models:
        headers += [f"{mdl}: found /{len(by_model[mdl])}", f"{mdl}: says detectable 3 T / 7 T", f"{mdl}: claimed effect 3 T / 7 T",
                    f"{mdl}: claimed ppm → reference", f"{mdl}: claimed k_ex → reference", f"{mdl}: mM"]
    rows = []
    for k in keys:
        pb = _median(p_best[k])
        row = [groups.name_for(k), str(p_found[k]) if p_found[k] else "—",
               (f"{p_det[k]}/{p_found[k]}" + (f" (unknown {p_unknown[k]})" if p_unknown[k] else "")) if p_found[k] else "—",
               _g(round(pb, 2) if pb is not None else None, " %"), _g(_median(p_conc[k]))]
        for mdl in models:
            e = v[mdl].get(k)
            if not e:
                row += ["—"] * 6; continue
            ms, cls = modal(e)
            ref_ppm, ref_kex = _median(m["ref_ppm"] for m in ms), _median(m["ref_kex"] for m in ms)
            lo, hi = _median(m["ref_lo"] for m in ms), _median(m["ref_hi"] for m in ms)
            row += [f"{e['found']}", f"{e['d3']} / {e['d7']}",
                    f"{_g(_median(e['e3']), ' %')} / {_g(_median(e['e7']), ' %')}",
                    f"{_g(_median(m['ppm'] for m in ms))} → {_g(ref_ppm)} [{cls}]",
                    f"{_g(_median(m['kex'] for m in ms))} → {_g(ref_kex)}" + (f" ({_g(lo)}-{_g(hi)})" if lo is not None and hi != lo else ""),
                    _g(_median(e["conc"]))]
        rows.append(row)
    tables.append(("Candidates: pipeline consensus vs the vanilla answer", headers, rows,
                   "Pipeline detectable = runs whose simulation reached the contrast threshold at some field. The vanilla model states an "
                   "effect it has no simulation to compute. 'reference' is the value the pipeline simulates with for that proton class "
                   "(reference table; '—' = the table has no verified value, so the pipeline reports unknown)."))

    # Table 3: agreement
    rows = []
    for mdl in models:
        vk = set(v[mdl]); shared = [k for k in vk if p_found[k]]
        judged = [k for k in shared if p_det[k] + (p_found[k] - p_det[k] - p_unknown[k]) > 0]  # pipeline simulated it
        agree = sum(1 for k in judged if (v[mdl][k]["d7"] * 2 >= v[mdl][k]["found"]) == (p_det[k] * 2 >= p_found[k] - p_unknown[k]))
        only_v = [groups.name_for(k) for k in vk if not p_found[k]]
        missed = [groups.name_for(k) for k in consensus if k not in vk]
        members = [m for k in vk for m in modal(v[mdl][k])[0]]
        dppm = [abs(m["ppm"] - m["ref_ppm"]) for m in members if m["ppm"] is not None and m["ref_ppm"] is not None]
        kex_pairs = [(m["kex"], m["ref_lo"], m["ref_hi"]) for m in members if m["kex"] is not None and m["ref_lo"] is not None]
        kex_ok = sum(1 for a, lo, hi in kex_pairs if (lo or 0) / 3 <= a <= (hi or lo) * 3)
        same_model = [r for r in pipeline if mdl in r.models]
        p_kept, p_drop = sum(r.n_citations_kept for r in same_model), sum(r.n_citations_dropped for r in same_model)
        rows.append([mdl, f"{len(shared)}/{len(vk)}", f"{agree}/{len(judged)}", "; ".join(sorted(only_v)) or "none",
                     "; ".join(sorted(missed)) or "none", _g(round(_median(dppm), 2) if dppm else None, " ppm") + f" (n={len(dppm)})",
                     f"{kex_ok}/{len(kex_pairs)}", f"{cites[mdl]['verified']}/{cites[mdl]['total']}",
                     f"{p_kept}/{p_kept + p_drop}" if same_model else "—"])
    tables.append(("Agreement per vanilla model", ["Vanilla model", "Candidates shared with the pipeline", "Detectability verdicts agreeing (7 T vs simulated)",
                   "Candidates only the vanilla model proposed", "Pipeline-consensus candidates it never proposed",
                   "Median shift error vs reference (ppm)", "Claimed k_ex within 3× of the reference range", "Vanilla PMIDs real/claimed",
                   "Same model in the pipeline: citations kept/claimed"], rows,
                   "A verdict agrees when the vanilla model's majority 7 T claim matches the pipeline's majority simulated verdict for that candidate "
                   "(candidates the pipeline could not simulate are left out). Pipeline-consensus = proposed by at least half the pipeline runs and "
                   "detectable in at least half of those."))
    return tables


def render_markdown(body_part: str, tables, pipeline, vanilla) -> str:
    out = [f"# Vanilla model vs CEST_Scout pipeline: {body_part}", "",
           f"Pipeline runs: {len(pipeline)}; vanilla answers: {len(vanilla)} ({', '.join(sorted({v.model for v in vanilla}))}).", ""]
    for title, headers, rows, note in tables:
        out += [f"## {title}", "", "| " + " | ".join(h.replace("|", "/") for h in headers) + " |", "|" + "---|" * len(headers)]
        out += ["| " + " | ".join(str(c).replace("|", "/") for c in row) + " |" for row in rows]
        out += ["", note, ""]
    return "\n".join(out)


def render_html(body_part: str, tables, pipeline, vanilla) -> str:
    esc = lambda s: _html.escape(str(s), quote=True)  # noqa: E731
    parts = ["<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
             f"<title>Vanilla model vs CEST_Scout: {esc(body_part)}</title>", _STYLE, "</head><body>",
             f"<h1>Vanilla model vs CEST_Scout pipeline: {esc(body_part)}</h1>",
             f"<p>Pipeline runs: {len(pipeline)}; vanilla answers: {len(vanilla)} ({esc(', '.join(sorted({v.model for v in vanilla})))}). "
             "The vanilla answer is the served model asked the tissue question directly, no tools, thinking off as in the pipeline.</p>"]
    for title, headers, rows, note in tables:
        parts.append(f"<h2>{esc(title)}</h2><table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in headers) + "</tr>")
        for row in rows:
            cells = []
            for c in row:
                c = _units_html(esc(c))
                c = re.sub(r"^(\d+) / (\d+)$", r"\1 / \2", c)
                cells.append(f"<td>{c}</td>")
            parts.append("<tr>" + "".join(cells) + "</tr>")
        parts.append(f"</table><p class='note'>{esc(note)}</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score the no-tools baseline against the pipeline's runs for one tissue.")
    ap.add_argument("--pipeline", nargs="+", required=True)
    ap.add_argument("--vanilla", nargs="+", required=True)
    ap.add_argument("--out", required=True, help="Output prefix; writes <out>.md and <out>.html")
    args = ap.parse_args(argv)
    pipeline = [load_run(Path(p)) for p in args.pipeline]
    vanilla = [load_vanilla(Path(p)) for p in args.vanilla]
    body_part = pipeline[0].final.body_part if pipeline and pipeline[0].final else ""
    tables = build_tables(pipeline, vanilla)
    out = Path(args.out)
    out.with_suffix(".md").write_text(render_markdown(body_part, tables, pipeline, vanilla))
    out.with_suffix(".html").write_text(render_html(body_part, tables, pipeline, vanilla))
    print(f"(Written to {out}.md and {out}.html)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
