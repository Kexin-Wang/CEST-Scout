"""Merge abstract-verified tissue relaxation/MT records into cest_scout/data/tissue_reference.json.

    python -m cest_scout.skills.build_tissue_reference records.json

`records.json` is the reference-hunting workflow's output: a list of {"group", "found": {"records":
[...]}, "verify": {"verdicts": [...]}} objects, each record naming a PMID, the exact title, a
VERBATIM abstract sentence, the tissue, the parameter (T1 | T2 | MT_fraction | MT_exchange_rate |
MT_T2b), the value text with its unit, the field strength and the measurement context. As with the
exchange-rate table, nothing is trusted: the PMID must resolve to the claimed title, the quote must
occur in the abstract, the number is parsed conservatively and converted to SI, and skeptic-refuted
records are excluded. Existing entries of the table (including the legacy organ values labelled
"not abstract-verified") are kept; the lookup prefers abstract-verified entries when both exist.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from cest_scout.skills import pubmed
from cest_scout.skills.build_exchange_reference import parse_number
from cest_scout.skills.tissue_reference import DATA_PATH, PARAMETERS

# Tissue names as the abstracts tend to write them -> canonical name + aliases the lookup matches.
_CANONICAL = (
    ("nucleus pulposus", ["np", "disc nucleus", "intervertebral disc nucleus"], r"nucleus pulposus|\bnp\b"),
    ("annulus fibrosus", ["af", "anulus fibrosus", "disc annulus"], r"annulus fibrosus|anulus fibrosus|\baf\b"),
    ("intervertebral disc", ["disc", "lumbar disc", "cervical disc"], r"intervertebral disc|\bdisc\b"),
    ("vertebral body marrow", ["vertebral bone marrow", "bone marrow", "vertebral body"], r"vertebral|bone marrow"),
    ("spinal cord", ["cord", "cervical cord"], r"spinal cord"),
    ("cerebrospinal fluid", ["csf"], r"cerebrospinal|\bcsf\b"),
    # Before the generic muscle pattern: "cardiac muscle" is the heart, not paraspinal muscle.
    ("myocardium", ["heart", "left ventricular myocardium", "lv myocardium", "cardiac muscle", "heart muscle",
                    "interventricular septum"], r"myocard|heart|cardiac|septum"),
    ("paraspinal muscle", ["skeletal muscle", "erector spinae", "multifidus", "psoas", "muscle"], r"muscle"),
    ("renal medulla", ["kidney medulla"], r"medulla"),
    ("renal cortex", ["kidney", "renal cortex (kidney)", "kidney cortex", "renal"], r"renal cortex|kidney|renal"),
    ("liver", ["liver parenchyma", "hepatic parenchyma", "hepatic"], r"liver|hepatic"),
    ("spleen", ["splenic parenchyma", "splenic"], r"spleen|splenic"),
    ("pancreas", ["pancreatic"], r"pancrea"),
    ("articular cartilage", ["cartilage", "knee cartilage", "hip cartilage"], r"cartilage"),
    ("meniscus", [], r"meniscus"),
    ("tendon", [], r"tendon"),
    # Fibroglandular tissue is what "breast" means for a CEST target (fat is its own compartment).
    ("breast fibroglandular tissue", ["breast", "fibroglandular tissue", "breast parenchyma", "glandular breast tissue"],
     r"fibroglandular|breast parenchyma|glandular breast"),
    ("breast adipose tissue", ["breast fat"], r"breast.*(adipose|fat)|(adipose|fat).*breast"),
    ("white matter", ["wm", "brain white matter"], r"white matter|\bwm\b"),
    ("gray matter", ["grey matter", "gm", "brain gray matter"], r"gr[ae]y matter|\bgm\b"),
    ("brain", ["cerebral"], r"brain"),
)


def canonical_tissue(text: str) -> tuple[str, list[str]] | None:
    lowered = text.lower()
    for name, aliases, pattern in _CANONICAL:
        if re.search(pattern, lowered):
            return name, aliases
    return None


def to_si(parameter: str, value: float, value_text: str) -> float | None:
    t = value_text.lower()
    if parameter in ("T1", "T2", "T1rho"):
        if re.search(r"\bms\b|millisecond", t) or (value > 20 and not re.search(r"\bs\b", t.replace("ms", ""))):
            return value / 1000.0
        return value
    if parameter == "MT_fraction":
        return value / 100.0 if ("%" in t or value > 1.0) else value
    if parameter == "MT_exchange_rate":
        return value
    if parameter == "MT_T2b":
        if re.search(r"ms|millisecond", t):
            return value / 1000.0
        if re.search(r"µs|us\b|microsecond|μs", t) or value > 1:
            return value / 1e6
        return value
    return None


def build(groups: list[dict], existing: dict) -> dict:
    records = []
    for g in groups:
        found = g.get("found") or {}
        verdicts = {}
        for v in ((g.get("verify") or {}).get("verdicts") or []):
            verdicts[(v.get("pmid"), (v.get("tissue") or "").lower(), (v.get("parameter") or "").upper())] = v
        for r in found.get("records", []):
            r = dict(r)
            r["_verdict"] = verdicts.get((r.get("pmid"), (r.get("tissue") or "").lower(), (r.get("parameter") or "").upper()))
            records.append(r)

    pairs = [(str(r["pmid"]), r.get("title", "")) for r in records if r.get("pmid")]
    title_verdicts = pubmed.verify_titles(pairs) if pairs else {}

    entries, dropped = list(existing.get("entries", [])), list(existing.get("dropped", []))
    seen = {(e["pmid"], e["tissue"], e["parameter"], e.get("field_T"), e["value_si"]) for e in entries}
    quote_cache: dict[tuple[str, str], bool] = {}
    for r in records:
        pmid = str(r.get("pmid", "")).strip()
        tv = title_verdicts.get(pmid) or {}
        title_ok = bool(tv.get("verified"))
        key = (pmid, r.get("quote", ""))
        if key not in quote_cache:
            try:
                quote_cache[key] = bool(pubmed.verify_quote(pmid, r.get("quote", "")).get("verified")) if pmid else False
            except Exception:
                quote_cache[key] = False
        quote_ok = quote_cache[key]
        param = (r.get("parameter") or "").strip()
        param = {"T1RHO": "T1rho", "MT_FRACTION": "MT_fraction", "MT_EXCHANGE_RATE": "MT_exchange_rate", "MT_T2B": "MT_T2b"}.get(param.upper(), param)
        value, value_range, note = parse_number(r.get("value", ""))
        field_val, _, _ = parse_number(r.get("field_strength_T", ""))
        canon = canonical_tissue(r.get("tissue", ""))
        skeptic = r.get("_verdict") or {}
        si = to_si(param, value, r.get("value", "")) if value is not None else None
        entry = {
            "tissue": canon[0] if canon else (r.get("tissue") or "").strip().lower(),
            "aliases": canon[1] if canon else [],
            "tissue_text": r.get("tissue", ""),
            "parameter": param, "field_T": field_val, "value_si": si, "value_text": r.get("value", ""), "value_note": note,
            "species": _species(r.get("conditions", "")), "measurement_type": r.get("measurement_type", ""),
            "conditions": r.get("conditions", ""), "pmid": pmid, "title": tv.get("real_title") or r.get("title", ""),
            "year": r.get("year", ""), "quote": r.get("quote", ""), "notes": r.get("notes", ""),
            "verification": {"title_verified": title_ok, "quote_verified": quote_ok,
                             "skeptic_refuted": bool(skeptic.get("refuted")), "skeptic_reason": skeptic.get("reason", "")},
        }
        reasons = []
        if not title_ok:
            reasons.append("PMID does not resolve to the claimed title")
        if not quote_ok:
            reasons.append("quote not found in the abstract")
        if skeptic.get("refuted"):
            reasons.append(f"refuted by the skeptic: {skeptic.get('reason', '')}")
        if param not in PARAMETERS:
            reasons.append(f"parameter {param!r} is not used by the simulation")
        if si is None:
            reasons.append(f"no usable number ({note})")
        if param in ("T1", "T2") and field_val is None:
            reasons.append("field strength not stated")
        if not entry["tissue"]:
            reasons.append("tissue not recognised")
        sig = (pmid, entry["tissue"], param, field_val, si)
        if not reasons and sig in seen:
            reasons.append("duplicate of an existing entry")
        if reasons:
            entry["dropped_because"] = reasons
            dropped.append(entry)
        else:
            seen.add(sig)
            entries.append(entry)

    return {
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": ("Legacy organ values carried over with their limitations stated, plus numbers stated in PubMed "
                   "abstracts found by literature agents and verified deterministically (PMID -> real title, quoted "
                   "sentence present in the abstract, conservative number parsing, skeptic-refuted records excluded)."),
        "n_entries": len(entries), "n_dropped": len(dropped), "entries": entries, "dropped": dropped,
    }


def _species(conditions: str) -> str:
    lowered = conditions.lower()
    for s, out in (("human", "human"), ("rat", "rat"), ("mouse", "mouse"), ("murine", "mouse"), ("bovine", "bovine"),
                   ("porcine", "pig"), ("pig", "pig"), ("canine", "dog"), ("rabbit", "rabbit"), ("ovine", "sheep")):
        if s in lowered:
            return out
    return ""


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    groups = json.loads(Path(argv[0]).read_text())
    existing = json.loads(DATA_PATH.read_text()) if DATA_PATH.exists() else {}
    table = build(groups, existing)
    out = Path(argv[1]) if len(argv) > 1 else DATA_PATH
    out.write_text(json.dumps(table, indent=1, ensure_ascii=False))
    by_tissue: dict[str, int] = {}
    for e in table["entries"]:
        by_tissue[e["tissue"]] = by_tissue.get(e["tissue"], 0) + 1
    print(f"{table['n_entries']} entries ({by_tissue}), {table['n_dropped']} dropped -> {out}")
    for d in table["dropped"][-40:]:
        if "dropped_because" in d and d["pmid"]:
            print(f"  dropped PMID {d['pmid']} [{d['tissue']} {d['parameter']}]: {'; '.join(d['dropped_because'])[:140]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
