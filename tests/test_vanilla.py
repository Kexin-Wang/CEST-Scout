"""The no-tools baseline keeps the model's claims verbatim and scores them deterministically."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

from cest_scout import compare_vanilla, vanilla
from cest_scout.skills import exchange_reference, pubmed

_ANSWER = {"metabolites": [
    {"name": "Phosphocreatine", "typical_concentration_mM": 10, "exchangeable_group": "guanidinium NH", "group_class": "guanidinium",
     "chemical_shift_ppm": 2.6, "exchange_rate_per_s": 150, "detectable_3T": True, "detectable_7T": True,
     "expected_cest_effect_percent_3T": 2.0, "expected_cest_effect_percent_7T": 4.0,
     "citations": [{"pmid": "1", "title": "real"}, {"pmid": "2", "title": "made up"}]},
    {"name": "Taurine", "typical_concentration_mM": "unknown", "exchangeable_group": "amine NH3+", "group_class": "amine",
     "chemical_shift_ppm": 3.0, "exchange_rate_per_s": None, "detectable_3T": False, "detectable_7T": True, "citations": []},
]}


def test_parse_json_accepts_fenced_or_prefixed_answers():
    text = "Here you go:\n```json\n" + json.dumps(_ANSWER) + "\n```"
    assert len(vanilla._parse_json(text)["metabolites"]) == 2
    assert vanilla._parse_json("no json here") == {"metabolites": [], "parse_error": True}


def test_annotate_checks_pmids_and_looks_up_the_reference_without_touching_claims(monkeypatch):
    monkeypatch.setattr(pubmed, "verify_titles", lambda pairs: {"1": {"verified": True, "real_title": "real"},
                                                                 "2": {"verified": False, "real_title": None}})
    ref = exchange_reference.Lookup(group_class="guanidinium", shift_ppm=2.6, kex_per_s=185.0, kex_entry=None, shift_entry=None,
                                    borrowed_from_other_tissue=True, metabolite_specific=True, provenance="p",
                                    kex_low_per_s=80.0, kex_high_per_s=290.0)
    monkeypatch.setattr(exchange_reference, "lookup", lambda cls, *a, **k: ref if cls == "guanidinium" else None)
    answer = json.loads(json.dumps(_ANSWER))
    vanilla._annotate(answer, "myocardium")
    pcr, tau = answer["metabolites"]
    assert [c["verified"] for c in pcr["citations"]] == [True, False]
    assert pcr["chemical_shift_ppm"] == 2.6 and pcr["exchange_rate_per_s"] == 150     # the model's own numbers stay
    assert pcr["reference"] == {"group_class": "guanidinium", "shift_ppm": 2.6, "kex_per_s": 185.0, "kex_low_per_s": 80.0, "kex_high_per_s": 290.0}
    assert tau["reference"]["kex_per_s"] is None and tau["reference"]["group_class"] == "amine"


def _pipeline_run(label, mets):
    """A minimal RunSummary-like object: (name, status, best contrast, concentration)."""
    final = NS(body_part="myocardium", metabolites=[NS(name=n, detectable_by_cest=st == "detectable",
                                                     simulated_contrast_percent={"7.0": c} if st != "unknown" else {}) for n, st, c, _ in mets])
    lit = NS(metabolites=[NS(name=n, aliases=[], typical_concentration_mM=mm) for n, _, _, mm in mets])
    return NS(path=Path(f"{label}.json"), label=label, models=label, final=final, lit=lit, n_candidates=len(mets), elapsed_min=10.0,
              prompt_tokens=1000, completion_tokens=100, n_citations_kept=8, n_citations_dropped=2)


def test_scoring_tables_match_candidates_and_count_agreement(tmp_path, monkeypatch):
    monkeypatch.setattr(compare_vanilla, "_status", lambda m: "detectable" if m.detectable_by_cest else ("unknown" if not m.simulated_contrast_percent else "not detectable"))
    pipeline = [_pipeline_run("deepseek-v4-flash", [("Phosphocreatine (PCr)", "detectable", 1.2, 10.0), ("Taurine", "unknown", 0.0, 13.0),
                                                     ("Glycogen", "not detectable", 0.1, 3.0)]),
                _pipeline_run("deepseek-v4-flash", [("Phosphocreatine", "detectable", 1.3, 10.5), ("Glycogen", "not detectable", 0.2, 5.0)])]
    answer = json.loads(json.dumps(_ANSWER))
    answer["metabolites"][0]["reference"] = {"group_class": "guanidinium", "shift_ppm": 2.6, "kex_per_s": 185.0, "kex_low_per_s": 80.0, "kex_high_per_s": 290.0}
    answer["metabolites"][0]["citations"][0]["verified"] = True
    answer["metabolites"][1]["reference"] = {"group_class": "amine", "shift_ppm": None, "kex_per_s": None}
    answer["metabolites"].append({"name": "Carnosine", "exchangeable_group": "imidazole", "detectable_7T": True, "chemical_shift_ppm": 7.8,
                                  "reference": {"group_class": "imidazole", "shift_ppm": 7.8, "kex_per_s": None}, "citations": []})
    p = tmp_path / "vanilla_myocardium_deepseek_v4_flash_x.json"
    p.write_text(json.dumps({"model": "deepseek-v4-flash", "answer": answer, "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "elapsed_s": 6}))
    tables = compare_vanilla.build_tables(pipeline, [compare_vanilla.load_vanilla(p)])
    titles = [t[0] for t in tables]
    assert titles == ["Runs compared", "Candidates: pipeline consensus vs the vanilla answer", "Agreement per vanilla model"]
    cand = {row[0]: row for row in tables[1][2]}
    pcr = cand["Phosphocreatine"]          # the row takes the most frequent spelling across runs
    assert pcr[1:3] == ["2", "2/2"] and pcr[5] == "1"
    assert pcr[8].startswith("2.6 → 2.6") and pcr[9].startswith("150 → 185 (80-290)")
    assert cand["Carnosine"][1] == "—" and cand["Carnosine"][5] == "1"            # only the vanilla model proposed it
    agree = tables[2][2][0]
    assert agree[0] == "deepseek-v4-flash" and agree[1] == "2/3" and agree[2] == "1/1"   # taurine unknown is left out
    assert agree[3] == "Carnosine" and agree[4] == "none" and agree[7] == "1/2" and agree[8] == "16/20"
    md = compare_vanilla.render_markdown("myocardium", tables, pipeline, [compare_vanilla.load_vanilla(p)])
    assert "| Phosphocreatine |" in md and "<" not in md.split("## Runs")[0]


def test_vanilla_report_renders_claims_beside_references_and_marks_pmids(tmp_path):
    from cest_scout import vanilla_report
    answer = json.loads(json.dumps(_ANSWER))
    answer["metabolites"][0]["reference"] = {"group_class": "guanidinium", "shift_ppm": 2.6, "kex_per_s": 185.0, "kex_low_per_s": 80.0, "kex_high_per_s": 290.0}
    answer["metabolites"][0]["citations"][0]["verified"] = True
    answer["metabolites"][0]["citations"][1]["real_title"] = "An unrelated paper"
    html = vanilla_report.render([{"model": "deepseek-v4-flash", "body_part": "myocardium", "answer": answer, "replicate": 1,
                                   "elapsed_s": 30, "usage": {"prompt_tokens": 300, "completion_tokens": 900}, "code_version": "abc",
                                   "user_prompt": "Q", "raw": "{}"}])
    assert "Vanilla deepseek-v4-flash on myocardium: 1 direct answers" in html
    assert "2.6 &rarr; 2.6" in html and "150 &rarr; 185 (80&ndash;290)" in html
    table = html.split("<p class='note'>")[0]            # the legend below the tables carries its own marks
    assert table.count("&#10003;") == 1 and table.count("&#10007;") == 1 and "An unrelated paper" in html
    assert "PMIDs real / cited: <b>1 / 2</b>" in html
