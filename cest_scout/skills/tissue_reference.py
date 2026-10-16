"""Water relaxation and semisolid-MT parameters per tissue, with provenance.

The Bloch-McConnell simulation needs the tissue's water T1/T2 and MT pool for the target tissue
and for every ROI of the verification scan. They come from cest_scout/data/tissue_reference.json:
each entry is one parameter of one tissue at one field strength, with the PMID, the measurement
context and a verification status. Entries whose number is quoted verbatim from a PubMed abstract
are preferred; entries copied from a paper's tables are kept but labelled "not abstract-verified",
and the label travels into every report. A tissue with no entry at all gets the simulator's
stated generic defaults, and the report says so -- nothing is guessed to fill a gap.

Water T1 and T2 depend on field strength, so a value measured at one field is never reused
unchanged at another. When the table has no measurement at the simulated field, the value is
ESTIMATED by a power law in B0 -- the form Bottomley et al. found for every tissue dispersion
("All tissue frequency dispersions can be fitted to the simple expression T1 = AVB in the range
1-100 MHz, with A and B tissue-dependent constants"; "T2 is found to be multicomponent, essentially
independent of NMR frequency", PMID 6482839) -- with the exponent taken from the tissue's own
multi-field measurements when it has them, otherwise the median exponent of the tissues in the
table that do. Estimated values are labelled as such, with the rule and the anchor field, in
every report; between two measured fields the same power law interpolates.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "tissue_reference.json"

PARAMETERS = ("T1", "T2", "MT_fraction", "MT_exchange_rate", "MT_T2b")
_MEASUREMENT_RANK = {"human in vivo": 4, "animal in vivo": 3, "ex vivo tissue": 2, "phantom/solution": 1}

# Values simulate_with_without_metabolite falls back to (brain-generic), restated here so a report
# can say exactly what was used when a tissue is not in the table. The relaxation times are 3 T
# values and are scaled to other fields with the same power law as tabulated tissues.
GENERIC_DEFAULTS = {"water_t1_s": 1.3, "water_t2_s": 0.08, "mt_fraction_of_water": 0.05,
                    "mt_exchange_rate_per_s": 30.0, "mt_t2_s": 10e-6}
GENERIC_DEFAULTS_FIELD_T = 3.0

# Field-scaling law: value(B) = value(B_meas) * (B / B_meas) ** b. A tissue's own exponent is used
# when it has measurements spanning at least a factor of two in field and the fit lands in the
# physically plausible band below; otherwise the median over the table's multi-field tissues; and
# only if the table had none, the stated fallback (T1: 0.35, in the range of Bottomley's
# tissue-dependent B; T2: 0, "essentially independent of NMR frequency", PMID 6482839).
_EXPONENT_BAND = {"T1": (0.10, 0.60), "T2": (-0.40, 0.10)}
_EXPONENT_FALLBACK = {"T1": 0.35, "T2": 0.0}
_FIELD_LAW_PMID = "6482839"


@dataclass
class TissueEntry:
    tissue: str
    aliases: list[str]
    parameter: str          # one of PARAMETERS
    field_T: float | None   # None for field-independent parameters (MT fraction, exchange rate)
    value: float            # SI: seconds for T1/T2/MT_T2b, fraction for MT_fraction, s^-1 for exchange rate
    species: str
    measurement_type: str
    conditions: str
    pmid: str
    title: str
    quote: str
    abstract_verified: bool

    def names(self) -> set[str]:
        return {_norm(n) for n in [self.tissue, *self.aliases] if n}

    def describe(self) -> str:
        where = ", ".join(x for x in (self.measurement_type, self.species, self.conditions) if x)
        fld = f" at {self.field_T:g} T" if self.field_T else ""
        tag = "" if self.abstract_verified else "; table value, not abstract-verified"
        return f"{self.tissue}{fld}, {where} (PMID {self.pmid}{tag})"


@dataclass
class TissueLookup:
    tissue: str
    overrides: dict = field(default_factory=dict)   # kwargs for simulate_with_without_metabolite
    provenance: str = ""
    source_pmids: list[str] = field(default_factory=list)
    any_abstract_verified: bool = False
    # One dict per parameter for tables: parameter, value (with unit), source, verified
    # ('abstract' / 'table' / 'default'), note (interpolated / clamped / generic default).
    rows: list[dict] = field(default_factory=list)


_PARAM_LABEL = {"T1": "water T1", "T2": "water T2", "MT_fraction": "MT fraction",
                "MT_exchange_rate": "MT k_ex", "MT_T2b": "MT T2b"}


def _source_text(entries: list["TissueEntry"]) -> str:
    seen: dict[str, str] = {}
    for e in entries:
        seen.setdefault(e.pmid, f"PMID {e.pmid} ({', '.join(x for x in (e.measurement_type, e.species) if x)}"
                        + ("" if e.abstract_verified else "; table value") + ")")
    return "; ".join(seen.values())


def _verified_level(entries: list["TissueEntry"]) -> str:
    return "abstract" if any(e.abstract_verified for e in entries) else "table"


def default_rows(field_strength_T: float = GENERIC_DEFAULTS_FIELD_T) -> list[dict]:
    """The simulator's generic defaults, as table rows, for a tissue the table does not know; T1
    and T2 scaled to the requested field."""
    t1, n1 = default_relaxation("T1", field_strength_T)
    t2, n2 = default_relaxation("T2", field_strength_T)
    return [
        {"parameter": "water T1", "value": f"{t1 * 1000:.0f} ms", "source": "generic default", "verified": "default",
         "note": "tissue not in the table" + (f"; {n1}" if n1 else "")},
        {"parameter": "water T2", "value": f"{t2 * 1000:.0f} ms", "source": "generic default", "verified": "default",
         "note": "tissue not in the table" + (f"; {n2}" if n2 else "")},
        {"parameter": "MT fraction", "value": f"{GENERIC_DEFAULTS['mt_fraction_of_water']:g}", "source": "generic default", "verified": "default", "note": ""},
        {"parameter": "MT k_ex", "value": f"{GENERIC_DEFAULTS['mt_exchange_rate_per_s']:g} s^-1", "source": "generic default", "verified": "default", "note": ""},
        {"parameter": "MT T2b", "value": f"{GENERIC_DEFAULTS['mt_t2_s'] * 1e6:g} us", "source": "generic default", "verified": "default", "note": ""},
    ]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


@lru_cache(maxsize=1)
def load_entries(path: str = str(DATA_PATH)) -> tuple[TissueEntry, ...]:
    p = Path(path)
    if not p.exists():
        return ()
    data = json.loads(p.read_text())
    out = []
    for e in data.get("entries", []):
        v = e.get("verification", {})
        if not v.get("title_verified"):
            continue
        out.append(TissueEntry(
            tissue=e["tissue"], aliases=list(e.get("aliases", [])), parameter=e["parameter"],
            field_T=e.get("field_T"), value=float(e["value_si"]), species=e.get("species", "") or "",
            measurement_type=e.get("measurement_type", "") or "", conditions=e.get("conditions", "") or "",
            pmid=str(e.get("pmid", "")), title=e.get("title", ""), quote=e.get("quote", ""),
            abstract_verified=bool(v.get("quote_verified")),
        ))
    return tuple(out)


def known_tissues(entries: tuple[TissueEntry, ...] | None = None) -> list[str]:
    entries = load_entries() if entries is None else entries
    return sorted({e.tissue for e in entries if e.parameter in ("T1", "T2")})


def _matches(names: set[str], roi_name: str) -> bool:
    """An entry applies to an ROI when its tissue name (or an alias) is contained in the ROI's
    name, or the other way round for multi-word ROI names ("nucleus pulposus of L4/5" matches the
    entry "nucleus pulposus"; "liver" matches "liver parenchyma")."""
    roi = _norm(roi_name)
    roi_tokens = set(roi.split())
    # A fat compartment ("breast adipose tissue") never stands in for the organ it is named
    # after, nor the organ for it: CEST candidates sit in the parenchyma, so fat entries and
    # fat ROIs only match each other.
    fat = ("adipose", "fat")
    if any(w in n for n in names for w in fat) != any(w in roi for w in fat):
        return False
    for n in names:
        if n == roi:
            return True          # exact match, so short aliases like "np", "csf", "wm" work
        if len(n) < 4:
            continue
        if n in roi or roi in n:
            return True
        if set(n.split()) <= roi_tokens:
            return True
    return False


def _tier(e: TissueEntry) -> tuple:
    return (e.abstract_verified, _MEASUREMENT_RANK.get(e.measurement_type, 0))


def _best(entries: list[TissueEntry]) -> list[TissueEntry]:
    top = max(_tier(e) for e in entries)
    return [e for e in entries if _tier(e) == top]


def _median(values: list[float]) -> float:
    return float(np.median(values))


def _power_fit(by_field: dict[float, float]) -> float | None:
    """Exponent b of value ~ B0^b through (field, value) points -- log-log least squares -- or
    None when the fields do not span a factor of two (too short a lever arm to trust)."""
    fields = sorted(f for f in by_field if f > 0 and by_field[f] > 0)
    if len(fields) < 2 or fields[-1] / fields[0] < 2.0:
        return None
    xs = np.log(fields)
    ys = np.log([by_field[f] for f in fields])
    return float(np.polyfit(xs, ys, 1)[0])


def table_exponent(param: str, entries: tuple[TissueEntry, ...] | None = None) -> tuple[float, str]:
    """(exponent, how it was obtained) for the whole table: the median of every tissue's own
    power-law fit over its measured fields, or the stated fallback when no tissue has two fields."""
    if entries is None:
        return _table_exponent_cached(param)
    return _table_exponent(param, entries)


@lru_cache(maxsize=8)
def _table_exponent_cached(param: str) -> tuple[float, str]:
    return _table_exponent(param, load_entries())


table_exponent.cache_clear = _table_exponent_cached.cache_clear  # type: ignore[attr-defined]


def _table_exponent(param: str, entries: tuple[TissueEntry, ...]) -> tuple[float, str]:
    per_tissue: dict[str, dict[float, list[float]]] = {}
    for e in entries:
        if e.parameter == param and e.field_T:
            per_tissue.setdefault(e.tissue, {}).setdefault(float(e.field_T), []).append(e.value)
    lo, hi = _EXPONENT_BAND[param]
    slopes = []
    for tissue, d in per_tissue.items():
        b = _power_fit({f: _median(v) for f, v in d.items()})
        if b is not None and lo <= b <= hi:
            slopes.append(b)
    if slopes:
        return round(_median(slopes), 2), f"median of {len(slopes)} table tissues' own dispersions"
    return _EXPONENT_FALLBACK[param], "stated fallback (no multi-field tissue in the table)"


def _scale_note(param: str, from_T: float, to_T: float, b: float, how: str) -> str:
    return (f"estimated: scaled from {from_T:g} T with {param} ∝ B0^{b:+.2f} ({how}; power-law form "
            f"per PMID {_FIELD_LAW_PMID})")


def default_relaxation(param: str, field_strength_T: float) -> tuple[float, str]:
    """The generic default for T1/T2 scaled to the requested field, with its note."""
    key = {"T1": "water_t1_s", "T2": "water_t2_s"}[param]
    base = GENERIC_DEFAULTS[key]
    if abs(field_strength_T - GENERIC_DEFAULTS_FIELD_T) < 1e-9:
        return base, ""
    b, how = table_exponent(param)
    return base * (field_strength_T / GENERIC_DEFAULTS_FIELD_T) ** b, _scale_note(param, GENERIC_DEFAULTS_FIELD_T, field_strength_T, b, how)


def lookup(roi_name: str, field_strength_T: float, entries: tuple[TissueEntry, ...] | None = None) -> TissueLookup | None:
    """Simulator overrides for this ROI at this field, or None if the table has neither water T1
    nor T2 for it. Each is taken from the best-verified tier, interpolated between measured fields
    and clamped at the ends (stated in the provenance); a missing one falls back to the stated
    generic default; MT parameters are used when present."""
    entries = load_entries() if entries is None else entries
    # Aliases belong to the tissue, not to the entry they happened to be written on: pool them.
    names_by_tissue: dict[str, set[str]] = {}
    for e in entries:
        names_by_tissue.setdefault(e.tissue, set()).update(e.names())
    mine = [e for e in entries if _matches(names_by_tissue[e.tissue], roi_name)]
    if not mine:
        return None
    tissue = max({e.tissue for e in mine}, key=len)
    parts, pmids, verified = [], set(), False
    overrides: dict = {}
    rows: list[dict] = []

    def relaxation(param: str, key: str) -> bool:
        nonlocal verified
        cands = [e for e in mine if e.parameter == param and e.field_T]
        if not cands:
            return False
        best = _best(cands)
        by_field: dict[float, list[float]] = {}
        for e in best:
            by_field.setdefault(float(e.field_T), []).append(e.value)
        fields = sorted(by_field)
        medians = {f: _median(by_field[f]) for f in fields}
        estimated = False
        if field_strength_T in medians:
            value, note = medians[field_strength_T], ""
        else:
            estimated = True
            # This tissue's own dispersion (all its fields, any tier) decides the exponent when it
            # spans enough field and is plausible; otherwise the table-wide median exponent.
            own_by_field: dict[float, list[float]] = {}
            for e in cands:
                own_by_field.setdefault(float(e.field_T), []).append(e.value)
            own = _power_fit({f: _median(v) for f, v in own_by_field.items()})
            lo, hi = _EXPONENT_BAND[param]
            if own is not None and lo <= own <= hi:
                b, how = round(own, 2), f"this tissue's own dispersion over {min(own_by_field):g}-{max(own_by_field):g} T"
            else:
                b, how = table_exponent(param)
            if fields[0] < field_strength_T < fields[-1]:
                f_lo = max(f for f in fields if f < field_strength_T)
                f_hi = min(f for f in fields if f > field_strength_T)
                # power-law interpolation between the two neighbouring measured fields
                slope = (np.log(medians[f_hi]) - np.log(medians[f_lo])) / (np.log(f_hi) - np.log(f_lo))
                value = float(medians[f_lo] * (field_strength_T / f_lo) ** slope)
                note = f" -- estimated: power-law interpolation between the {f_lo:g} T and {f_hi:g} T measurements"
            else:
                anchor = fields[0] if field_strength_T < fields[0] else fields[-1]
                value = float(medians[anchor] * (field_strength_T / anchor) ** b)
                note = " -- " + _scale_note(param, anchor, field_strength_T, b, how)
        overrides[key] = value
        srcs = "; ".join(f"{e.value * 1000:g} ms {e.describe()}" for e in best)
        parts.append(f"{param} {value * 1000:.0f} ms{note}: {srcs}")
        rows.append({"parameter": _PARAM_LABEL[param], "value": f"{value * 1000:.0f} ms", "source": _source_text(best),
                     "verified": "estimated" if estimated else _verified_level(best), "note": note.strip(" -")})
        pmids.update(e.pmid for e in best)
        verified = verified or any(e.abstract_verified for e in best)
        return True

    have_t1 = relaxation("T1", "water_t1_s")
    have_t2 = relaxation("T2", "water_t2_s")
    if not (have_t1 or have_t2):
        return None
    # One verified relaxation time is worth using; the other is then the stated generic default,
    # never a guess (a disc's T2 is measured in dozens of abstracts, its T1 in almost none).
    for have, param, key in ((have_t1, "T1", "water_t1_s"), (have_t2, "T2", "water_t2_s")):
        if not have:
            value, note = default_relaxation(param, field_strength_T)
            overrides[key] = value
            parts.append(f"{param} unknown for {tissue} in the table -- generic default "
                         f"{value * 1000:.0f} ms used" + (f" ({note})" if note else ""))
            rows.append({"parameter": _PARAM_LABEL[param], "value": f"{value * 1000:.0f} ms",
                         "source": "generic default", "verified": "default",
                         "note": f"unknown for {tissue}" + (f"; {note}" if note else "")})

    for param, key, unit, scale in (("MT_fraction", "mt_fraction_of_water", "", 1.0),
                                    ("MT_exchange_rate", "mt_exchange_rate_per_s", " s^-1", 1.0),
                                    ("MT_T2b", "mt_t2_s", " us", 1e6)):
        cands = [e for e in mine if e.parameter == param]
        if not cands:
            continue
        best = _best(cands)
        value = _median([e.value for e in best])
        overrides[key] = value
        parts.append(f"{param} {value * scale:g}{unit}: " + "; ".join(e.describe() for e in best))
        rows.append({"parameter": _PARAM_LABEL[param], "value": f"{value * scale:g}{unit}", "source": _source_text(best),
                     "verified": _verified_level(best), "note": ""})
        pmids.update(e.pmid for e in best)
        verified = verified or any(e.abstract_verified for e in best)
    missing_mt = [k for k in ("mt_fraction_of_water", "mt_exchange_rate_per_s", "mt_t2_s") if k not in overrides]
    if missing_mt:
        parts.append("MT pool " + ", ".join(missing_mt) + f" not in the table for {tissue} -- generic defaults used "
                     f"({GENERIC_DEFAULTS['mt_fraction_of_water']:g} of water, {GENERIC_DEFAULTS['mt_exchange_rate_per_s']:g} s^-1, "
                     f"{GENERIC_DEFAULTS['mt_t2_s'] * 1e6:g} us)")
        for key in missing_mt:
            row = next(r for r in default_rows() if r["parameter"] == {"mt_fraction_of_water": "MT fraction", "mt_exchange_rate_per_s": "MT k_ex", "mt_t2_s": "MT T2b"}[key])
            rows.append({**row, "note": f"not in the table for {tissue}"})
    return TissueLookup(tissue=tissue, overrides=overrides, provenance="; ".join(parts) + ".",
                        source_pmids=sorted(pmids), any_abstract_verified=verified, rows=rows)


def parameter_rows(roi_name: str, field_strength_T: float, entries: tuple[TissueEntry, ...] | None = None) -> list[dict]:
    """Table rows (parameter, value, source, verified, note) for what the simulation used for
    this ROI at this field -- the table form of describe()."""
    found = lookup(roi_name, field_strength_T, entries)
    return list(found.rows) if found else default_rows(field_strength_T)


def describe(roi_name: str, field_strength_T: float, entries: tuple[TissueEntry, ...] | None = None) -> str:
    """One sentence for a report: what the simulation used for this ROI's water and MT pools."""
    found = lookup(roi_name, field_strength_T, entries)
    if found is None:
        t1, n1 = default_relaxation("T1", field_strength_T)
        t2, _ = default_relaxation("T2", field_strength_T)
        scaled = f" scaled to {field_strength_T:g} T ({n1})" if n1 else ""
        return (f"no verified water T1/T2 for '{roi_name}' in the tissue table -- generic defaults used "
                f"(T1 {t1:.2f} s, T2 {t2 * 1000:.0f} ms{scaled}, MT fraction "
                f"{GENERIC_DEFAULTS['mt_fraction_of_water']:g}); we do not know this tissue's values")
    return f"{found.tissue} water/MT parameters: {found.provenance}"
