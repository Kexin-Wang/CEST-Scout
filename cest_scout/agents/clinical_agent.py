"""Agent 5: clinical evidence. For every candidate pool, finds the diseases in which that
metabolite's CEST/NOE signal has been applied, and records per abstract: disease, species, group
sizes, the metric's group means with SD, direction and p value -- each with the verbatim sentence.
Deterministic steps then verify every PMID and quote, null any number that is not in its quote,
count humans and animals, and pool group differences (Hedges' g, random effects) where at least
three verified studies with complete statistics exist for one pool and disease category.
"""
from __future__ import annotations

import json
import re

from openai import OpenAI
from pydantic import BaseModel, Field

from cest_scout.llm import ToolCallback, _record_usage, run_research_agent
from cest_scout.schemas import ClinicalEvidence, ClinicalReport, ClinicalStudy, LiteratureReport, PhysicistReport, PooledEffect
from cest_scout.skills import meta_analysis, pubmed, pubmed_clinical

SYSTEM_PROMPT = """\
You are a clinical MRI scientist assessing the potential clinical impact of CEST / NOE MRI signals, \
one candidate metabolite (or macromolecular pool) at a time, from the published literature.

For EACH candidate you are given, search PubMed (search_pubmed, 2-4 targeted queries per candidate) for \
TWO kinds of evidence, in patients, in animal models, or ex vivo:
(a) evidence_type "cest_application": studies that measured THIS metabolite's CEST or NOE signal in a \
    disease (e.g. "APT MRI glioma grading", "gluCEST epilepsy", "glucoCEST tumor", "CrCEST myopathy", \
    "gagCEST disc degeneration", "NOE MRI stroke");
(b) evidence_type "metabolite_change": studies by ANY other method showing that the metabolite itself \
    changes in a disease -- MRS, metabolomics, histology, biochemistry, enzyme assays, observational \
    cohorts or mechanistic / fundamental-biology work in cells or animals (e.g. "<metabolite> MRS \
    <disease>", "<metabolite> metabolomics <disease>", "<metabolite> depletion <disease> model"). \
    These point to where the CEST signal could be applied even if nobody has imaged it yet.
Treat phosphocreatine (PCr) and creatine (Cr) as DIFFERENT candidates with their own evidence, never merged, \
and respect what each method can measure: 31P MRS measures PCr (and ATP, Pi) but NEVER free creatine -- \
search it for PCr ("31P MRS phosphocreatine <tissue> <disease>", "PCr/ATP ratio heart failure") and file it \
only under PCr; 1H MRS at clinical field strengths measures only TOTAL creatine (Cr + PCr) and cannot \
separate the two -- file a 1H-MRS creatine result under creatine with measurement "1H-MRS total creatine \
(Cr + PCr)" and the note "does not separate Cr from PCr", never as PCr evidence and never as free-creatine- \
specific evidence; CrCEST and PCrCEST are the creatine- and PCr-specific imaging measures. Likewise keep \
glutamate and glutamine (Glx), and NAA and NAAG, apart.
Report every relevant study as a ClinicalStudy with:
- evidence_type; disease (as the abstract names it) and disease_category (EXACTLY one of tumor, stroke, \
  neurodegeneration, epilepsy, psychiatric, metabolic, musculoskeletal, cardiovascular, inflammation, other);
- for (a) cest_metric (e.g. "MTRasym at 3.5 ppm", "APTw", "NOE(-3.5 ppm)"); for (b) measurement (method \
  and quantity, e.g. "1H-MRS glutamate (mM)", "LC-MS tissue lactate"); species, study_setting, field strength;
- n_disease and n_control ONLY if the abstract states them; disease_mean / disease_sd / control_mean / \
  control_sd ONLY if the abstract states those numbers (mark spread_is_sem when the abstract says SEM); \
  p_value_text as written; direction (higher/lower in disease, no difference, not stated);
- quote: the VERBATIM sentence(s) from the abstract that carry the result and every number you report. \
  Every PMID and every quote is checked against PubMed; a number that is not in the quote is discarded.
Leave a number null rather than estimating it. A review or methods paper with no patient/animal/cell \
result is not a study. If you find nothing for a candidate, return it with an empty studies list and say \
so in search_note. Keep summary under 80 words and say which diseases have CEST evidence and which only \
metabolite-change evidence. Return a ClinicalEvidence entry for EVERY candidate given.
"""

_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
_UNITS = {w: i for i, w in enumerate(("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                      "fifteen sixteen seventeen eighteen nineteen").split())}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SPELLED_RE = re.compile(r"\b(" + "|".join([*_TENS, *_UNITS]) + r")(?:[- ](" + "|".join(_UNITS) + r"))?(?: (hundred|thousand))?\b", re.I)


def _numbers_in(text: str) -> set[float]:
    """Every number in a text, as digits ('20', '3.2', '1,28') or spelled out ('Twenty', 'forty-five',
    'two hundred') -- group sizes often open a sentence as a word."""
    text = text.replace(",", ".")
    found = {float(x) for x in _NUMBER_RE.findall(text)}
    for m in _SPELLED_RE.finditer(text):
        a, b, scale = m.group(1).lower(), (m.group(2) or "").lower(), (m.group(3) or "").lower()
        n = _TENS.get(a, _UNITS.get(a, 0)) + (_UNITS.get(b, 0) if b else 0)
        found.add(float(n * {"hundred": 100, "thousand": 1000}.get(scale, 1)))
    return found
# A methods or normal-physiology paper is not a disease application, however the model files it.
# A review, meta-analysis or editorial is not a primary study, however the model files it.
_NOT_PRIMARY_RE = re.compile(r"\b(systematic )?review\b|\bmeta-?analys[ie]s\b|\beditorial\b|\bcommentary\b|\bnot a primary study\b", re.I)
_NOT_A_DISEASE_RE = re.compile(r"^\s*(healthy|normal)\b|\b(healthy|normal) (volunteers?|subjects?|controls?|brain|tissue)\b|\bphantom\b|\bnone\b", re.I)


def run(
    client: OpenAI, model: str, literature_report: LiteratureReport, physicist_report: PhysicistReport,
    on_tool_call: ToolCallback | None = None, extra_body: dict | None = None, stats_sink: dict | None = None,
    fulltext: bool = True,
) -> ClinicalReport:
    verdict_by_name = {v.metabolite_name: v for v in physicist_report.verdicts}
    candidates = []
    for m in literature_report.metabolites:
        v = verdict_by_name.get(m.name)
        candidates.append({
            "name": m.name, "aliases": m.aliases,
            "simulated_detectable_fields_T": sorted(float(k) for k, c in (v.simulated_contrast_percent.items() if v else []) if c >= 0.5),
        })
    user_prompt = (
        f"Body part / tissue: {literature_report.body_part}\n\n"
        "Candidate metabolites / pools (with the field strengths at which the pipeline's simulation found "
        "them detectable, for context only):\n"
        f"{json.dumps(candidates, indent=2)}\n\n"
        "For each, find (a) the diseases in which its CEST/NOE signal has been applied and (b) the diseases in "
        "which the metabolite itself is shown to change by any method, in humans and in animals, and produce a "
        "ClinicalEvidence entry."
    )
    report = run_research_agent(
        client=client, model=model, system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
        skills=[pubmed_clinical], schema_model=ClinicalReport,
        max_tool_iters=max(12, 3 * len(candidates) + 4), on_tool_call=on_tool_call, extra_body=extra_body,
        output_check=_make_completeness_check([c["name"] for c in candidates]), stats_sink=stats_sink,
        coverage_check=_make_coverage_check([(c["name"], c["aliases"]) for c in candidates]),
    )
    verify(report)
    if fulltext:
        enrich_from_fulltext(report, client, model, extra_body=extra_body, stats_sink=stats_sink)
    return report


# --- Full-text mode -------------------------------------------------------------------------
#
# Abstracts rarely state means, SDs and both group sizes together, so pooling almost never reaches
# three studies from abstracts alone. For a verified study whose statistics are incomplete, the
# open-access full text (PubMed Central) is read: the model extracts the missing numbers with a
# verbatim passage, and the pipeline accepts them only if that passage and every number are in the
# full text. Only fields the abstract left empty are filled; abstract-verified values are kept.

_STAT_FIELDS = ("n_disease", "n_control", "disease_mean", "disease_sd", "control_mean", "control_sd")
FULLTEXT_EXCERPT_CHARS = 40000


class FullTextStats(BaseModel):
    n_disease: int | None = None
    n_control: int | None = None
    disease_mean: float | None = None
    disease_sd: float | None = None
    control_mean: float | None = None
    control_sd: float | None = None
    spread_is_sem: bool = False
    unit: str = ""
    quote: str = Field(default="", description="VERBATIM passage(s) or table row text carrying every number given.")
    note: str = Field(default="", description="e.g. 'median and IQR only', 'contralateral region as control', or ''.")


FULLTEXT_PROMPT = """\
You are given the open-access full text of a paper (Methods, Results and tables; other sections may be \
cut). The study is: {disease} -- {metric} -- {species}, {setting}. The abstract already gave: {known}.
From the full text, report the MISSING group statistics for the comparison disease vs control: the \
number of subjects (or animals) per group and the {metric} as mean and SD per group (set spread_is_sem \
if the paper reports SEM; if only median/IQR or a figure is given, leave the means null and say so in \
note). Copy every number from a sentence or table row and put that text VERBATIM in quote -- the quote \
and every number are checked against the full text, and anything not found is discarded. Answer ONLY \
with JSON for this schema: {schema}

FULL TEXT:
{text}"""


def _incomplete(s: ClinicalStudy) -> bool:
    return any(getattr(s, f) is None for f in _STAT_FIELDS)


def _results_excerpt(text: str, limit: int = FULLTEXT_EXCERPT_CHARS) -> str:
    """Methods + Results + tables first; the rest only if room remains."""
    low = text.lower()
    starts = [m.start() for m in re.finditer(r"\n\s*(methods|materials and methods|results|results and discussion|table \d)", low)]
    if not starts:
        return text[:limit]
    head = text[starts[0]:]
    disc = re.search(r"\n\s*(discussion|conclusions?)\s*\n", head.lower())
    core = head[:disc.start()] if disc and disc.start() > 2000 else head
    return core[:limit] if len(core) >= limit // 4 else (core + "\n" + text)[:limit]


def _ask_fulltext(client, model, extra_body, stats_sink, study: ClinicalStudy, text: str) -> FullTextStats | None:
    known = ", ".join(f"{f}={getattr(study, f)}" for f in _STAT_FIELDS if getattr(study, f) is not None) or "nothing"
    prompt = FULLTEXT_PROMPT.format(disease=study.disease, metric=study.cest_metric or study.measurement or "metabolite level",
                                    species=study.species or "?", setting=study.study_setting or "?", known=known,
                                    schema=json.dumps(FullTextStats.model_json_schema()["properties"]), text=_results_excerpt(text))
    kwargs = {"extra_body": extra_body} if extra_body else {}
    resp = client.chat.completions.create(model=model, response_format={"type": "json_object"},
                                          messages=[{"role": "user", "content": prompt}], **kwargs)
    if stats_sink is not None:
        _record_usage(stats_sink, resp)
    raw = resp.choices[0].message.content or ""
    try:
        return FullTextStats.model_validate(json.loads(raw))
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            return FullTextStats.model_validate(json.loads(m.group(0))) if m else None
        except Exception:
            return None


def enrich_from_fulltext(report: ClinicalReport, client, model, extra_body=None, stats_sink=None,
                         fetch=None, ask=None) -> None:
    """Fill missing group statistics from PubMed Central full texts, verified, then re-count and
    re-pool. `fetch(pmid)` and `ask(study, text)` are injectable for tests."""
    fetch = fetch or pubmed.fetch_fulltext
    ask = ask or (lambda study, text: _ask_fulltext(client, model, extra_body, stats_sink, study, text))
    for e in report.entries:
        for s in e.studies:
            if not _incomplete(s):
                continue
            ft = fetch(s.pmid)
            if not ft.get("text"):
                s.fulltext_note = ft.get("reason") or "no open-access full text"
                continue
            got = ask(s, ft["text"])
            if got is None or not got.quote.strip():
                s.fulltext_note = f"{ft['pmcid']}: the model returned no usable statistics"
                continue
            compact = pubmed._compact(ft["text"])
            if pubmed._compact(got.quote) not in compact:
                s.fulltext_note = f"{ft['pmcid']}: the quoted passage is not in the full text; discarded"
                continue
            present = _numbers_in(ft["text"])
            filled, rejected = [], []
            for f in _STAT_FIELDS:
                if getattr(s, f) is not None or getattr(got, f) is None:
                    continue
                if any(abs(float(getattr(got, f)) - v) < 1e-9 for v in present):
                    setattr(s, f, getattr(got, f)); filled.append(f)
                else:
                    rejected.append(f)
            if filled:
                s.stats_source = f"full text ({ft['pmcid']})"
                s.fulltext_quote = got.quote.strip()
                if got.spread_is_sem and ("disease_sd" in filled or "control_sd" in filled):
                    s.spread_is_sem = True
                if got.unit and not s.unit:
                    s.unit = got.unit
                if got.note:
                    s.note = f"{s.note}; {got.note}".strip("; ")
            s.fulltext_note = (f"{ft['pmcid']}: filled {', '.join(filled) or 'nothing'}"
                               + (f"; {', '.join(rejected)} not in the full text, discarded" if rejected else "")
                               + ("" if filled or rejected else "; statistics not stated"))
        _count(e)
        _pool(e)


def _make_coverage_check(candidates: list[tuple[str, list[str]]]):
    """Send the model back to work if it answers without having searched PubMed for every candidate:
    a model that makes no searches and still writes "Searched for ..." notes was observed (Qwen3.6,
    skeletal muscle, 2026-10-13). A candidate counts as searched when its name or an alias occurs
    in a query."""
    def searched(name: str, aliases: list[str], queries: list[str]) -> bool:
        keys = [k.lower() for k in [name, *aliases] if k and len(k.strip()) >= 3]
        head = re.split(r"[(/,;:]", name)[0].strip().lower()
        if len(head) >= 3:
            keys.append(head)
        return any(k in q for k in keys for q in queries)

    def check(trace: list[dict]) -> str | None:
        queries = [str((t.get("args") or {}).get("query", "")).lower() for t in trace if t.get("name") == "search_pubmed"]
        missing = [name for name, aliases in candidates if not searched(name, aliases, queries)]
        if not missing:
            return None
        return (f"You have not searched PubMed for: {', '.join(missing)}. Run at least one search_pubmed query "
                "for each of them (CEST/NOE application and metabolite change in disease) before writing the final JSON; "
                "do not describe searches you did not run.")
    return check


def _make_completeness_check(expected_names: list[str]):
    def check(report: ClinicalReport) -> str | None:
        got = {e.metabolite_name.strip().lower() for e in report.entries}
        missing = [n for n in expected_names if n.strip().lower() not in got]
        if not missing:
            return None
        return (f"Your JSON is missing a ClinicalEvidence entry for: {', '.join(missing)}. Every candidate needs "
                "one, with an empty studies list and a search_note if nothing was found. Re-emit the complete JSON.")
    return check


def _abstract_text(pmid: str) -> str | None:
    """Normalized title + full abstract of a PMID (None when PubMed has no record)."""
    records = pubmed._efetch([pmid], truncate_abstract=None)
    if not records:
        return None
    return pubmed._normalize_for_match(f"{records[0]['title']} {records[0]['abstract']}")


def _numbers_missing(study, abstract: str) -> list[str]:
    """Names of the numeric fields whose value appears nowhere in the verified abstract. Group
    sizes usually sit in a Methods sentence and the result in a Results sentence, so the whole
    abstract is the reference, not the single quoted sentence."""
    present = _numbers_in(abstract)
    missing = []
    for name in ("n_disease", "n_control", "disease_mean", "disease_sd", "control_mean", "control_sd", "field_strength_T"):
        value = getattr(study, name)
        if value is None:
            continue
        if not any(abs(float(value) - p) < 1e-9 for p in present):
            missing.append(name)
    return missing


def verify(report: ClinicalReport) -> None:
    """Deterministic: PMID -> real title, quote in that abstract, every number in that abstract, counts, pooling.
    Studies failing the title or quote check are dropped and counted in n_studies_dropped."""
    pairs = [(s.pmid, s.title) for e in report.entries for s in e.studies if s.pmid]
    try:
        verdicts = pubmed.verify_titles(pairs) if pairs else {}
    except Exception as exc:  # network unavailable: nothing is verified, and the report says so
        for e in report.entries:
            e.verification_note = f"Verification skipped: {exc}"
            e.studies = []
            e.pooled_effects = []
        return
    for e in report.entries:
        kept, dropped = [], []
        for s in e.studies:
            s.numbers_note = ""                       # pipeline-only field; the model's text goes in `note`
            if _NOT_A_DISEASE_RE.search(s.disease or "") or (s.study_setting or "").strip().lower() == "phantom":
                dropped.append((s.pmid, f"not a disease application ({s.disease!r}, {s.study_setting or 'setting not stated'})"))
                continue
            if _NOT_PRIMARY_RE.search(s.title or "") or _NOT_PRIMARY_RE.search(s.note or ""):
                dropped.append((s.pmid, "not a primary study (review, meta-analysis or editorial)"))
                continue
            v = verdicts.get(s.pmid)
            s.title_verified = bool(v and v["verified"])
            if not s.title_verified:
                dropped.append((s.pmid, "title does not match PubMed's record for that PMID"))
                continue
            try:
                abstract = _abstract_text(s.pmid)
            except Exception as exc:
                abstract = None
                reason = f"could not fetch the abstract: {exc}"
            else:
                reason = "no PubMed record" if abstract is None else "quote text not found in the abstract"
            s.quote_verified = bool(abstract) and pubmed._normalize_for_match(s.quote) in abstract
            if not s.quote_verified:
                dropped.append((s.pmid, reason))
                continue
            # Statistics already taken from the verified full text are not expected in the abstract:
            # re-verifying a saved run must not discard them.
            missing = [] if s.stats_source.startswith("full text") else _numbers_missing(s, abstract)
            if missing:
                for name in missing:
                    setattr(s, name, None)
                s.numbers_note = f"{', '.join(missing)} not stated in the abstract; discarded"
            kept.append(s)
        e.studies = kept
        e.n_studies_dropped = len(dropped)
        e.verification_note = "; ".join(f"PMID {p}: {why}" for p, why in dropped)
        _count(e)
        _pool(e)


def _count(e: ClinicalEvidence) -> None:
    e.n_cest_studies = sum(1 for s in e.studies if s.evidence_type == "cest_application")
    e.n_change_studies = len(e.studies) - e.n_cest_studies
    e.n_human_studies = sum(1 for s in e.studies if _is_human(s))
    e.n_animal_studies = sum(1 for s in e.studies if not _is_human(s) and s.study_setting != "phantom")
    e.n_humans = sum((s.n_disease or 0) + (s.n_control or 0) for s in e.studies if _is_human(s))
    e.n_animals = sum((s.n_disease or 0) + (s.n_control or 0) for s in e.studies if not _is_human(s) and s.study_setting != "phantom")


def _is_human(s) -> bool:
    return "human" in (s.species or "").lower() or "human" in (s.study_setting or "").lower() or "patient" in (s.species or "").lower()


def _pool(e: ClinicalEvidence) -> None:
    rows = []
    for s in e.studies:
        if None in (s.n_disease, s.n_control, s.disease_mean, s.disease_sd, s.control_mean, s.control_sd):
            continue
        sd_d, sd_c = s.disease_sd, s.control_sd
        if s.spread_is_sem:
            sd_d, sd_c = meta_analysis.sem_to_sd(sd_d, s.n_disease), meta_analysis.sem_to_sd(sd_c, s.n_control)
        rows.append({"pmid": s.pmid, "evidence_type": s.evidence_type, "disease_category": s.disease_category,
                     "n_disease": s.n_disease, "n_control": s.n_control,
                     "disease_mean": s.disease_mean, "disease_sd": sd_d, "control_mean": s.control_mean, "control_sd": sd_c})
    from_fulltext = {s.pmid for s in e.studies if s.stats_source.startswith("full text")}
    e.pooled_effects = []
    for p in meta_analysis.pooled_effects(rows):
        k = sum(1 for pmid in p.pmids if pmid in from_fulltext)
        note = (f"statistics of {k} of {p.n_studies} studies read from open-access full text, the rest from abstracts; "
                "a screening-grade estimate" if k else meta_analysis.ABSTRACT_LEVEL_NOTE)
        e.pooled_effects.append(PooledEffect(evidence_type=p.evidence_type, disease_category=p.disease_category, n_studies=p.n_studies,
                                             n_disease=p.n_disease, n_control=p.n_control, hedges_g=p.g, ci_low=p.ci_low, ci_high=p.ci_high,
                                             i2_percent=p.i2_percent, direction=p.direction, pmids=p.pmids, note=note))
