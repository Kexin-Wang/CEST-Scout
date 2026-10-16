"""The PubMed search tool for the clinical-evidence agent: the same search, but abstracts are
returned up to 3,500 characters instead of 2,000, because group sizes, means and p-values sit in
the Results sentences at the end of a structured abstract, exactly where the shorter cut lands."""
from __future__ import annotations

import copy

from cest_scout.skills import pubmed

ABSTRACT_CHARS = 3500

TOOL_SPEC = copy.deepcopy(pubmed.TOOL_SPEC)
TOOL_SPEC["function"]["description"] = (
    "Search PubMed and return matching articles with their title, year, MeSH-based species and study-setting "
    "hints, and the abstract (up to 3,500 characters, so the Results sentences with group sizes, means +/- SD "
    "and p-values are included). Use precise queries such as 'APT CEST glioma grade MTRasym', "
    "'gluCEST epilepsy', 'glucoCEST tumor', 'gagCEST intervertebral disc degeneration', 'CrCEST myopathy', "
    "'NOE CEST stroke'. Only numbers that appear verbatim in the returned abstract may be reported."
)


def call(args: dict) -> dict:
    results = pubmed.search_pubmed(args["query"], args.get("max_results", 8), truncate_abstract=ABSTRACT_CHARS)
    return {"query": args["query"], "n_results": len(results), "results": results}
