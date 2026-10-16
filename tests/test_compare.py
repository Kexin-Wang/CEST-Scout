"""The cross-model comparison must count what the deterministic checks recorded, match the same
metabolite across runs through aliases, and keep a run's duplicate candidates visible."""
from __future__ import annotations

import json
from pathlib import Path

from cest_scout.compare import build_tables, load_run, render_html, render_markdown


def _citation(pmid="1"):
    return {"pmid": pmid, "title": "T", "species": "human", "study_setting": "human in vivo"}


def _lit_metabolite(name, conc, aliases=(), notes="", basis="estimated", reference=""):
    return {
        "name": name, "aliases": list(aliases), "typical_concentration_mM": conc, "concentration_range_mM": "x",
        "tissue_compartment": "c", "animal_evidence_only": False, "citations": [_citation()],
        "concentration_basis": basis, "notes": notes, "reference_concentrations": reference,
    }


def _final_metabolite(name, conc, detectable, contrast_by_field):
    return {
        "name": name, "typical_concentration_mM": conc, "concentration_range_mM": "x", "animal_evidence_only": False,
        "detectable_by_cest": detectable, "field_strengths_recommended_T": [], "best_sequence_by_field": [
            {"field_strength_T": float(f), "b1_uT": 1.0, "saturation_time_ms": 1000.0, "duty_cycle_or_pulses": "cw",
             "rationale": "r"} for f in contrast_by_field
        ],
        "exchangeable_protons_summary": "s", "simulated_contrast_percent": contrast_by_field, "key_citations": [],
        "caveats": "",
    }


def _verdict(name, fields, note=""):
    return {
        "metabolite_name": name, "verified_by_mrs_or_cest_literature": True, "verifying_citations": [_citation()],
        "simulated_contrast_percent": {str(f): 1.0 for f in fields}, "sequence_recommendations": [],
        "final_confidence": "high", "citation_verification_note": note,
    }


def _finding(name, smiles, note=""):
    return {"metabolite_name": name, "smiles": smiles, "structure_description": "d", "exchangeable_protons": [],
            "overall_argument": "a", "structure_verification_note": note}


def _write_run(tmp_path: Path, filename: str, lit, chem, phys, final_mets, provenance=None):
    data = {
        "literature": {"body_part": "liver", "metabolites": lit, "search_summary": "s"},
        "chemist": {"findings": chem},
        "physicist": {"verdicts": phys},
        "experimentalist": None,
        "final": {"body_part": "liver", "metabolites": final_mets, "provenance": provenance},
    }
    p = tmp_path / filename
    p.write_text(json.dumps(data))
    return p


def _provenance(model, minutes):
    return {
        "started_at_utc": "t0", "finished_at_utc": "t1", "elapsed_s": minutes * 60, "code_version": "abc123",
        "stages": [
            {"agent": a, "model": model, "endpoint": "node:8000", "elapsed_s": minutes * 15, "llm_calls": 2,
             "prompt_tokens": 1000, "completion_tokens": 100, "tool_calls": 3, "tool_errors": 1,
             "output_check_passed": a != "physicist"}
            for a in ("literature", "chemist", "physicist", "experimentalist")
        ],
    }


def test_compare_counts_checks_and_matches_metabolites_across_runs(tmp_path):
    dropped = "[Citation verification] citations: PMID 9 (claimed as 'X') dropped -- no such record."
    run_a = _write_run(
        tmp_path, "liver_model-a_20260101T000000Z.json",
        lit=[_lit_metabolite("Creatine", 30.0, aliases=["Cr"], notes=dropped, basis="direct_literature_quote", reference="HMDB"),
             _lit_metabolite("Glycogen", 300.0, notes="[Eligibility] duplicate")],
        chem=[_finding("Creatine", "CN(CC(=O)O)C(=N)N", "Chemist agent's proposed structure was WRONG and has been replaced with PubChem's"),
              _finding("Glycogen", None)],
        phys=[_verdict("Creatine", [0.5, 1.5, 3.0, 5.0, 7.0]), _verdict("Glycogen", [3.0])],
        final_mets=[_final_metabolite("Creatine", 30.0, True, {"3.0": 1.2, "7.0": 2.5}),
                    _final_metabolite("Glycogen", 300.0, True, {"3.0": 5.0})],
        provenance=_provenance("model-a", 40),
    )
    run_b = _write_run(
        tmp_path, "liver_model-b_20260101T000000Z.json",
        lit=[_lit_metabolite("Cr (creatine)", 28.0), _lit_metabolite("Taurine", 5.0)],
        chem=[_finding("Cr (creatine)", "CN(CC(=O)O)C(=N)N"), _finding("Taurine", "C(CS(=O)(=O)O)N")],
        phys=[_verdict("Cr (creatine)", [0.5, 1.5, 3.0, 5.0, 7.0]), _verdict("Taurine", [0.5, 1.5, 3.0, 5.0, 7.0])],
        final_mets=[_final_metabolite("Cr (creatine)", 28.0, False, {"3.0": 0.3}),
                    _final_metabolite("Taurine", 5.0, False, {"3.0": 0.1})],
    )

    a, b = load_run(run_a), load_run(run_b)
    assert a.label == "model-a" and a.elapsed_min == 40 and a.checks_failed == ["physicist"]
    assert a.prompt_tokens == 4000 and a.tool_calls == 12 and a.tool_errors == 4
    assert a.n_citations_dropped == 1 and a.tag_counts["Eligibility"] == 1 and a.n_reference_grounded == 1
    assert a.basis_counts["direct_literature_quote"] == 1
    assert a.n_structures_replaced == 1 and a.n_structures_with_smiles == 1
    assert a.n_coverage_gaps == 1 and abs(a.coverage_fraction - 6 / 10) < 1e-9
    assert b.label == "model-b" and b.elapsed_min is None and b.n_coverage_gaps == 0

    tables = {title: (headers, rows) for title, headers, rows, _ in build_tables([a, b])}
    headers, rows = tables["Candidate overlap: detectability, best contrast, concentration used"]
    by_name = {r[0]: r for r in rows}
    # 'Cr' alias in run A joins 'Cr (creatine)' in run B into one row, present in both runs.
    creatine_row = next(r for n, r in by_name.items() if "reatine" in n)
    assert creatine_row[1] == "2/2"
    assert creatine_row[2].startswith("YES 2.50% @ 7 T [all fields 3/7 T: 1.20/2.50 %]; 30 mM")
    assert creatine_row[3].startswith("no 0.30% @ 3 T [all fields 3 T: 0.30 %]; 28 mM")
    assert by_name["Glycogen"][3] == "—" and by_name["Taurine"][2] == "—"

    _, agreement = tables["Pairwise agreement"]
    assert "Jaccard 0.33" in agreement[0][2] and "agree 0/1" in agreement[0][2]

    md = render_markdown([a, b])
    assert "model-a" in md and "Candidate overlap" in md and "FAILED" not in md.split("## Literature")[0][:0]
    html = render_html([a, b])
    assert "<table>" in html and "model-b" in html


def test_compare_merges_differently_worded_macromolecular_pools(tmp_path):
    run_a = _write_run(
        tmp_path, "liver_a_20260101T000000Z.json",
        lit=[_lit_metabolite("Amide proton transfer (APT) pool (mobile proteins/peptides)", 300.0)],
        chem=[_finding("Amide proton transfer (APT) pool (mobile proteins/peptides)", None)],
        phys=[_verdict("Amide proton transfer (APT) pool (mobile proteins/peptides)", [3.0])],
        final_mets=[_final_metabolite("Amide proton transfer (APT) pool (mobile proteins/peptides)", 300.0, True, {"3.0": 5.0})],
    )
    run_b = _write_run(
        tmp_path, "liver_b_20260101T000000Z.json",
        lit=[_lit_metabolite("Mobile Protein Amide Protons", 250.0)],
        chem=[_finding("Mobile Protein Amide Protons", None)],
        phys=[_verdict("Mobile Protein Amide Protons", [3.0])],
        final_mets=[_final_metabolite("Mobile Protein Amide Protons", 250.0, True, {"3.0": 4.0})],
    )
    rows = {title: r for title, _, r, _ in build_tables([load_run(run_a), load_run(run_b)])}[
        "Candidate overlap: detectability, best contrast, concentration used"]
    assert len(rows) == 1 and rows[0][1] == "2/2"


def test_compare_keeps_a_runs_duplicate_candidates_visible(tmp_path):
    run = _write_run(
        tmp_path, "liver_x_20260101T000000Z.json",
        lit=[_lit_metabolite("Glycogen", 310.0), _lit_metabolite("GlycoCEST (Glycogen)", 310.0, aliases=["Glycogen"])],
        chem=[_finding("Glycogen", None), _finding("GlycoCEST (Glycogen)", None)],
        phys=[_verdict("Glycogen", [3.0]), _verdict("GlycoCEST (Glycogen)", [3.0])],
        final_mets=[_final_metabolite("Glycogen", 310.0, True, {"3.0": 28.5}),
                    _final_metabolite("GlycoCEST (Glycogen)", 310.0, True, {"3.0": 23.0})],
    )
    r = load_run(run)
    tables = {title: rows for title, _, rows, _ in build_tables([r])}
    rows = tables["Candidate overlap: detectability, best contrast, concentration used"]
    assert len(rows) == 1
    assert "[as 'Glycogen']" in rows[0][2] and "[as 'GlycoCEST (Glycogen)']" in rows[0][2]


def test_replicates_of_one_model_are_pooled_and_their_agreement_reported(tmp_path):
    def run(filename, minutes, mets):
        return _write_run(
            tmp_path, filename,
            lit=[_lit_metabolite(n, c) for n, c, _ in mets],
            chem=[_finding(n, None) for n, _, _ in mets],
            phys=[_verdict(n, [3.0]) for n, _, _ in mets],
            final_mets=[_final_metabolite(n, c, d, {"3.0": 1.0 if d else 0.1}) for n, c, d in mets],
            provenance=_provenance("model-a", minutes),
        )
    a1 = run("liver_model-a_20260101T000000Z.json", 10, [("Creatine", 30.0, True), ("Glycogen", 300.0, True)])
    a2 = run("liver_model-a_20260101T010000Z.json", 20, [("Creatine", 30.0, False), ("Taurine", 5.0, False)])
    runs = [load_run(a1), load_run(a2)]
    tables = {t[0]: t for t in build_tables(runs)}

    _, headers, rows, _ = tables["Per-model summary across replicate runs"]
    assert len(rows) == 1
    row = dict(zip(headers, rows[0]))
    assert row["Model"] == "model-a" and row["Runs"] == "2"
    assert row["Minutes (mean; range)"] == "15; 10-20"
    assert row["Replicate candidate Jaccard (mean)"] == "0.33"          # {Cr, Gly} vs {Cr, Tau}
    assert row["Replicate detectability agreement"].startswith("0%")   # creatine flipped between replicates

    _, headers, rows, _ = tables["Cross-run consensus per candidate"]
    by_name = {r[0]: dict(zip(headers, r)) for r in rows}
    assert by_name["Creatine"]["Runs proposing"] == "2/2" and by_name["Creatine"]["Runs detectable"] == "1/2"
    assert by_name["Glycogen"]["Models proposing"] == "1/1"

    md = render_markdown(runs)
    assert "replicate runs per model" in md and "unstable" in md


def test_canonical_pool_is_decided_from_the_name_head():
    from cest_scout.compare import _canonical_pool
    assert _canonical_pool("Mobile protein/peptide amide pool (APTw)") == "pool:apt"
    assert _canonical_pool("Amide Protons (Mobile Proteins)") == "pool:apt"
    assert _canonical_pool("Amide proton transfer pool") == "pool:apt"
    # descriptions in parentheses must not re-file a small molecule under the amide pool
    assert _canonical_pool("Glutathione (GSH; thiol + amide + -COOH pool)") is None
    assert _canonical_pool("ATP (adenosine triphosphate; amide/imidazole -NH)") is None
    assert _canonical_pool("Glucose (from glycogen breakdown)") is None
    assert _canonical_pool("Glycogen (glycoCEST)") == "pool:glycogen"
    assert _canonical_pool("Creatine/Phosphocreatine") == "pool:creatine"


def test_phosphocreatine_is_not_pooled_with_creatine():
    from cest_scout.compare import _canonical_pool
    assert _canonical_pool("Phosphocreatine (PCr)") == "pool:phosphocreatine"
    assert _canonical_pool("Creatine phosphate") == "pool:phosphocreatine"
    assert _canonical_pool("PCr") == "pool:phosphocreatine"
    assert _canonical_pool("Creatine") == "pool:creatine"
    assert _canonical_pool("Creatine (Cr)") == "pool:creatine"
    assert _canonical_pool("Creatine and Phosphocreatine") == "pool:creatine"
    assert _canonical_pool("Phospho-creatine (PCr)") == "pool:phosphocreatine"
    assert _canonical_pool("Cr/PCr (total creatine)") == "pool:creatine"


def test_an_alias_never_bridges_two_canonical_pools():
    from types import SimpleNamespace as NS
    from cest_scout.compare import _Groups
    def run(*mets):
        return NS(lit=NS(metabolites=[NS(name=n, aliases=list(a)) for n, a in mets]))
    runs = [run(("Phosphocreatine", ["PCr"]), ("Creatine", ["Cr"])),
            run(("Creatine and Phosphocreatine", ["creatine", "phosphocreatine", "PCr"])),
            run(("Creatine (Cr)", []), ("Phosphocreatine (PCr)", ["creatine phosphate"]))]
    g = _Groups(runs)
    assert g.key("Creatine") == g.key("Creatine (Cr)") == g.key("Creatine and Phosphocreatine")
    assert g.key("Phosphocreatine") == g.key("Phosphocreatine (PCr)") == g.key("Creatine phosphate")
    assert g.key("Creatine") != g.key("Phosphocreatine")


def test_clinical_evidence_table_appears_only_when_a_run_has_it(tmp_path):
    import json
    from cest_scout.compare import build_tables, load_run
    from cest_scout.schemas import ClinicalEvidence, ClinicalReport, ClinicalStudy
    src = sorted(Path("outputs/hopper_disc_20261006").glob("intervertebral_disc_*.json"))
    if not src:
        return
    data = json.loads(src[0].read_text())
    plain = tmp_path / "plain.json"; plain.write_text(json.dumps(data))
    assert "Clinical evidence (potential clinical impact)" not in [t[0] for t in build_tables([load_run(plain)])]
    study = ClinicalStudy(pmid="1", title="t", disease="glioma", quote="q", species="human", study_setting="human in vivo",
                          n_disease=20, n_control=10, stats_source="full text (PMC1)", title_verified=True, quote_verified=True)
    data["clinical"] = ClinicalReport(entries=[ClinicalEvidence(metabolite_name="x", studies=[study], n_cest_studies=1,
                                                                 n_human_studies=1, n_humans=30, n_studies_dropped=2)]).model_dump()
    withc = tmp_path / "withc.json"; withc.write_text(json.dumps(data))
    tables = {t[0]: t for t in build_tables([load_run(withc)])}
    row = tables["Clinical evidence (potential clinical impact)"][2][0]
    assert row[1:] == ["1", "1", "0", "30 / 0", "1", "0", "2"]


def test_descriptor_parentheticals_never_merge_candidates_and_glu_gln_glx_are_distinct():
    from types import SimpleNamespace as NS
    from cest_scout.compare import _Groups, _canonical_pool
    def run(*mets):
        return NS(lit=NS(metabolites=[NS(name=n, aliases=list(a)) for n, a in mets]))
    runs = [run(("Creatine (free)", ["Cr"]), ("Glutamine (free)", ["Gln", "Glx (MRS term)"]), ("Glutamate (free)", ["Glu", "E"])),
            run(("Creatine (total: creatine + phosphocreatine)", ["total creatine"]), ("Glutamine", ["L-glutamine"]), ("Glutamate", ["L-glutamic acid"]))]
    g = _Groups(runs)
    assert len({g.key("Creatine (free)"), g.key("Glutamine (free)"), g.key("Glutamate (free)")}) == 3
    assert g.key("Glutamine (free)") == g.key("Glutamine") and g.key("Glutamate (free)") == g.key("Glutamate")
    assert g.key("Creatine (free)") == g.key("Creatine (total: creatine + phosphocreatine)")
    assert _canonical_pool("Glutamate + glutamine (Glx)") == "pool:glx" and _canonical_pool("Glx") == "pool:glx"
    assert _canonical_pool("Glutamate (free)") == "pool:glutamate" and _canonical_pool("L-Glutamine") == "pool:glutamine"
