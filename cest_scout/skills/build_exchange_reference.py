"""Build cest_scout/data/exchange_reference.json from literature records.

    python -m cest_scout.skills.build_exchange_reference records.json

`records.json` is a list of {"group": ..., "records": [...], "verify": {"verdicts": [...]}} objects
as produced by the reference-hunting workflow: each record names a PMID, the exact title, a
VERBATIM sentence from the abstract, the k_ex / shift text exactly as stated, the measurement
context and type. This script does not trust any of that. It

1. resolves every PMID on PubMed and checks the claimed title (pubmed.verify_titles);
2. checks that the quoted sentence really occurs in that abstract (pubmed.verify_quote) -- or, for a
   record marked "quote_source": "full text", in the PubMed Central open-access full text
   (pubmed.verify_fulltext_quote);
3. parses the stated numbers conservatively -- a single value, "a ± b" or "a-b" ranges; anything
   comparative, composite or unbounded ("vs", "<", "composite", "extrapolated", several pools in
   one field) becomes null rather than a number;
4. drops records the skeptic agent refuted, and records with no usable number.

Everything, including the dropped records and why, is written out, so the table can be audited.
Only entries with title_verified and quote_verified (or fulltext_quote_verified, labelled as such in
every provenance line) are ever loaded by exchange_reference.py.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from cest_scout.skills import pubmed
from cest_scout.skills.exchange_reference import DATA_PATH, GROUP_CLASSES, classify_group

_AMBIGUOUS = ("vs", "versus", "composite", "extrapolat", "<", ">", "not amide", "not specific", "range over all",
              "upper bound", "lower bound", "ratio", "times higher", "×10", "x10", "10(", "above", "below", "at least",
              "up to", "threshold", "faster than", "slower than", "exceed", "more than", "less than")
_APPROX = re.compile(r"[~∼≈]|\babout\b|\bapprox(?:imately)?\.?\b|\bca\.\b", re.I)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

_METABOLITES = {  # order matters: "phosphocreatine" must be tested before "creatine"
    "phosphocreatine": ["phosphocreatine", "pcr"], "creatine": ["creatine", "crcest", "cr guanid"],
    "glutamate": ["glutamate", "glucest"], "glutamine": ["glutamine"], "gaba": ["gaba"], "glycogen": ["glycogen", "glycocest"],
    # Glucose analogues and polymers are other molecules: their "glucose" in the pool text must
    # not make a 3-O-methyl-glucose rate at pH 6.1 and 4 C read as glucose's own.
    "3-o-methyl-glucose": ["3-o-methyl", "3omg"], "2-deoxyglucose": ["2-deoxy", "2-dg", "2dg"],
    "cyclodextrin": ["cyclodextrin"], "dextran": ["dextran"],
    "glucose": ["glucose", "glucocest"], "myo-inositol": ["myo-inositol", "myoinositol", "mi cest"], "lactate": ["lactate"],
    "taurine": ["taurine"], "alanine": ["alanine"], "glutathione": ["glutathione", "gsh"], "carnosine": ["carnosine"],
    "histidine": ["histidine"], "urea": ["urea"], "atp": ["atp"], "amp": ["amp "], "nad": ["nad"],
    "glycosaminoglycan": ["glycosaminoglycan", "gagcest", "gag "], "arginine": ["arginine"], "lysine": ["lysine"],
    "asparagine": ["asparagine"], "protamine": ["protamine"], "bsa": ["bsa", "albumin"],
}
# Other names of a table metabolite. A candidate named "creatine phosphate" is phosphocreatine,
# and exchange_reference uses these to keep creatine's entries from matching it.
_ALIASES = {"phosphocreatine": ["creatine phosphate", "phosphorylcreatine"]}
# Pool descriptions that are not one proton class: composite/omega-plot rates over every pool,
# FLEX-detected mixtures, "total" exchangeable protons. Such a record can be a real, verbatim
# abstract statement and still be the wrong number for a single functional group.
_NOT_ONE_POOL = ("composite", "flex", "all saturation", "all exchangeable", "total exchange", "not specific",
                 "not amide", "mixture", "overall exchange", "aggregate", "downfield 1h mrs", "pooled", "jointly",
                 "hydroxyl and amino", "amino and hydroxyl")
# Chemistry the class label cannot override: creatine's exchangeable protons ARE its guanidinium
# group, even when an abstract calls them "amine".
_CLASS_BY_METABOLITE = {"creatine": "guanidinium", "phosphocreatine": "guanidinium"}
_TISSUES = ("liver", "brain", "muscle", "calf", "kidney", "renal", "heart", "myocard", "spleen", "cartilage", "tumor",
            "glioma", "spinal", "prostate", "breast", "abdomen", "phantom", "solution", "lysate")
_SPECIES = ("human", "rat", "mouse", "murine", "swine", "pig", "canine", "dog", "rabbit")


def _label_of(segment: str) -> str:
    m = re.search(r"\(([^)]*)\)", segment)
    return m.group(1).strip().lower() if m else ""


# Words by which a segment label names a proton pool ("(PCr)", "(amide)", "(urea)"), as opposed to
# a tissue, method or condition ("(gray matter)", "(CEST-MRF)"), whose values are one pool's.
_POOL_WORDS = tuple(sorted({c for c in GROUP_CLASSES if c != "other"} | {h.strip() for hs in _METABOLITES.values() for h in hs}
                           | {"cr", "guanid", "protein", "chondroitin"}))


def _names_a_pool(label: str, words) -> bool:
    return any(re.search(rf"\b{re.escape(w)}", label) for w in words if w)


def parse_number(text: str, prefer: list[str] | tuple[str, ...] = ()) -> tuple[float | None, list[float] | None, str]:
    """(value, [low, high] or None, note). Conservative: returns None when the text is comparative,
    composite or lists values that disagree by more than 2x. A field whose labels name several
    pools ("2.6 (PCr); 1.9 (Cr)") yields the one segment whose label names a `prefer` word, or
    None; averaging them described neither pool."""
    raw = (text or "").strip()
    if not raw:
        return None, None, "not stated"
    lowered = raw.lower()
    if any(a in lowered for a in _AMBIGUOUS):
        return None, None, f"ambiguous/comparative text kept for the record: {raw!r}"
    raw = re.sub(r"(\d),(\d{3})\b", r"\1\2", raw)  # "10,000" -> "10000"
    # "1.2 kHz" -> 1200; a unit the number parser would otherwise silently drop.
    raw = re.sub(r"(\d+(?:\.\d+)?)\s*k\s*hz\b", lambda m: str(float(m.group(1)) * 1000), raw, flags=re.I)
    segments = [s for s in re.split(r";", raw) if _NUMBER.search(s)]
    labels = [_label_of(seg) for seg in segments]
    if len({lab for lab in labels if lab and _names_a_pool(lab, _POOL_WORDS)}) > 1:
        mine = [seg for seg, lab in zip(segments, labels) if lab and _names_a_pool(lab, prefer)]
        if len(mine) != 1:
            return None, None, f"several pools in one field, not parsed: {raw!r}"
        segments = mine
    values: list[float] = []
    ranges: list[list[float]] = []
    for seg in segments:
        seg = _APPROX.sub("", seg)
        seg = re.sub(r"\[.*?\]|\(.*?\)", " ", seg)  # drop bracketed labels like (gray matter)
        pm = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:±|\+/-|\+-)\s*(\d+(?:\.\d+)?)", seg)
        rng = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(-?\d+(?:\.\d+)?)", seg)
        if pm:
            v, sd = float(pm.group(1)), float(pm.group(2))
            values.append(v)
            ranges.append([v - sd, v + sd])
        elif rng and float(rng.group(1)) < float(rng.group(2)):
            lo, hi = float(rng.group(1)), float(rng.group(2))
            values.append(0.5 * (lo + hi))
            ranges.append([lo, hi])
        else:
            nums = _NUMBER.findall(seg)
            if len(nums) == 1:
                values.append(float(nums[0]))
            elif nums:
                return None, None, f"several numbers in one segment, not parsed: {raw!r}"
    if not values:
        return None, None, f"no number parsed from {raw!r}"
    # Compared on magnitude: a single negative shift such as "-3.5" (an upfield rNOE pool) is not
    # "a value more than twice another value" -- the old signed comparison dropped every
    # negative-offset record in the table's first build.
    mags = [abs(v) for v in values]
    if max(mags) > 2 * min(mags) or (len(values) > 1 and min(values) < 0 < max(values)):
        return None, None, f"values differ by more than 2x, not pooled: {raw!r}"
    value = float(f"{sum(values) / len(values):.4g}")
    low = min([r[0] for r in ranges] + values)
    high = max([r[1] for r in ranges] + values)
    rng_out = [low, high] if (ranges or len(values) > 1) else None
    return value, rng_out, "parsed"


def _metabolite_of(pool_text: str) -> str:
    lowered = f" {pool_text.lower()} "
    for name, hints in _METABOLITES.items():
        if any(h in lowered for h in hints):
            return name
    return ""


def _tissue_of(conditions: str, measurement_type: str) -> str:
    lowered = conditions.lower()
    found = [t for t in _TISSUES if re.search(rf"\b{t}", lowered)]
    if "phantom" in measurement_type or "solution" in measurement_type:
        return "phantom"
    return found[0] if found else ""


def _species_of(conditions: str) -> str:
    """Whole words only: 'rat' must not be found inside 'demonstration' (it was)."""
    lowered = conditions.lower()
    for s in _SPECIES:
        if re.search(rf"\b{s}s?\b", lowered):
            return {"murine": "mouse", "swine": "pig", "canine": "dog"}.get(s, s)
    return ""


def _class_of(group_text: str, pool_text: str) -> str:
    head = group_text.strip().lower().split(" ")[0].split("(")[0]
    if head in GROUP_CLASSES and head != "other":
        return head
    return classify_group(pool_text)


def build(groups: list[dict]) -> dict:
    records = []
    for g in groups:
        found = g.get("found") or {}
        verdicts = {v.get("pmid"): v for v in ((g.get("verify") or {}).get("verdicts") or [])}
        for r in found.get("records", []):
            r = dict(r)
            r["_group_text"] = found.get("group", g.get("group", ""))
            r["_verdict"] = verdicts.get(r.get("pmid"))
            records.append(r)

    pairs = [(str(r["pmid"]), r.get("title", "")) for r in records if r.get("pmid")]
    title_verdicts = pubmed.verify_titles(pairs) if pairs else {}

    entries, dropped = [], []
    seen: set[tuple] = set()
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
        # A record may declare that its number is stated only in the paper's body; the quote is
        # then checked verbatim against the PubMed Central open-access full text instead, and the
        # entry is labelled full-text wherever it is used.
        fulltext = {}
        if r.get("quote_source") == "full text" and pmid:
            try:
                fulltext = pubmed.verify_fulltext_quote(pmid, r.get("quote", ""))
            except Exception as exc:
                fulltext = {"verified": False, "reason": str(exc)}
        fulltext_ok = bool(fulltext.get("verified"))
        skeptic = r.get("_verdict") or {}
        refuted = bool(skeptic.get("refuted"))
        cls = _class_of(r["_group_text"], r.get("metabolite_or_pool", ""))
        metabolite = _metabolite_of(r.get("metabolite_or_pool", ""))
        if metabolite in _CLASS_BY_METABOLITE and cls != _CLASS_BY_METABOLITE[metabolite]:
            # Creatine/phosphocreatine CEST is their guanidinium protons whatever the abstract or
            # finder called the pool (amine, phosphate, ...).
            cls = _CLASS_BY_METABOLITE[metabolite]
        prefer = [cls, *(h.strip() for h in _METABOLITES.get(metabolite, []))]
        kex, kex_range, kex_note = parse_number(r.get("kex_per_s", ""), prefer)
        shift, shift_range, shift_note = parse_number(r.get("shift_ppm", ""), prefer)
        pool_lower = f"{r.get('metabolite_or_pool', '')} {r.get('notes', '')} {r.get('kex_per_s', '')}".lower()
        not_one_pool = any(k in pool_lower for k in _NOT_ONE_POOL)
        entry = {
            "group_class": cls,
            "metabolite": metabolite,
            "aliases": list(_ALIASES.get(metabolite, [])),
            "pool_text": r.get("metabolite_or_pool", ""),
            "shift_ppm": shift, "shift_range_ppm": shift_range, "shift_text": r.get("shift_ppm", ""), "shift_note": shift_note,
            "kex_per_s": kex, "kex_range_per_s": kex_range, "kex_text": r.get("kex_per_s", ""), "kex_note": kex_note,
            "tissue": _tissue_of(r.get("conditions", ""), r.get("measurement_type", "")),
            "species": _species_of(r.get("conditions", "")),
            "measurement_type": r.get("measurement_type", ""),
            "conditions": r.get("conditions", ""),
            "pmid": pmid, "title": tv.get("real_title") or r.get("title", ""), "year": r.get("year", ""),
            "quote": r.get("quote", ""), "notes": r.get("notes", ""),
            "verification": {
                "title_verified": title_ok, "quote_verified": quote_ok,
                **({"fulltext_quote_verified": fulltext_ok, "pmcid": fulltext.get("pmcid")} if fulltext else {}),
                "skeptic_refuted": refuted, "skeptic_reason": skeptic.get("reason", ""),
                "skeptic_conditions_accurate": skeptic.get("conditions_accurate"),
            },
        }
        reasons = []
        if not title_ok:
            reasons.append("PMID does not resolve to the claimed title")
        if fulltext and not fulltext_ok:
            reasons.append(f"full-text quote not verified: {fulltext.get('reason')}")
        elif not quote_ok and not fulltext_ok:
            reasons.append("quote not found in the abstract")
        if refuted:
            reasons.append(f"refuted by the skeptic: {skeptic.get('reason', '')}")
        if kex is None and shift is None:
            reasons.append("no usable number (kex: %s; shift: %s)" % (kex_note, shift_note))
        if cls == "other":
            reasons.append("proton class could not be determined")
        if not_one_pool and kex is not None:
            reasons.append("exchange rate describes a composite/mixed pool, not this proton class")
        if cls.startswith("rnoe") and "downfield" in pool_lower and (shift is None or shift > 0):
            # The downfield rNOE (around +3.5 ppm, overlapping the amide pool) is not the upfield
            # aliphatic pool this class simulates; a positive offset here would put an rNOE
            # pool on the wrong side of water.
            reasons.append("downfield rNOE record (positive offset); the rnoe classes are the upfield pools")
        if cls.startswith("rnoe") and shift is not None and shift > 0:
            reasons.append("rNOE offset must be upfield (negative); positive value not used")
        # The same abstract restated by the finder (e.g. as a cross-reference) must count once,
        # or it would double-weight the median.
        signature = (pmid, cls, entry["metabolite"], kex, shift)
        if not reasons and signature in seen:
            reasons.append("duplicate of an earlier record from the same abstract")
        if reasons:
            entry["dropped_because"] = reasons
            dropped.append(entry)
        else:
            seen.add(signature)
            entries.append(entry)

    return {
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": ("Numbers stated in PubMed abstracts, found by literature agents, then verified deterministically: "
                   "PMID -> real title match, quoted sentence present in the abstract (or, for a record marked "
                   "quote_source 'full text', in the PubMed Central open-access full text, and labelled so), parsed "
                   "conservatively; records refuted by an independent skeptic agent are excluded."),
        "n_entries": len(entries), "n_dropped": len(dropped),
        "entries": entries,
        "dropped": dropped,
        "known_but_not_in_abstract": [k for g in groups for k in ((g.get("found") or {}).get("known_but_not_in_abstract") or [])],
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    groups = json.loads(Path(argv[0]).read_text())
    table = build(groups)
    out = Path(argv[1]) if len(argv) > 1 else DATA_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(table, indent=1, ensure_ascii=False))
    by_class: dict[str, int] = {}
    for e in table["entries"]:
        by_class[e["group_class"]] = by_class.get(e["group_class"], 0) + 1
    print(f"{table['n_entries']} verified entries ({by_class}), {table['n_dropped']} dropped -> {out}")
    for d in table["dropped"]:
        print(f"  dropped PMID {d['pmid']} [{d['group_class']}]: {'; '.join(d['dropped_because'])[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
