"""ONE-TIME BUILD STEP -- not part of the runtime pipeline.

Streams the full HMDB 5.0 XML dump and emits the compact JSON index that
cest_scout.skills.reference_data loads at runtime. Nothing in the agent pipeline imports this
module; it exists so the committed index is reproducible and so the extraction decisions below
are auditable rather than folklore.

Run it manually (the raw dump must stay OUTSIDE the repo -- see "Redistribution" below):

    mkdir -p /home/wangk6/hmdb
    curl -L -o /home/wangk6/hmdb/hmdb_metabolites.zip \\
        https://www.serummetabolome.ca/system/downloads/current/hmdb_metabolites.zip
    python -m cest_scout.skills.build_hmdb_index /home/wangk6/hmdb/hmdb_metabolites.zip

Where the dump comes from
-------------------------
hmdb.ca itself sits behind a Cloudflare JS challenge and returns 403 for every path, including
its download URLs. The identical full database is served unblocked from the sibling Wishart-lab
hosts www.serummetabolome.ca (and www.csfmetabolome.ca / www.urinemetabolome.ca) at the same
/system/downloads/current/ path: 953,913,010 bytes, HMDB 5.0, Last-Modified 2021-11-17. The zip
holds one entry, hmdb_metabolites.xml, 6.49 GB uncompressed -- hence iterparse with element
clearing, never ET.parse().

What is extracted
-----------------
Per <metabolite>: accession, name, lowercased synonyms, and every <normal_concentrations>/
<concentration> child that has a non-empty <concentration_value>. Each kept record carries its
biospecimen, value string, units, subject age/sex/condition, and the first reference's
<reference_text> + <pubmed_id>. Metabolites with no surviving record are dropped entirely.

The value is kept verbatim as TEXT and is never parsed into a float: HMDB stores "7.7 +/- 1.9",
"0.16 (0.02-0.69)" and bare numbers in the same field, and reducing those to a single number
would silently discard the spread that tells a reader how well-determined the value is.

Two kinds of row are dropped, both for the same reason -- the point of this index is that every
number arrives with a source attached, so a half-record is worse than no record:
  * empty <concentration_value>. HMDB has many of these; they record "this metabolite was
    detected in this biofluid", with a citation but no number.
  * empty <reference_text>. Only 46 of 20,974 otherwise-usable rows, all but one of them urine,
    and exactly one metabolite (1-Hydroxypyrene glucuronide) is lost entirely to this. An
    uncitable number here would be indistinguishable from the LLM recall this module replaces.

Rows that have a reference_text but no <pubmed_id> are KEPT (4,910 of them). They are still
citable -- they are mostly older papers and book chapters that predate PubMed indexing -- they
just cannot be re-checked automatically by cest_scout.skills.pubmed.verify_titles, which is why
_record_sort_key ranks them below rows that do carry a PMID.

Records are emitted in _record_sort_key order rather than HMDB's own, so that a caller reading
only the first few gets Normal-condition, adult, PMID-bearing, blood-first rows, and so that
rebuilding the index from the same dump is byte-identical.

IMPORTANT -- what HMDB does NOT contain
---------------------------------------
Every HMDB biospecimen is a BIOFLUID (Blood, Urine, Feces, Saliva, Cerebrospinal Fluid, Sweat,
Bile, Breast Milk, Amniotic Fluid...). There is no "Liver" biospecimen, and no solid-tissue
biospecimen of any kind. <tissue_locations> does exist but is purely qualitative -- a list of
organ names with no numbers attached -- so it is deliberately NOT indexed: it would look like
tissue grounding while carrying no concentration at all. Solid-tissue concentrations must come
from the separate BioNumbers path in cest_scout.skills.reference_data.

Size / pruning
--------------
NO PRUNING IS APPLIED, because none is needed. The complete extract is 11.0 MiB (11,523,995
bytes): 217,920 metabolites scanned, 3,291 with at least one usable normal concentration, 20,928
concentration records (16,064 of which carry a PMID). That is comfortably under the ~25 MB ceiling for a file committed to the
repo and read on every pipeline run, so the index is the complete set of HMDB normal
concentrations and no biospecimen, subject group or literature source has been filtered out.

(For the record, if a future HMDB release did blow past that ceiling, the two prunes to reach for
-- in this order -- would be dropping the thin, non-comparable exotic biospecimens (Sweat, Bile,
Breast Milk, Amniotic Fluid, Semen, Aqueous Humour, ...) and then capping records per metabolite,
keeping _record_sort_key's leaders and round-robining across biospecimens so a blood-heavy
metabolite does not lose its only CSF row. Neither is implemented, since implementing a filter
that is not currently needed only creates a way for the shipped data to quietly become
incomplete.)

Redistribution
--------------
The 954 MB raw dump must never enter the repo. Keeping it in /home/wangk6/hmdb/ puts it outside
the working tree, so .gitignore does not need a rule for it. The derived index committed at
cest_scout/data/hmdb_concentrations.json carries the source attribution -- HMDB accession plus
the full literature reference and PMID -- on every single number, which is the acknowledgment
HMDB asks for. Note that HMDB's terms-of-use text could not be read from an unblocked host (it
lives on hmdb.ca/about, which 403s; it is not in the zip, not in the XML, and not on the
serummetabolome.ca mirror pages), so those terms have NOT been confirmed first-hand -- see the
note in reference_data's module docstring.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

NS = "{http://www.hmdb.ca}"

DEFAULT_ZIP = "/home/wangk6/hmdb/hmdb_metabolites.zip"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "hmdb_concentrations.json")

SOURCE_URL = "https://www.serummetabolome.ca/system/downloads/current/hmdb_metabolites.zip"


def _text(parent: ET.Element, tag: str) -> str:
    el = parent.find(NS + tag)
    if el is None or el.text is None:
        return ""
    return el.text.strip()


def _record_sort_key(record: dict) -> tuple:
    """Rank concentration records so the most useful ones lead the list. Lower sorts first.

    Priority: Normal subject_condition, then adult subjects, then records that actually carry a
    PMID (a bare reference_text with no PMID cannot be re-verified by
    cest_scout.skills.pubmed.verify_titles), then blood before the other fluids. Ties fall back to
    the value string purely so the build is deterministic.
    """
    condition = (record.get("subject_condition") or "").lower()
    age = (record.get("subject_age") or "").lower()
    biospecimen = (record.get("biospecimen") or "").lower()
    return (
        0 if condition == "normal" else 1,
        0 if age.startswith("adult") else 1,
        0 if record.get("pubmed_id") else 1,
        0 if biospecimen == "blood" else 1,
        record.get("value") or "",
    )


def _parse_metabolite(elem: ET.Element) -> dict | None:
    """Turn one <metabolite> element into an index entry, or None if it has no usable
    concentration data.
    """
    container = elem.find(NS + "normal_concentrations")
    if container is None:
        return None

    records: list[dict] = []
    for conc in container.findall(NS + "concentration"):
        value = _text(conc, "concentration_value")
        if not value:
            continue  # detection-only row: a citation but no number, so it grounds nothing

        reference_text, pubmed_id = "", None
        first_ref = conc.find(NS + "references/" + NS + "reference")
        if first_ref is not None:
            reference_text = _text(first_ref, "reference_text")
            pubmed_id = _text(first_ref, "pubmed_id") or None
        if not reference_text:
            continue  # a number with no source is what this module exists to replace

        records.append({
            "biospecimen": _text(conc, "biospecimen"),
            "value": value,
            "units": _text(conc, "concentration_units"),
            "subject_age": _text(conc, "subject_age"),
            "subject_sex": _text(conc, "subject_sex"),
            "subject_condition": _text(conc, "subject_condition"),
            "reference_text": reference_text,
            "pubmed_id": pubmed_id,
        })

    if not records:
        return None

    synonyms = sorted({
        s.text.strip().lower()
        for s in elem.iterfind(NS + "synonyms/" + NS + "synonym")
        if s.text and s.text.strip()
    })

    return {
        "accession": _text(elem, "accession"),
        "name": _text(elem, "name"),
        "synonyms": synonyms,
        "concentrations": sorted(records, key=_record_sort_key),
    }


def build_index(zip_path: str, out_path: str) -> dict:
    """Stream the HMDB zip and write the JSON index. Returns build statistics."""
    entries: list[dict] = []
    n_metabolites = 0

    with zipfile.ZipFile(zip_path) as archive:
        with archive.open("hmdb_metabolites.xml") as stream:
            context = ET.iterparse(stream, events=("start", "end"))
            _, root = next(context)  # the <hmdb> root, kept only so it can be cleared
            for event, elem in context:
                if event != "end" or elem.tag != NS + "metabolite":
                    continue
                n_metabolites += 1
                entry = _parse_metabolite(elem)
                if entry:
                    entries.append(entry)
                # Both calls are needed: elem.clear() frees this metabolite's subtree, and
                # root.clear() stops the root from accumulating 200k+ drained <metabolite>
                # shells. Without them the 6.49 GB document is retained in memory.
                elem.clear()
                root.clear()

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    payload = {
        "source": f"HMDB 5.0 (hmdb_metabolites.xml), downloaded from {SOURCE_URL}",
        "note": "Normal (non-disease) concentrations only. All biospecimens are BIOFLUIDS -- "
                "HMDB has no solid-tissue concentrations.",
        "built_by": "cest_scout.skills.build_hmdb_index",
        "metabolites": entries,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)

    return {
        "metabolites_scanned": n_metabolites,
        "metabolites_with_concentrations": len(entries),
        "concentration_records": sum(len(e["concentrations"]) for e in entries),
        "bytes": os.path.getsize(out_path),
        "out_path": out_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the HMDB concentration index (one-off).")
    parser.add_argument("zip_path", nargs="?", default=DEFAULT_ZIP)
    parser.add_argument("-o", "--out", default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    stats = build_index(args.zip_path, args.out)
    for key, value in stats.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
