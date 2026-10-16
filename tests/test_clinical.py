"""Potential clinical impact: verified disease applications per pool, counts, pooled group differences."""
from __future__ import annotations

import math

from cest_scout.agents import clinical_agent
from cest_scout.pipeline import _merge, clinical_markdown
from cest_scout.report.html import _clinical_section
from cest_scout.schemas import (
    CandidateMetabolite,
    ChemistReport,
    ClinicalEvidence,
    ClinicalReport,
    ClinicalStudy,
    LiteratureReport,
    PhysicistReport,
    PhysicistVerdict,
)
from cest_scout.skills import meta_analysis, pubmed


def test_hedges_g_and_random_effects_pooling_match_hand_computation():
    # Two groups of 10, means 3 and 2, SD 1 each: d = 1, J = 1 - 3/(4*18-1) = 0.957746
    g, var = meta_analysis.hedges_g(3.0, 1.0, 10, 2.0, 1.0, 10)
    assert math.isclose(g, 0.957746, rel_tol=1e-5)
    assert math.isclose(var, 20 / 100 + g ** 2 / 40, rel_tol=1e-9)
    assert meta_analysis.hedges_g(3.0, 0.0, 10, 2.0, 1.0, 10) is None       # zero SD: no effect size
    assert meta_analysis.hedges_g(3.0, 1.0, 1, 2.0, 1.0, 10) is None        # n < 2
    # Three identical studies pool to the same g with no heterogeneity.
    e = meta_analysis.StudyEffect("1", 10, 10, g, var)
    pooled = meta_analysis.pool([e, e, e])
    assert pooled is not None and math.isclose(pooled[0], g, rel_tol=1e-9) and pooled[3] == 0.0
    assert pooled[1] < g < pooled[2]
    assert math.isclose(meta_analysis.sem_to_sd(0.5, 16), 2.0)


def test_pooling_needs_three_usable_studies_per_disease_category():
    row = dict(n_disease=12, n_control=12, disease_mean=3.1, disease_sd=0.5, control_mean=2.0, control_sd=0.4)
    studies = [dict(row, pmid=str(i), disease_category="tumor") for i in range(3)] + [dict(row, pmid="9", disease_category="stroke")]
    out = meta_analysis.pooled_effects(studies)
    assert [p.disease_category for p in out] == ["tumor"] and out[0].n_studies == 3 and out[0].n_disease == 36
    # A CEST metric and a metabolite concentration are never pooled together.
    mixed = studies[:2] + [dict(row, pmid="7", disease_category="tumor", evidence_type="metabolite_change")]
    assert meta_analysis.pooled_effects(mixed) == []
    assert out[0].direction == "higher in disease" and out[0].ci_low > 0
    # The same PMID twice counts once.
    assert meta_analysis.pooled_effects(studies[:2] + [dict(row, pmid="0", disease_category="tumor")]) == []


def _study(pmid, **kw):
    base = dict(pmid=pmid, title=f"paper {pmid}", disease="glioma", disease_category="tumor", cest_metric="MTRasym at 3.5 ppm",
                species="human", study_setting="human in vivo", n_disease=20, n_control=10, disease_mean=3.2, disease_sd=0.6,
                control_mean=2.1, control_sd=0.5, direction="higher in disease", p_value_text="p < 0.01",
                quote="APTw was 3.2 ± 0.6 % in 20 high-grade gliomas versus 2.1 ± 0.5 % in 10 controls (p < 0.01).")
    base.update(kw)
    return ClinicalStudy(**base)


def test_verify_drops_bad_pmids_and_quotes_nulls_numbers_not_in_the_quote_and_counts(monkeypatch):
    monkeypatch.setattr(pubmed, "verify_titles", lambda pairs: {p: {"verified": p != "4", "real_title": "x"} for p, _ in pairs})
    # The abstract carries the group sizes in a Methods sentence and the result in a Results sentence;
    # PMID 5's abstract lacks the quoted sentence.
    abstract = pubmed._normalize_for_match("Twenty high-grade gliomas and 10 controls were imaged at 3 T. "
                                           "APTw was 3.2 ± 0.6 % in 20 high-grade gliomas versus 2.1 ± 0.5 % in 10 controls (p < 0.01). "
                                           "APT in 8 tumour rats was 3.2 ± 0.6 % vs 2.1 ± 0.5 % in 8 controls.")
    monkeypatch.setattr(clinical_agent, "_abstract_text", lambda pmid: "a different abstract" if pmid == "5" else abstract)
    report = ClinicalReport(entries=[ClinicalEvidence(metabolite_name="Mobile protein amide pool", pool="amide (APT)", studies=[
        _study("1"), _study("2"), _study("3", species="rat", study_setting="animal in vivo", n_disease=8, n_control=8,
                                       quote="APT in 8 tumour rats was 3.2 ± 0.6 % vs 2.1 ± 0.5 % in 8 controls."),
        _study("4"),                                  # title does not match -> dropped
        _study("5"),                                  # quote not in abstract -> dropped
        _study("6", n_control=99, numbers_note="model text"),   # 99 is nowhere in the abstract -> nulled, study kept
        _study("7", disease="Healthy volunteers"),    # not a disease application -> dropped
        _study("8", title="APT imaging of glioma: a systematic review"),   # not a primary study -> dropped
        _study("9", n_control=99, stats_source="full text (PMC9)"),         # full-text statistics survive re-verification
    ])])
    clinical_agent.verify(report)
    e = report.entries[0]
    assert [s.pmid for s in e.studies] == ["1", "2", "3", "6", "9"] and e.n_studies_dropped == 4
    assert "PMID 4" in e.verification_note and "PMID 5" in e.verification_note and "PMID 7: not a disease" in e.verification_note
    assert "PMID 8: not a primary study" in e.verification_note
    assert e.studies[-1].n_control == 99 and not e.studies[-1].numbers_note
    six = next(s for s in e.studies if s.pmid == "6")
    assert six.n_control is None and "n_control" in six.numbers_note and "model text" not in six.numbers_note and six.disease_mean == 3.2
    assert e.studies[0].n_disease == 20 and not e.studies[0].numbers_note      # sizes found in the Methods sentence
    assert (e.n_human_studies, e.n_animal_studies, e.n_humans, e.n_animals) == (4, 1, 20 + 10 + 20 + 10 + 20 + 20 + 99, 16)
    assert (e.n_cest_studies, e.n_change_studies) == (5, 0)
    # Four human tumour studies with complete statistics pool (1, 2, 9 and 3 is animal...): study 6 lacks n_control.
    assert len(e.pooled_effects) == 1 and e.pooled_effects[0].n_studies == 4 and e.pooled_effects[0].direction == "higher in disease"
    assert "abstracts" in e.pooled_effects[0].note


def test_merge_attaches_evidence_and_both_renderers_show_it():
    lit = LiteratureReport(body_part="brain", search_summary="s", metabolites=[CandidateMetabolite(
        name="Mobile protein amide pool", typical_concentration_mM=70.0, concentration_range_mM="50-100 mM",
        tissue_compartment="intracellular", animal_evidence_only=False, citations=[])])
    chem = ChemistReport(findings=[])
    phys = PhysicistReport(verdicts=[PhysicistVerdict(metabolite_name="Mobile protein amide pool", verified_by_mrs_or_cest_literature=True,
                                                      verifying_citations=[], sequence_recommendations=[],
                                                      simulated_contrast_percent={"3.0": 2.0}, final_confidence="high")])
    ce = ClinicalEvidence(metabolite_name="mobile protein amide pool", pool="amide (APT)", studies=[_study("1", title_verified=True, quote_verified=True)],
                          summary="APT grades gliomas.", n_human_studies=1, n_humans=30)
    final = _merge(lit, chem, phys, {}, None, ClinicalReport(entries=[ce]))
    m = final.metabolites[0]
    assert m.clinical_evidence is not None and m.clinical_evidence.studies[0].pmid == "1"
    md = "\n".join(clinical_markdown(m.clinical_evidence))
    assert "Potential clinical impact" in md and "| CEST/NOE | glioma (tumor) |" in md and "3.2 ± 0.6 vs 2.1 ± 0.5 % (SD)" in md
    assert "not computed" in md
    html = _clinical_section(m.clinical_evidence)
    assert "Potential clinical impact" in html and "PMID 1" in html and "glioma" in html
    bare = ClinicalEvidence(metabolite_name="y", studies=[_study("2", direction="not stated", p_value_text="", note="", cest_metric="")])
    assert "&amp;mdash;" not in _clinical_section(bare) and "<td>&mdash;</td>" in _clinical_section(bare)
    assert clinical_markdown(None) == [] and _clinical_section(None) == ""
    empty = ClinicalEvidence(metabolite_name="x", search_note="Searched 'x CEST'; nothing.")
    assert "no verified disease application" in clinical_markdown(empty)[0]


def test_fulltext_mode_fills_only_missing_verified_numbers_and_then_pools(monkeypatch):
    """Three tumour studies whose abstracts give the means but not the group sizes: the full text
    supplies the sizes (one of them as a table row), a quote not in the text is rejected, and the
    pool is reached with the note saying how many studies came from full text."""
    fulltext = {
        "1": "Methods\nTwenty patients and 10 healthy controls were enrolled.\nResults\nAPTw was higher in tumours.",
        "2": "Table 1 | Group | n | APTw (%)\nGlioma | 18 | 3.1 ± 0.5\nControls | 12 | 2.0 ± 0.4",
        "3": "Methods\nWe studied 15 patients and 15 controls.",
    }
    answers = {
        "1": clinical_agent.FullTextStats(n_disease=20, n_control=10, quote="Twenty patients and 10 healthy controls were enrolled."),
        "2": clinical_agent.FullTextStats(n_disease=18, n_control=12, quote="Glioma | 18 | 3.1 ± 0.5\nControls | 12 | 2.0 ± 0.4"),
        "3": clinical_agent.FullTextStats(n_disease=15, n_control=15, quote="a sentence that is not in the paper"),
    }
    report = ClinicalReport(entries=[ClinicalEvidence(metabolite_name="APT", studies=[
        _study("1", n_disease=None, n_control=None, title_verified=True, quote_verified=True),
        _study("2", n_disease=None, n_control=None, title_verified=True, quote_verified=True),
        _study("3", n_disease=None, n_control=None, title_verified=True, quote_verified=True),
        _study("4", n_disease=None, n_control=None, title_verified=True, quote_verified=True),   # no open-access text
    ])])
    clinical_agent.enrich_from_fulltext(
        report, client=None, model="x",
        fetch=lambda pmid: {"pmcid": f"PMC{pmid}", "text": fulltext[pmid]} if pmid in fulltext else {"pmcid": None, "text": None, "reason": "no PubMed Central full text"},
        ask=lambda study, text: answers[study.pmid],
    )
    e = report.entries[0]
    s1, s2, s3, s4 = e.studies
    assert (s1.n_disease, s1.n_control, s1.stats_source) == (20, 10, "full text (PMC1)") and s1.fulltext_quote.startswith("Twenty")
    assert (s2.n_disease, s2.n_control) == (18, 12) and s2.disease_mean == 3.2        # abstract value kept, sizes filled
    assert s3.n_disease is None and "not in the full text" in s3.fulltext_note
    assert s4.n_disease is None and s4.fulltext_note == "no PubMed Central full text"
    assert e.n_humans == 60
    # Two studies now complete + the third still missing sizes -> below the threshold of 3.
    assert e.pooled_effects == []
    # Give the third its sizes from a quote that IS in the text and the pool is reached.
    answers["3"] = clinical_agent.FullTextStats(n_disease=15, n_control=15, quote="We studied 15 patients and 15 controls.")
    clinical_agent.enrich_from_fulltext(report, client=None, model="x",
                                        fetch=lambda pmid: {"pmcid": f"PMC{pmid}", "text": fulltext.get(pmid)} if pmid in fulltext else {"pmcid": None, "text": None, "reason": "none"},
                                        ask=lambda study, text: answers[study.pmid])
    assert len(e.pooled_effects) == 1 and e.pooled_effects[0].n_studies == 3
    assert "3 of 3 studies read from open-access full text" in e.pooled_effects[0].note


def test_results_excerpt_prefers_methods_and_results():
    body = "\nMethods\nTwenty patients. " + "m" * 1500 + "\nResults\nAPTw rose. " + "r" * 1500
    text = "Abstract\nblah\nIntroduction\n" + "x" * 3000 + body + "\nDiscussion\n" + "y" * 3000 + "\nReferences"
    ex = clinical_agent._results_excerpt(text, limit=40000)
    # Methods and Results come first; the rest of the paper follows only because the limit allows it.
    assert ex.startswith("\nMethods") and "Twenty patients" in ex and "APTw rose" in ex
    assert ex.index("APTw rose") < ex.index("Introduction") < ex.index("yyy")
    assert "yyy" not in clinical_agent._results_excerpt(text, limit=3200)        # tight limit: core only
    assert clinical_agent._results_excerpt("no headings at all " * 10, limit=50) == ("no headings at all " * 10)[:50]
    assert clinical_agent._numbers_in("Twenty-five patients, forty controls, 3.2 ± 0.6 and two hundred scans") >= {25, 40, 3.2, 0.6, 200}


def test_coverage_check_names_candidates_never_searched():
    check = clinical_agent._make_coverage_check([("Phosphocreatine (PCr)", ["PCr"]), ("Glycogen", []), ("Taurine", [])])
    trace = [{"name": "search_pubmed", "args": {"query": "31P MRS phosphocreatine heart failure"}, "result": {}},
             {"name": "search_pubmed", "args": {"query": "glycoCEST liver glycogen"}, "result": {}}]
    msg = check(trace)
    assert msg and "Taurine" in msg and "Glycogen" not in msg and "Phosphocreatine" not in msg
    assert check([]) and "Phosphocreatine (PCr), Glycogen, Taurine" in check([])
    assert check(trace + [{"name": "search_pubmed", "args": {"query": "taurine CEST myocardium"}, "result": {}}]) is None
