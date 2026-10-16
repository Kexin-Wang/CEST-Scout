"""Reference exchange rates and chemical shifts per exchangeable-proton group.

The simulation's k_ex and shift used to be whatever the physicist model typed into the tool call,
and across runs the same functional group was given values two orders of magnitude apart. They
now come from cest_scout/data/exchange_reference.json: every entry is a number stated in a PubMed
abstract, with the PMID, the verbatim sentence, and the measurement context (tissue, species,
in vivo / ex vivo / phantom, pH, temperature). A lookup returns the number together with a plain
statement of where it came from -- including that a liver simulation is borrowing an amide rate
measured in rat brain -- and returns None, meaning "we don't know", when the table has nothing
for that group. Nothing here fills a gap with a plausible guess.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "exchange_reference.json"

GROUP_CLASSES = ("amide", "guanidinium", "amine", "hydroxyl", "imidazole", "thiol", "phosphate", "rnoe", "rnoe16", "other")

# Relayed-NOE pools are not chemical exchange: saturation of non-exchangeable aliphatic/aromatic
# C-H protons of mobile macromolecules reaches water by dipolar cross-relaxation relayed through
# exchangeable protons. Multi-pool Bloch-McConnell fits nevertheless model them as an exchange-like
# pool with an EFFECTIVE transfer rate at a negative (upfield) offset, which is how they are
# simulated here -- with the same rule: the effective rate and the offset must be numbers stated in
# a verified abstract, or the pool is reported as unknown.
# "rnoe": the aliphatic C-H pool of mobile proteins/lipids/polysaccharides near -3.5 ppm (glycogen's
# own glycoNOE near -1 ppm is a metabolite-specific entry of this class); "rnoe16": the distinct
# membrane-lipid pool near -1.6 ppm. They are separate classes because their offsets differ and a
# median across both would describe neither.
RNOE_CLASSES = ("rnoe", "rnoe16")


def mechanism_of(group_class: str) -> str:
    return "rNOE" if group_class in RNOE_CLASSES else "chemical exchange"

# Free-text fallback when the chemist's group_class is missing or not one of GROUP_CLASSES.
# Explicit chemical-group words are tried first, in an order where "guanidinium NH2" does not
# read as an amine and "amide" does not read as anything else; metabolite names (histidine,
# carnosine) only decide when no group word is present -- carnosine has both an imidazole ring
# and a peptide amide, and a text saying "carnosine amide NH" means the amide.
_CLASS_PATTERNS = (
    ("rnoe16", re.compile(r"(?:noe|overhauser)[^.]{0,30}-?\s?1\.[67]\s*ppm|-?\s?1\.[67]\s*ppm[^.]{0,30}(?:noe|overhauser)")),
    ("rnoe", re.compile(r"\brnoe\b|relayed[- ]noe|overhauser|\bnoe\b|aliphatic\s*c-?h|aromatic\s*c-?h|\bglyconoe\b")),
    ("guanidinium", re.compile(r"guanid")),
    ("imidazole", re.compile(r"imidazol")),
    ("thiol", re.compile(r"thiol|sulfhydryl|\bsh\b|\bs-h\b")),
    ("amide", re.compile(r"amide|\bapt\b|backbone\s*n-?h|\bnh-co|\bconh|lactam")),
    ("hydroxyl", re.compile(r"hydroxyl|\bo-?h\b|alcohol")),
    ("amine", re.compile(r"\bamine|\bamino\b|nh2|nh3|ammonium")),
    ("phosphate", re.compile(r"phosph")),
)
_NAME_ONLY_PATTERNS = (
    ("imidazole", re.compile(r"histidin|carnosin")),
    ("amide", re.compile(r"peptide|protein")),
)

_MEASUREMENT_RANK = {"human in vivo": 4, "animal in vivo": 3, "ex vivo tissue": 2, "phantom/solution": 1}


def classify_group(group_text: str, declared_class: str = "") -> str:
    declared = (declared_class or "").strip().lower()
    if declared in GROUP_CLASSES and declared != "other":
        return declared
    text = (group_text or "").lower()
    # "rNOE at -1.6 ppm" names the -1.6 ppm pool however the words are ordered.
    if _CLASS_PATTERNS[0][1].search(text):
        return _CLASS_PATTERNS[0][0]
    # The group named FIRST is the one being described ("carnosine peptide-bond amide NH, not
    # the imidazole ring"); ties fall back to the priority order of _CLASS_PATTERNS.
    hits = []
    for priority, (cls, pattern) in enumerate(_CLASS_PATTERNS):
        m = pattern.search(text)
        if m:
            hits.append((m.start(), priority, cls))
    if hits:
        return min(hits)[2]
    for cls, pattern in _NAME_ONLY_PATTERNS:
        if pattern.search(text):
            return cls
    return "other"


@dataclass
class ReferenceEntry:
    group_class: str
    metabolite: str          # "" when the entry is about the proton class in general
    aliases: list[str]
    shift_ppm: float | None
    shift_range_ppm: list[float] | None
    kex_per_s: float | None
    kex_range_per_s: list[float] | None
    tissue: str
    species: str
    measurement_type: str
    conditions: str
    pmid: str
    title: str
    quote: str
    # "" when the number is quoted from the abstract; the PMC id when it is quoted from the
    # open-access full text instead (a results table, say), which every provenance line states.
    fulltext_pmcid: str = ""

    def names(self) -> set[str]:
        return {_norm(n) for n in [self.metabolite, *self.aliases] if n}

    def describe(self) -> str:
        cls = "rNOE" if self.group_class in RNOE_CLASSES else self.group_class
        what = f"{self.metabolite} {cls}" if self.metabolite else f"generic {cls}"
        where = ", ".join(x for x in (self.measurement_type, self.tissue, self.species, self.conditions) if x and x != "not stated in abstract")
        source = f"; from the full text ({self.fulltext_pmcid}), not the abstract" if self.fulltext_pmcid else ""
        return f"[PMID {self.pmid}] {what} protons, {where}{source}"


@dataclass
class Lookup:
    group_class: str
    shift_ppm: float
    kex_per_s: float
    kex_entry: ReferenceEntry
    shift_entry: ReferenceEntry
    borrowed_from_other_tissue: bool
    metabolite_specific: bool
    provenance: str
    source_pmids: list[str] = field(default_factory=list)
    # The spread of the tier the median was taken from -- every measurement's own value and its
    # stated range -- so the simulation can be repeated at the low and high end instead of
    # presenting one number as if the literature agreed on it. Equal to kex_per_s when the tier is
    # a single measurement with no stated range (then the spread is simply unknown, and said so).
    kex_low_per_s: float | None = None
    kex_high_per_s: float | None = None
    n_kex_measurements: int = 1


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def table_version(path: str = str(DATA_PATH)) -> str:
    p = Path(path)
    if not p.exists():
        return "no reference table"
    data = json.loads(p.read_text())
    return f"built {data.get('built_at_utc', '?')}, {data.get('n_entries', '?')} verified entries"


@lru_cache(maxsize=1)
def load_entries(path: str = str(DATA_PATH)) -> tuple[ReferenceEntry, ...]:
    p = Path(path)
    if not p.exists():
        return ()
    data = json.loads(p.read_text())
    entries = []
    for e in data.get("entries", []):
        v = e.get("verification", {})
        # Only entries whose PMID resolved to the claimed title AND whose quote was found verbatim
        # in that abstract (or in the PMC full text, for a record that says its number is only
        # there) are usable; anything else stays in the file for the record but is never handed
        # to the simulation.
        fulltext = bool(v.get("fulltext_quote_verified")) and not v.get("quote_verified")
        if not (v.get("title_verified") and (v.get("quote_verified") or fulltext)):
            continue
        entries.append(ReferenceEntry(
            group_class=e["group_class"], metabolite=e.get("metabolite", "") or "",
            aliases=list(e.get("aliases", [])), shift_ppm=e.get("shift_ppm"),
            shift_range_ppm=e.get("shift_range_ppm"), kex_per_s=e.get("kex_per_s"),
            kex_range_per_s=e.get("kex_range_per_s"), tissue=e.get("tissue", "") or "",
            species=e.get("species", "") or "", measurement_type=e.get("measurement_type", "") or "",
            conditions=e.get("conditions", "") or "", pmid=str(e.get("pmid", "")), title=e.get("title", ""),
            quote=e.get("quote", ""), fulltext_pmcid=(v.get("pmcid") or "PMC") if fulltext else "",
        ))
    return tuple(entries)


def _names_match(entry: ReferenceEntry, names: set[str], known: frozenset[str] = frozenset()) -> bool:
    """'Glycogen (glycoCEST)' must resolve to the same entries as 'Glycogen', so an entry's
    metabolite name contained in the candidate's name counts -- unless it is only there as part of
    a longer table name: 'creatine' inside 'phosphocreatine' or 'creatine phosphate' is not
    creatine, so a phosphocreatine candidate does not inherit creatine's 2 ppm (a 'creatine +
    phosphocreatine' candidate names both and gets both). `known` is every table name."""
    for en in entry.names():
        if len(en) < 4:
            continue
        longer = [k for k in known if len(k) > len(en) and en in k]
        for n in names:
            if len(n) < 4:
                continue
            for k in longer:
                n = n.replace(k, "|")
            if en in n:
                return True
    return False


def _known_names(entries) -> frozenset[str]:
    return frozenset(n for e in entries for n in e.names())


def _identity(metabolite_name: str, aliases, candidates, known: frozenset[str]) -> set[str]:
    """The names a candidate is looked up under. Its own name, with parentheticals dropped
    ("Glycogen (as glucosyl unit)" is glycogen), is its identity; the literature agent's aliases
    are fallback spellings used only when the name matches no table metabolite ("PCr" is too
    short to match, its alias "phosphocreatine" does). An alias never widens a name that already
    matched: glycogen aliased "polyglucose" was pooled with glucose's own hydroxyl entries."""
    primary = {_norm(re.sub(r"\([^)]*\)", " ", metabolite_name or ""))} - {""}
    if any(_names_match(e, primary, known) for e in candidates):
        return primary
    return primary | {_norm(a) for a in (aliases or []) if a}


def _tier(entry: ReferenceEntry, names: set[str], tissue: str, by_setting: bool = True,
          known: frozenset[str] = frozenset()) -> tuple:
    """Metabolite-specific beats class-level, which beats ANOTHER metabolite's specific value
    (glycogen's own -1 ppm glycoNOE must not be pooled into a class-level rNOE lookup for GAG);
    then, with by_setting, the requested tissue beats a borrowed one and in vivo beats ex vivo
    beats phantom."""
    metabolite_match = _names_match(entry, names, known)
    generic_or_own = metabolite_match or not entry.metabolite
    if not by_setting:
        return (metabolite_match, generic_or_own)
    tissue_match = bool(tissue) and _norm(tissue) in _norm(entry.tissue)
    return (metabolite_match, generic_or_own, tissue_match, _MEASUREMENT_RANK.get(entry.measurement_type, 0))


def _median(values: list[float]) -> float:
    values = sorted(values)
    n = len(values)
    if n == 1:
        return float(values[0])          # one measurement: report it exactly, never re-rounded
    mid = values[n // 2] if n % 2 else 0.5 * (values[n // 2 - 1] + values[n // 2])
    return float(f"{mid:.3g}")


def _best_tier(candidates: list[ReferenceEntry], names: set[str], tissue: str,
               by_setting: bool = True, known: frozenset[str] = frozenset()) -> list[ReferenceEntry]:
    top = max(_tier(e, names, tissue, by_setting, known) for e in candidates)
    return [e for e in candidates if _tier(e, names, tissue, by_setting, known) == top]


def lookup(group_class: str, metabolite_name: str, aliases: list[str] | None = None,
           tissue: str = "", entries: tuple[ReferenceEntry, ...] | None = None) -> Lookup | None:
    """k_ex and shift for this proton class on this metabolite in this tissue, or None when the
    table has no exchange rate (or no shift) for the class -- the caller must then report
    'unknown', not substitute anything.

    All entries of the best-matching tier are used and their MEDIAN is taken, with every one of
    them named in the provenance: several papers measuring the same pool with different fitting
    models disagree by 2-4x, and picking one silently would hide that spread.

    The shift is NOT ranked by tissue or by in vivo over phantom, only k_ex is: a shift is a
    property of the molecule, and an in vivo abstract usually states the offset it saturated at
    for its field, not a resolved resonance (glucose: two 3 T brain glucoCEST papers saturating at
    2 ppm outranked seven values of 0.9-1.6 ppm and set the glucose OH to 2 ppm)."""
    entries = load_entries() if entries is None else entries
    same_class = [e for e in entries if e.group_class == group_class]
    with_kex = [e for e in same_class if e.kex_per_s is not None]
    with_shift = [e for e in same_class if e.shift_ppm is not None]
    if not with_kex or not with_shift:
        return None
    known = _known_names(entries)
    names = _identity(metabolite_name, aliases, same_class, known)
    kex_pool = _best_tier(with_kex, names, tissue, known=known)
    shift_pool = _best_tier(with_shift, names, tissue, by_setting=False, known=known)
    kex_entry, shift_entry = kex_pool[0], shift_pool[0]
    kex = _median([e.kex_per_s for e in kex_pool])
    shift = _median([e.shift_ppm for e in shift_pool])
    spread = [e.kex_per_s for e in kex_pool]
    for e in kex_pool:
        spread += [float(x) for x in (e.kex_range_per_s or []) if x is not None and float(x) > 0]
    kex_low, kex_high = min(spread), max(spread)

    borrowed = not (_norm(tissue) and _norm(tissue) in _norm(kex_entry.tissue))
    specific = _names_match(kex_entry, names, known)

    def listing(pool: list[ReferenceEntry], attr: str, unit: str) -> str:
        return "; ".join(f"{getattr(e, attr):g} {unit} ({e.describe()})" for e in pool)

    rate_word = "effective rNOE transfer rate" if group_class in RNOE_CLASSES else "k_ex"
    parts = [f"{rate_word} {kex:g} s^-1" + (f", the median of {len(kex_pool)} measurements: " if len(kex_pool) > 1 else " from ")
             + listing(kex_pool, "kex_per_s", "s^-1")]
    parts.append(f"shift {shift:g} ppm" + (f", the median of {len(shift_pool)} values from every tissue and setting: "
                                           if len(shift_pool) > 1 else " from ")
                 + listing(shift_pool, "shift_ppm", "ppm"))
    if kex_high > kex_low:
        parts.append(f"k_ex spread across this tier (values and their stated ranges) {kex_low:g}-{kex_high:g} s^-1, "
                     "used for the uncertainty corners")
    else:
        parts.append("a single k_ex measurement with no stated range, so its uncertainty is unknown and no k_ex "
                     "spread enters the uncertainty corners")
    if not specific:
        others = sorted({e.metabolite for e in kex_pool if e.metabolite})
        parts.append(f"no measurement of {metabolite_name}'s own {group_class} protons in the reference table -- "
                     + (f"borrowed from other metabolites' measurements ({', '.join(others)})" if others
                        else f"a class-level {group_class} value is used"))
    if borrowed:
        where = ", ".join(sorted({", ".join(x for x in (e.tissue, e.species, e.measurement_type) if x) for e in kex_pool}))
        parts.append(f"no {tissue or 'tissue'}-specific exchange-rate measurement in the reference table -- "
                     f"borrowed from {where or 'the source(s) above'}")
    return Lookup(
        group_class=group_class, shift_ppm=shift, kex_per_s=kex,
        kex_entry=kex_entry, shift_entry=shift_entry, borrowed_from_other_tissue=borrowed,
        metabolite_specific=specific, provenance="; ".join(parts) + ".",
        source_pmids=sorted({e.pmid for e in kex_pool} | {e.pmid for e in shift_pool}),
        kex_low_per_s=kex_low, kex_high_per_s=kex_high, n_kex_measurements=len(kex_pool),
    )
