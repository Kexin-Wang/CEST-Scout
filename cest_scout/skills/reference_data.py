"""Deterministic reference-data grounding for metabolite concentrations.

Not exposed as an LLM tool (no TOOL_SPEC/call here) -- like cest_scout.skills.pubchem, this is a
lookup the calling agent applies to its own output, so that a concentration in the final report
comes from a reference database with a citation attached to the number itself, rather than from
the model's recall. A model asked for "the liver concentration of taurine" will produce a
confident, plausible, round number with no traceable source; that is the failure this exists to
replace. Journal papers found via cest_scout.skills.pubmed remain useful, but as the weaker,
secondary source: they are found by keyword relevance, whereas everything returned here was
curated into a reference database with its primary literature source recorded alongside it.

Two sources, because no single one covers both cases:

1. lookup_concentrations()  -- BIOFLUIDS, offline.
   Reads the pre-built HMDB 5.0 index at cest_scout/data/hmdb_concentrations.json (built by
   cest_scout.skills.build_hmdb_index, which is a one-time step, not part of the pipeline). No
   network, so it cannot fail mid-run. Every record carries the full literature reference and,
   usually, a PMID -- which means a number returned here can be re-verified end to end with
   cest_scout.skills.pubmed.verify_titles.

2. lookup_tissue_concentration()  -- SOLID TISSUE, live scrape of BioNumbers.
   HMDB simply has no solid-tissue concentrations: every one of its biospecimens is a biofluid
   (Blood, Urine, CSF, Feces, Saliva, Sweat, Bile...) and there is no "Liver" biospecimen. Its
   <tissue_locations> field is qualitative only -- organ names, no numbers -- so it is not
   indexed. For an actual liver/muscle/brain-tissue concentration the source is BioNumbers, whose
   records often trace back to a canonical reference work (frequently Williamson & Brosnan,
   "Concentrations of metabolites in animal tissues", in Bergmeyer's Methods of Enzymatic
   Analysis) rather than to a single paper. BioNumbers has no JSON API -- /api/search 404s -- so
   this is an HTML scrape, and its coverage is sparse and its keyword search noisy; see the
   filtering in _is_plausible_tissue_hit.

Error handling follows pubchem.py: ordinary exceptions propagate and the caller decides what to
do (the agent pattern is to catch, leave the number unverified, and say so in a note). Nothing
here ever invents a value, a unit or a citation -- a lookup that finds nothing returns
found=False.

Licensing note: HMDB asks for acknowledgment of the source, which the index provides on every
record (accession + full reference + PMID). HMDB's terms-of-use text itself could not be read
from an unblocked host -- it lives on hmdb.ca/about, which is Cloudflare-gated and 403s, and is
not present in the downloaded archive -- so the redistribution terms behind the committed index
have not been confirmed first-hand.
"""
from __future__ import annotations

import html
import json
import os
import re
import time

import requests

_INDEX_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "hmdb_concentrations.json")

BIONUMBERS_BASE = "https://bionumbers.hms.harvard.edu"

# Words a BioNumbers record title must contain for the record to be treated as a concentration
# rather than a rate, a flux, a turnover time or an enzyme parameter. The search is a plain
# keyword match over the whole database, so "glucose liver" also returns things like
# "Apparent penetration of glucose-6-P through microsomal membrane".
_CONCENTRATION_WORDS = ("concentration", "conc.", "level", "content", "amount")

# Cached at module level: the index is ~11 MB of JSON (a couple of seconds to parse) and is
# looked up once per metabolite per run, so re-parsing it per call would dominate a pipeline pass.
_index_cache: dict | None = None
_lookup_cache: dict[str, dict] | None = None


def _load_index() -> tuple[dict, dict[str, dict]]:
    """Load and index the HMDB extract once. Returns (raw payload, {lowercased name or synonym:
    metabolite entry}). Primary names take precedence over synonyms on collision, since HMDB
    synonym lists overlap between related compounds (e.g. an acid and its conjugate base).
    """
    global _index_cache, _lookup_cache
    if _index_cache is not None and _lookup_cache is not None:
        return _index_cache, _lookup_cache

    with open(_INDEX_PATH, encoding="utf-8") as fh:
        payload = json.load(fh)

    by_key: dict[str, dict] = {}
    for entry in payload["metabolites"]:
        for synonym in entry["synonyms"]:
            by_key.setdefault(synonym, entry)
    # Second pass so a primary name always wins over another metabolite's synonym.
    for entry in payload["metabolites"]:
        name = (entry.get("name") or "").strip().lower()
        if name:
            by_key[name] = entry

    _index_cache, _lookup_cache = payload, by_key
    return payload, by_key


def lookup_concentrations(name: str, aliases: list[str] | None = None) -> dict:
    """Look up normal human BIOFLUID concentrations for a metabolite in the offline HMDB index.

    Tries `name` first, then each alias in order, matching case-insensitively against HMDB
    primary names and synonyms. Returns:
      found:          bool
      matched_name:   HMDB's canonical name for the matched metabolite, or None
      matched_query:  which of name/aliases actually produced the match, or None
      accession:      HMDB accession (e.g. "HMDB0000122"), or None
      records:        list of concentration records, each with biospecimen, value, units,
                      subject_age, subject_sex, subject_condition, reference_text, pubmed_id
      records_dropped_uncited: how many of this metabolite's records were withheld for having no
                      citation. Expected to be 0 -- build_hmdb_index already drops those -- so a
                      non-zero value means the index on disk is older than that build rule.

    These are BIOFLUID concentrations. They are not a substitute for a solid-tissue value: blood
    and liver concentrations of the same metabolite routinely differ by an order of magnitude.
    Callers reporting a tissue number must use lookup_tissue_concentration instead, or label the
    number as the biofluid it actually came from.
    """
    _, by_key = _load_index()

    for candidate in [name, *(aliases or [])]:
        if not candidate:
            continue
        entry = by_key.get(candidate.strip().lower())
        if entry is None:
            continue
        # Defensive, and normally a no-op: build_hmdb_index already refuses to emit a record with
        # no reference_text (46 such rows exist in HMDB 5.0). Re-checking here means the
        # "every number names its source" guarantee holds even if this module is pointed at an
        # index built before that rule, rather than depending on the data file being current.
        cited = [dict(r) for r in entry["concentrations"] if r.get("reference_text")]
        return {
            "found": True,
            "matched_name": entry["name"],
            "matched_query": candidate,
            "accession": entry["accession"],
            "records": cited,
            "records_dropped_uncited": len(entry["concentrations"]) - len(cited),
        }

    return {"found": False, "matched_name": None, "matched_query": None, "accession": None,
            "records": [], "records_dropped_uncited": 0}


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _parse_search_rows(page: str) -> list[dict]:
    """Pull the results table out of a BioNumbers search page. Columns are, in order: Property
    (an <a> to the record), Organism, Value, Units, ID, Details (a truncated reference).
    """
    rows = []
    for row_html in re.findall(r"<tr>(.*?)</tr>", page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        if len(cells) < 6:
            continue
        link = re.search(r"bionumber\.aspx\?id=(\d+)", cells[0])
        if not link:
            continue
        rows.append({
            "bnid": link.group(1),
            "title": _strip_tags(cells[0]),
            "organism": _strip_tags(cells[1]),
            "value": _strip_tags(cells[2]),
            "units": _strip_tags(cells[3]),
            "details": _strip_tags(cells[5]),
        })
    return rows


def _is_plausible_tissue_hit(row: dict, metabolite: str, tissue: str) -> bool:
    """BioNumbers search is a noisy full-database keyword match, so candidate rows are filtered
    rather than trusted. A row is kept only if its title mentions the metabolite AND a
    concentration word AND the requested tissue, and its value actually contains a number.

    The tissue requirement is the important one and is deliberately stricter than "mentions a
    concentration of this metabolite": searching "glucose liver" returns "Plasma glucose
    concentration" as a high-ranking hit, and handing that back as a liver concentration would be
    precisely the fabrication this module exists to prevent. Rows whose Value is a link to an
    attached table or spreadsheet ("Table - link", "Excel Table - link") are dropped too: there is
    no number on the page to ground anything with.

    Known remaining limitation: word-boundary matching correctly rejects a name match inside a
    longer, unrelated compound word (querying "alanine" no longer matches "Phenylalanine";
    "choline" no longer matches "Acetylcholine" or "Phosphatidylcholine"; "taurine" no longer
    matches "Hypotaurine"), but a hyphen counts as a word boundary in Python's \\b, so it cannot
    tell a genuinely different hyphen-prefixed compound ("beta-Alanine", a distinct isomer) from a
    same-compound stereo-prefix naming variant ("L-Alanine", "D-Alanine") -- both would match.
    Reliably telling those apart needs a chemistry-aware name parser, not a regex.
    """
    title = row["title"].lower()
    metabolite = metabolite.strip().lower()
    tissue = tissue.strip().lower()

    # Word-boundary match, not substring: a plain `in` check lets "alanine" match inside
    # "Phenylalanine" or "beta-Alanine", "choline" inside "Acetylcholine" or
    # "Phosphatidylcholine", and "taurine" inside "Hypotaurine" -- returning a real, cited,
    # differently-shaped compound's number dressed up as the queried metabolite's own. That is
    # worse than the ordinary "not found" miss this module is built to prefer: it looks sourced.
    if metabolite and not re.search(rf"\b{re.escape(metabolite)}\b", title):
        return False
    if tissue and not re.search(rf"\b{re.escape(tissue)}\b", title):
        return False
    if not any(word in title for word in _CONCENTRATION_WORDS):
        return False

    value = row["value"].lower()
    if value.startswith("table") or value.startswith("excel table"):
        return False
    return bool(re.search(r"\d", value))


def _parse_reference_cell(cell_html: str) -> tuple[str, str | None]:
    """A Reference / Primary Source cell is free text followed by a 'PubMed ID' marker whose
    value is either a link to /redirect.aspx?pbmid=<pmid> or the literal 'not available online'
    (common for the book chapters BioNumbers cites as primary sources).
    """
    match = re.search(r"pbmid=(\d+)", cell_html)
    pubmed_id = match.group(1) if match else None
    text = _strip_tags(cell_html)
    text = re.sub(r"\s*PubMed ID\s*(?:\d+|not available online)?\s*$", "", text).strip()
    return text, pubmed_id


def _fetch_record_details(bnid: str, timeout: int) -> dict:
    """Fetch one BioNumbers record page for its full Reference / Primary Source / PubMed ID --
    the search results table shows only a truncated author list ("Albe KR, Butler MH, Wright...")
    which is unusable as a citation.
    """
    resp = requests.get(f"{BIONUMBERS_BASE}/bionumber.aspx", params={"id": bnid}, timeout=timeout)
    resp.raise_for_status()
    fields: dict[str, str] = {}
    for label, cell in re.findall(r"<th[^>]*>\s*(.*?)\s*</th>\s*<td[^>]*>(.*?)</td>", resp.text, re.S):
        fields.setdefault(_strip_tags(label), cell)

    reference, pubmed_id = _parse_reference_cell(fields.get("Reference", ""))
    primary_source, _ = _parse_reference_cell(fields.get("Primary Source", ""))
    return {
        "reference": reference or None,
        "pubmed_id": pubmed_id,
        "primary_source": primary_source or None,
    }


def lookup_tissue_concentration(metabolite: str, tissue: str, max_records: int = 3,
                                timeout: int = 20, fetch_details: bool = True) -> dict:
    """Look up a SOLID-TISSUE concentration on BioNumbers (live HTML scrape; there is no API).

    Returns:
      found:   bool
      records: [{"value": str, "organism": str, "reference": str, "pubmed_id": str | None,
                 "primary_source": str | None, "bnid": str, "title": str, "url": str}]
      query:   the search string actually sent, so a caller can reproduce the lookup by hand

    `value` is the value and its units as BioNumbers states them (e.g. "9860-10200 µM"); ranges
    and approximations are kept verbatim rather than collapsed to a single number. `organism` is
    load-bearing and must be carried through to the report: most solid-tissue records here are
    rat or mouse, not human.

    Request budget, to stay polite to a small academic server: one search request, then at most
    `max_records` record-page fetches for the rows that survive filtering (the reference and PMID
    exist only on the record page), spaced apart, with no pagination and no retries. Pass
    fetch_details=False for a strict single-request lookup, in which case `reference` is the
    search page's truncated author list and pubmed_id/primary_source are None.

    A network failure on the search raises (caller decides, per pubchem.py's convention). A
    failure on an individual record page is not fatal: that record keeps the truncated reference
    and a None pubmed_id rather than being dropped or filled in with a guess.
    """
    query = f"{metabolite} {tissue}".strip()
    resp = requests.get(f"{BIONUMBERS_BASE}/search.aspx",
                        params={"task": "searchbytrmorg", "trm": query}, timeout=timeout)
    resp.raise_for_status()

    hits = [row for row in _parse_search_rows(resp.text)
            if _is_plausible_tissue_hit(row, metabolite, tissue)][:max_records]

    records = []
    for i, row in enumerate(hits):
        record = {
            "value": " ".join(p for p in (row["value"], row["units"]) if p and p != "N/A"),
            "organism": row["organism"],
            "reference": row["details"] or None,
            "pubmed_id": None,
            "primary_source": None,
            "bnid": row["bnid"],
            "title": row["title"],
            "url": f"{BIONUMBERS_BASE}/bionumber.aspx?id={row['bnid']}",
        }
        if fetch_details:
            if i:
                time.sleep(0.5)
            try:
                record.update(_fetch_record_details(row["bnid"], timeout))
            except Exception:
                pass  # keep the truncated search-page reference; never invent one
        records.append(record)

    return {"found": bool(records), "records": records, "query": query}
