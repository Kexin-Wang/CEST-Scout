"""PubMed search tool via NCBI E-utilities (no API key required).

Used by both the literature agent (initial metabolite discovery) and the
physicist agent (finding MRS/CEST-specific verification papers). Returns
each record's MeSH headings so callers can determine whether the supporting
evidence is from an animal model or human subjects/tissue, rather than
guessing from the abstract text.
"""
from __future__ import annotations

import difflib
import html
import re
import time
import xml.etree.ElementTree as ET

import requests

from cest_scout.config import load_settings

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

_ANIMAL_MESH_HINTS = {
    "animals", "rats", "mice", "rabbits", "dogs", "swine", "mice, inbred c57bl",
    "disease models, animal", "rats, sprague-dawley", "rats, wistar", "primates",
    "macaca mulatta", "guinea pigs", "cricetinae",
}

# MeSH species/strain heading -> a specific, human-readable species name, so callers don't have
# to guess a concrete organism from the generic "Animals" heading alone.
_MESH_SPECIES_NAMES = {
    "humans": "human",
    "rats": "rat",
    "rats, sprague-dawley": "Sprague-Dawley rat",
    "rats, wistar": "Wistar rat",
    "rats, inbred lew": "Lewis rat",
    "mice": "mouse",
    "mice, inbred c57bl": "C57BL/6 mouse",
    "mice, nude": "nude mouse",
    "mice, sv129": "129/Sv mouse",
    "rabbits": "rabbit",
    "dogs": "dog",
    "swine": "pig",
    "swine, miniature": "minipig",
    "primates": "non-human primate",
    "macaca mulatta": "rhesus macaque",
    "guinea pigs": "guinea pig",
    "cricetinae": "hamster",
    "zebrafish": "zebrafish",
}

# Title/abstract phrases that indicate the measurement was made outside a living organism, so the
# physicist agent can flag that any exchange rate reported there may not equal the in vivo rate.
_EX_VIVO_HINTS = (
    "ex vivo", "excised", "postmortem", "post-mortem", "biopsy", "resected tissue",
)
_PHANTOM_OR_INVITRO_HINTS = (
    "phantom", "in vitro", "aqueous solution", "solution phantom", "cell culture", "cultured cells",
)

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "search_pubmed",
        "description": (
            "Search PubMed and return matching articles (title, journal, year, abstract, whether "
            "the evidence is animal- or human-based per MeSH headings, a best-effort specific "
            "species guess from MeSH, e.g. 'Sprague-Dawley rat', and a best-effort study_setting_hint "
            "e.g. 'human in vivo' / 'animal in vivo' / 'ex vivo tissue/phantom' / 'in vitro/solution "
            "phantom'). These hints are not authoritative -- confirm species, subject count, and "
            "setting against the actual title/abstract text before reporting them.) "
            "Use precise queries, e.g. '\"prefrontal cortex\"[tiab] AND glutamate[tiab] AND concentration[tiab]', "
            "or 'CEST[tiab] AND creatine[tiab] AND muscle[tiab]'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "PubMed query string."},
                "max_results": {"type": "integer", "description": "Max articles to return (default 8, max 20).", "default": 8},
            },
            "required": ["query"],
        },
    },
}


def _rate_limit_sleep(api_key: str | None) -> None:
    time.sleep(0.11 if api_key else 0.35)


_RETRY_STATUSES = (429, 500, 502, 503, 504)


def _get(url: str, params: dict, timeout: int) -> requests.Response:
    """GET with a short exponential backoff on 429/5xx. Without an NCBI API key the limit is
    3 requests/s, and a run's own esearch+efetch bursts plus title verification trip it; a 429
    used to surface as a tool error the model then had to work around."""
    delay = 1.0
    for attempt in range(4):
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code in _RETRY_STATUSES and attempt < 3:
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")


def _common_params() -> dict:
    settings = load_settings()
    params = {"email": settings.ncbi_email, "tool": "cest_scout"}
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key
    return params


def _esearch(query: str, retmax: int) -> list[str]:
    settings = load_settings()
    # field="tiab" (title/abstract) is the default field for any *untagged* term in the
    # query; explicit tags like [MeSH Terms] in the query still work (NCBI honors them
    # over the default). Without this, PubMed's Automatic Term Mapping can badly misparse
    # ordinary phrases -- e.g. "...in human brain" gets read as an author-name search
    # ("Human, In"[Full Author Name]) and silently returns zero hits.
    params = {
        **_common_params(), "db": "pubmed", "term": query, "retmax": retmax,
        "sort": "relevance", "field": "tiab",
    }
    resp = _get(f"{EUTILS}/esearch.fcgi", params, timeout=20)
    root = ET.fromstring(resp.content)
    pmids = [el.text for el in root.findall(".//IdList/Id") if el.text]
    _rate_limit_sleep(settings.ncbi_api_key)
    return pmids


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _efetch(pmids: list[str], truncate_abstract: int | None = 2000) -> list[dict]:
    if not pmids:
        return []
    settings = load_settings()
    params = {**_common_params(), "db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "xml"}
    resp = _get(f"{EUTILS}/efetch.fcgi", params, timeout=30)
    root = ET.fromstring(resp.content)
    _rate_limit_sleep(settings.ncbi_api_key)

    records = []
    for article in root.findall(".//PubmedArticle"):
        pmid = _text(article.find(".//PMID"))
        title = _text(article.find(".//ArticleTitle"))
        journal = _text(article.find(".//Journal/Title"))
        year_el = article.find(".//JournalIssue/PubDate/Year")
        if year_el is None:
            year_el = article.find(".//JournalIssue/PubDate/MedlineDate")
        year_text = _text(year_el)
        year = None
        for tok in year_text.replace("-", " ").split():
            if tok.isdigit() and len(tok) == 4:
                year = int(tok)
                break

        abstract_parts = [_text(a) for a in article.findall(".//Abstract/AbstractText")]
        abstract = " ".join(p for p in abstract_parts if p)

        mesh_terms = [
            _text(mh.find("DescriptorName"))
            for mh in article.findall(".//MeshHeadingList/MeshHeading")
        ]
        mesh_lower = {m.lower() for m in mesh_terms if m}
        is_human = "humans" in mesh_lower
        is_animal = bool(mesh_lower & _ANIMAL_MESH_HINTS)

        species_hints = sorted({_MESH_SPECIES_NAMES[m] for m in mesh_lower if m in _MESH_SPECIES_NAMES})

        haystack = f"{title} {abstract}".lower()
        setting_hint = "unspecified"
        if is_human and not is_animal:
            setting_hint = "human in vivo"
        elif any(h in haystack for h in _EX_VIVO_HINTS):
            setting_hint = "ex vivo tissue/phantom"
        elif any(h in haystack for h in _PHANTOM_OR_INVITRO_HINTS):
            setting_hint = "in vitro/solution phantom"
        elif is_animal:
            setting_hint = "animal in vivo"

        records.append({
            "pmid": pmid,
            "title": title,
            "journal": journal,
            "year": year,
            "abstract": abstract if truncate_abstract is None else abstract[:truncate_abstract],
            "mesh_terms": mesh_terms,
            "is_human_evidence": is_human,
            "is_animal_evidence": is_animal and not is_human,
            # Best-effort grounding hints for the LLM's Citation.species / study_setting fields --
            # NOT authoritative (species/subject-count wording varies too much to parse reliably),
            # but they steer the model away from just writing "animal" or guessing the setting.
            "species_hints_from_mesh": species_hints,
            "study_setting_hint": setting_hint,
        })
    return records


def search_pubmed(query: str, max_results: int = 8, truncate_abstract: int | None = 2000) -> list[dict]:
    max_results = max(1, min(int(max_results), 20))
    pmids = _esearch(query, max_results)
    return _efetch(pmids, truncate_abstract=truncate_abstract)


def call(args: dict) -> dict:
    results = search_pubmed(args["query"], args.get("max_results", 8))
    return {"query": args["query"], "n_results": len(results), "results": results}


# PubMed IDs are dense enough that almost any digit string in a plausible range resolves to a
# real, unrelated paper -- so a model that fabricates a citation instead of transcribing a real
# search_pubmed result usually still produces a PMID that "exists". Checking title identity
# (not just PMID existence) is what actually catches this. 0.6 tolerates minor
# capitalization/punctuation drift between what the model copied and PubMed's raw ArticleTitle
# while still rejecting an unrelated title outright (observed ratios for real fabrications were
# well under 0.3).
_TITLE_MATCH_RATIO_THRESHOLD = 0.6


def verify_titles(citations: list[tuple[str, str]]) -> dict[str, dict]:
    """For each (pmid, claimed_title) pair, look up the PMID's real title via NCBI efetch and
    compare it against what was claimed. Returns {pmid: {"verified": bool, "real_title": str |
    None}} (real_title is None if the PMID has no matching PubMed record at all).
    """
    pmids = sorted({pmid for pmid, _ in citations if pmid})
    real_titles = {r["pmid"]: r["title"] for r in _efetch(pmids) if r["title"]}
    result: dict[str, dict] = {}
    for pmid, claimed_title in citations:
        real_title = real_titles.get(pmid)
        if not real_title:
            result[pmid] = {"verified": False, "real_title": None}
            continue
        ratio = difflib.SequenceMatcher(None, claimed_title.strip().lower(), real_title.strip().lower()).ratio()
        result[pmid] = {"verified": ratio >= _TITLE_MATCH_RATIO_THRESHOLD, "real_title": real_title}
    return result


def drop_unverified_citations(citations: list, verdicts_map: dict) -> tuple[list, list[tuple[str, str, str | None]]]:
    """Split a list of Citation-like objects (needing .pmid and .title) into (kept, dropped)
    against a verify_titles() result. dropped is a list of (pmid, claimed_title, real_title) for
    citations that failed verification (real_title is None if the PMID doesn't exist at all).
    Shared by every agent that cites PubMed articles (literature/physicist/experimentalist), so
    the drop policy and message wording stay identical everywhere it's applied.
    """
    kept, dropped = [], []
    for c in citations:
        v = verdicts_map.get(c.pmid)
        if v and v["verified"]:
            kept.append(c)
        else:
            dropped.append((c.pmid, c.title, v["real_title"] if v else None))
    return kept, dropped


def format_drop_notes(dropped: list[tuple[str, str, str | None]], field_name: str = "citations") -> str:
    notes = []
    for pmid, claimed, real in dropped:
        real_desc = f"PubMed's actual title is {real!r}" if real else "no PubMed record matches that PMID"
        notes.append(f"{field_name}: PMID {pmid} (claimed as {claimed!r}) dropped -- {real_desc}.")
    return " ".join(notes)


def _normalize_for_match(text: str) -> str:
    text = text.lower()
    text = text.replace("±", "+/-").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def verify_quote(pmid: str, quote: str) -> dict:
    """Check whether `quote` genuinely appears (verbatim, modulo whitespace/punctuation
    normalization) in PMID's title+abstract -- the only text search_pubmed actually exposes to
    the agents, so any text claimed as a real "verbatim" excerpt must be traceable there. A model
    can cite a real, verified PMID/title and still invent the supporting sentence itself, so
    citation-identity verification (verify_titles) alone does not catch this.
    Returns {"verified": bool, "reason": str | None}.
    """
    quote = (quote or "").strip()
    if not quote:
        return {"verified": False, "reason": "empty quote"}
    try:
        records = _efetch([pmid], truncate_abstract=None)
    except Exception as exc:
        return {"verified": False, "reason": f"could not fetch PMID {pmid}: {exc}"}
    if not records:
        return {"verified": False, "reason": "no matching PubMed record"}
    haystack = _normalize_for_match(f"{records[0]['title']} {records[0]['abstract']}")
    needle = _normalize_for_match(quote)
    if needle in haystack:
        return {"verified": True, "reason": None}
    return {"verified": False, "reason": "quote text not found in the cited title/abstract"}


def _compact(text: str) -> str:
    """_normalize_for_match with ALL whitespace removed: tag-stripped full text writes k<sub>B</sub>
    as 'kB' or 'k B' depending on the stripping, and a verbatim quote must match either way."""
    return re.sub(r"\s+", "", _normalize_for_match(text.replace(" ", " ").replace(" ", " ")))


def _pmc_id(pmid: str) -> str | None:
    params = {**_common_params(), "dbfrom": "pubmed", "db": "pmc", "id": pmid, "retmode": "json"}
    data = _get(f"{EUTILS}/elink.fcgi", params, timeout=20).json()
    _rate_limit_sleep(load_settings().ncbi_api_key)
    for linkset in data.get("linksets", []):
        for db in linkset.get("linksetdbs", []):
            if db.get("linkname") == "pubmed_pmc" and db.get("links"):
                return str(db["links"][0])
    return None


def fetch_fulltext(pmid: str) -> dict:
    """The open-access full text of a PMID from PubMed Central, tag-stripped, with tables kept as
    row text: {"pmcid": "PMC...", "text": str} or {"pmcid": str | None, "text": None, "reason": str}."""
    try:
        pmcid = _pmc_id(pmid)
        if not pmcid:
            return {"pmcid": None, "text": None, "reason": "no PubMed Central full text"}
        xml = _get(f"{EUTILS}/efetch.fcgi", {**_common_params(), "db": "pmc", "id": pmcid}, timeout=60).text
        _rate_limit_sleep(load_settings().ncbi_api_key)
    except Exception as exc:
        return {"pmcid": None, "text": None, "reason": f"could not fetch full text for PMID {pmid}: {exc}"}
    if "<body" not in xml:
        return {"pmcid": f"PMC{pmcid}", "text": None, "reason": "PMC record has no open-access body"}
    # Table cells become " | "-separated row text so a model can read them and a quote can match them.
    xml = re.sub(r"</t[dh]>", " | ", xml)
    xml = re.sub(r"</tr>|</title>|</p>|</sec>", "\n", xml)
    body = html.unescape(re.sub(r"<[^>]+>", "", xml))
    return {"pmcid": f"PMC{pmcid}", "text": re.sub(r"[ \t]+", " ", body)}


def verify_fulltext_quote(pmid: str, quote: str) -> dict:
    """Like verify_quote, but against the article's open-access full text in PubMed Central --
    for numbers a paper states only in its body (exchange rates in a results table, say). A
    number verified this way is labelled as full-text, not abstract, wherever it is used.
    Returns {"verified": bool, "pmcid": str | None, "reason": str | None}."""
    quote = (quote or "").strip()
    if not quote:
        return {"verified": False, "pmcid": None, "reason": "empty quote"}
    ft = fetch_fulltext(pmid)
    if not ft.get("text"):
        return {"verified": False, "pmcid": ft.get("pmcid"), "reason": ft.get("reason")}
    if _compact(quote) in _compact(ft["text"]):
        return {"verified": True, "pmcid": ft["pmcid"], "reason": None}
    return {"verified": False, "pmcid": ft["pmcid"], "reason": "quote text not found in the PMC full text"}
