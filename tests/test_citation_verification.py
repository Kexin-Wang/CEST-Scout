"""Unit tests for the deterministic citation-verification step each agent applies to its own
output (see literature_agent._verify_citations / physicist_agent._verify_citations /
experimentalist_agent._verify_citations). These stub out cest_scout.skills.pubmed.verify_titles
so they run offline, unlike test_pubmed_live.py.
"""
from cest_scout.agents import experimentalist_agent, literature_agent, physicist_agent
from cest_scout.schemas import (
    CandidateMetabolite,
    Citation,
    InVivoVerificationPlan,
    LiteratureReport,
    OrganMetabolitePrediction,
    PhysicistReport,
    PhysicistVerdict,
    ReasoningStep,
    ScanProtocol,
    SequenceRecommendation,
)


def _citation(pmid: str, title: str) -> Citation:
    return Citation(pmid=pmid, title=title, species="human", study_setting="human in vivo")


def test_literature_agent_drops_unverified_citation_and_clears_evidence_quote(monkeypatch):
    monkeypatch.setattr(
        literature_agent.pubmed, "verify_titles",
        lambda pairs: {"111": {"verified": True, "real_title": "Real title"},
                       "222": {"verified": False, "real_title": "Unrelated real paper"}},
    )
    report = LiteratureReport(
        body_part="liver",
        metabolites=[
            CandidateMetabolite(
                name="taurine", typical_concentration_mM=2.0, concentration_range_mM="1-3 mM",
                tissue_compartment="cytosol", animal_evidence_only=False,
                citations=[_citation("111", "Real title"), _citation("222", "Fabricated title")],
                concentration_evidence_quote="Taurine was 2.0 mM.", concentration_evidence_pmid="222",
            ),
        ],
        search_summary="test",
    )

    literature_agent._verify_citations(report)

    m = report.metabolites[0]
    assert [c.pmid for c in m.citations] == ["111"]
    assert m.concentration_evidence_quote == ""
    assert m.concentration_evidence_pmid == ""
    assert "PMID 222" in m.notes
    assert "cleared" in m.notes


def test_literature_agent_clears_quote_that_is_not_in_the_cited_abstract(monkeypatch):
    monkeypatch.setattr(
        literature_agent.pubmed, "verify_quote",
        lambda pmid, quote: {"verified": False, "reason": "quote text not found in the cited title/abstract"},
    )
    report = LiteratureReport(
        body_part="liver",
        metabolites=[
            CandidateMetabolite(
                name="taurine", typical_concentration_mM=2.0, concentration_range_mM="1-3 mM",
                tissue_compartment="cytosol", animal_evidence_only=False,
                citations=[_citation("111", "Real title")],
                concentration_evidence_quote="Taurine was 2.0 mM in all subjects.",
                concentration_evidence_pmid="111", concentration_basis="direct_literature_quote",
            ),
        ],
        search_summary="test",
    )

    literature_agent._verify_quotes(report)

    m = report.metabolites[0]
    assert m.concentration_evidence_quote == ""
    assert m.concentration_evidence_pmid == ""
    assert "could not be confirmed" in m.notes
    # The citation itself is a real, title-verified paper -- only the invented quote is dropped.
    assert [c.pmid for c in m.citations] == ["111"]


def test_literature_agent_keeps_quote_that_is_present_in_the_abstract(monkeypatch):
    monkeypatch.setattr(
        literature_agent.pubmed, "verify_quote",
        lambda pmid, quote: {"verified": True, "reason": None},
    )
    report = LiteratureReport(
        body_part="liver",
        metabolites=[
            CandidateMetabolite(
                name="taurine", typical_concentration_mM=2.0, concentration_range_mM="1-3 mM",
                tissue_compartment="cytosol", animal_evidence_only=False,
                citations=[_citation("111", "Real title")],
                concentration_evidence_quote="Taurine was 2.0 mM in all subjects.",
                concentration_evidence_pmid="111", concentration_basis="direct_literature_quote",
            ),
        ],
        search_summary="test",
    )

    literature_agent._verify_quotes(report)

    m = report.metabolites[0]
    assert m.concentration_evidence_quote == "Taurine was 2.0 mM in all subjects."
    assert m.notes == ""


def test_physicist_agent_drops_unverified_citation_and_notes_it(monkeypatch):
    monkeypatch.setattr(
        physicist_agent.pubmed, "verify_titles",
        lambda pairs: {"111": {"verified": True, "real_title": "Real title"},
                       "222": {"verified": False, "real_title": "Unrelated real paper"}},
    )
    report = PhysicistReport(verdicts=[
        PhysicistVerdict(
            metabolite_name="taurine", verified_by_mrs_or_cest_literature=True,
            verifying_citations=[_citation("111", "Real title"), _citation("222", "Fabricated title")],
            simulated_contrast_percent={"3.0": 0.8},
            sequence_recommendations=[
                SequenceRecommendation(field_strength_T=3.0, b1_uT=1.5, saturation_time_ms=1500,
                                        duty_cycle_or_pulses="continuous", rationale="test"),
            ],
            final_confidence="moderate",
        ),
    ])

    physicist_agent._verify_citations(report)

    v = report.verdicts[0]
    assert [c.pmid for c in v.verifying_citations] == ["111"]
    assert "PMID 222" in v.citation_verification_note


def test_experimentalist_agent_drops_unverified_citation_and_notes_it(monkeypatch):
    monkeypatch.setattr(
        experimentalist_agent.pubmed, "verify_titles",
        lambda pairs: {"111": {"verified": True, "real_title": "Real title"},
                       "222": {"verified": False, "real_title": "Unrelated real paper"}},
    )
    plan = InVivoVerificationPlan(
        scan_rationale="test", scan_protocol=ScanProtocol(
            field_strength_T=3.0, pulse_shape="pulsed", b1_uT=1.5, n_pulses=20,
            pulse_duration_ms=80, interpulse_delay_ms=20, duty_cycle_percent=80,
            total_saturation_time_ms=2000, tr_ms=4000, sar_rationale="test", compromise_rationale="test",
        ),
        zspectrum_offsets=[], fat_suppression="test", motion_mitigation="test", slice_coverage="test",
        organs_in_fov=["liver", "paraspinal muscle"],
        organ_predictions=[
            OrganMetabolitePrediction(
                organ="paraspinal muscle", metabolite_name="creatine", organ_concentration_mM=30.0,
                concentration_rationale="test",
                citations=[_citation("111", "Real title"), _citation("222", "Fabricated title")],
            ),
        ],
        differential_analysis_plan="test", caveats="",
    )

    experimentalist_agent._verify_citations(plan)

    pred = plan.organ_predictions[0]
    assert [c.pmid for c in pred.citations] == ["111"]
    assert "PMID 222" in plan.caveats


def _prediction(organ: str, metabolite: str, conc_mM: float) -> OrganMetabolitePrediction:
    return OrganMetabolitePrediction(
        organ=organ, metabolite_name=metabolite, organ_concentration_mM=conc_mM,
        concentration_rationale="test",
    )


def _base_plan(organ_predictions: list) -> InVivoVerificationPlan:
    return InVivoVerificationPlan(
        scan_rationale="test", scan_protocol=ScanProtocol(
            field_strength_T=3.0, pulse_shape="pulsed", b1_uT=1.5, n_pulses=20,
            pulse_duration_ms=80, interpulse_delay_ms=20, duty_cycle_percent=80,
            total_saturation_time_ms=2000, tr_ms=4000, sar_rationale="test", compromise_rationale="test",
        ),
        zspectrum_offsets=[], fat_suppression="test", motion_mitigation="test", slice_coverage="test",
        organs_in_fov=["liver", "paraspinal muscle"], organ_predictions=organ_predictions,
        differential_analysis_plan="test", caveats="",
    )


def test_flags_metabolite_with_identical_concentration_across_organs():
    plan = _base_plan([
        _prediction("liver", "creatine", 8.0),
        _prediction("paraspinal muscle", "creatine", 8.0),
    ])

    experimentalist_agent._flag_undifferentiated_concentrations(plan)

    assert "creatine" in plan.caveats
    assert "Undifferentiated ROI concentrations" in plan.caveats


def test_does_not_flag_metabolite_with_differentiated_concentration():
    plan = _base_plan([
        _prediction("liver", "creatine", 8.0),
        _prediction("paraspinal muscle", "creatine", 30.0),
    ])

    experimentalist_agent._flag_undifferentiated_concentrations(plan)

    assert plan.caveats == ""


def _estimated(steps, reasoning="derived below"):
    return CandidateMetabolite(
        name="taurine", typical_concentration_mM=2.0, concentration_range_mM="1-3 mM",
        tissue_compartment="cytosol", animal_evidence_only=False, citations=[],
        concentration_basis="estimated", concentration_reasoning=reasoning, reasoning_steps=steps,
    )


def test_flags_estimate_with_no_per_step_sources():
    report = LiteratureReport(body_part="liver", metabolites=[_estimated([])], search_summary="t")

    literature_agent._flag_unsourced_reasoning(report)

    assert "no per-step sources" in report.metabolites[0].notes


def test_flags_numeric_step_resting_on_a_bare_assumption():
    steps = [ReasoningStep(statement="liver taurine is about 2 mM", value="2 mM",
                            source_type="assumption", source_detail="")]
    report = LiteratureReport(body_part="liver", metabolites=[_estimated(steps)], search_summary="t")

    literature_agent._flag_unsourced_reasoning(report)

    assert "unsourced assumption" in report.metabolites[0].notes


def test_accepts_fully_sourced_derivation():
    steps = [
        ReasoningStep(statement="whole-blood taurine is 0.06 mM", value="0.06 mM",
                       source_type="reference_database", source_detail="HMDB0000251"),
        ReasoningStep(statement="liver:blood ratio for taurine is ~30x", value="30",
                       source_type="reference_work", source_detail="Geigy Scientific Tables"),
        ReasoningStep(statement="0.06 x 30", value="1.8 mM", source_type="calculation"),
    ]
    report = LiteratureReport(body_part="liver", metabolites=[_estimated(steps)], search_summary="t")

    literature_agent._flag_unsourced_reasoning(report)

    assert report.metabolites[0].notes == ""


# --- literature-agent output eligibility checks -----------------------------

def _metabolite(name, conc_mM=5.0):
    return CandidateMetabolite(
        name=name, typical_concentration_mM=conc_mM, concentration_range_mM="x",
        tissue_compartment="c", animal_evidence_only=False, citations=[],
    )


def test_check_output_rejects_31p_category_labels():
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Glucose"), _metabolite("Phosphomonoesters (PME)"),
        _metabolite("Taurine"), _metabolite("Lactate"), _metabolite("Alanine"),
    ], search_summary="s")

    complaint = literature_agent._check_output(report)

    assert complaint and "Phosphomonoesters (PME)" in complaint
    assert "Glucose" not in complaint.split("\n- ")[1] if "\n- " in complaint else True


def test_check_output_flags_macromolecule_reported_at_whole_molecule_molarity():
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Glucose"), _metabolite("Amide proton transfer (APT) pool", conc_mM=0.02),
        _metabolite("Taurine"), _metabolite("Lactate"), _metabolite("Alanine"),
    ], search_summary="s")

    complaint = literature_agent._check_output(report)

    assert complaint and "Amide proton transfer (APT) pool" in complaint
    assert "200-300 mM" in complaint


def test_check_output_passes_a_clean_report():
    # Distinct concentrations: the duplicate check below keys on (concentration, compartment),
    # so five metabolites sharing this helper's default 5.0 mM/"c" would collide with each other
    # regardless of name -- give each its own value, matching how a real run would differ.
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Glucose", conc_mM=5.0), _metabolite("Glycogen", conc_mM=250.0),
        _metabolite("Taurine", conc_mM=2.0), _metabolite("Lactate", conc_mM=1.5),
        _metabolite("Alanine", conc_mM=3.0),
    ], search_summary="s")

    assert literature_agent._check_output(report) is None


def test_flag_remaining_eligibility_issues_notes_a_surviving_category_label():
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Phosphodiesters (PDE)"),
    ], search_summary="s")

    literature_agent._flag_remaining_eligibility_issues(report)

    assert "[Eligibility]" in report.metabolites[0].notes
    assert "category label" in report.metabolites[0].notes


def test_flag_remaining_eligibility_issues_leaves_clean_metabolites_alone():
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Glycogen", conc_mM=250.0),
    ], search_summary="s")

    literature_agent._flag_remaining_eligibility_issues(report)

    assert report.metabolites[0].notes == ""


def test_check_output_flags_duplicate_metabolite_named_after_the_cest_technique():
    """Regression for a real run: 'Glycogen' and 'GlycoCEST (Glycogen)' were reported as two
    separate candidates at the identical 310.0 mM in the identical compartment -- the second name
    is the CEST technique applied to glycogen, not a distinct molecule.
    """
    dup = _metabolite("GlycoCEST (Glycogen)", conc_mM=310.0)
    dup.tissue_compartment = "intracellular, liver parenchyma"
    original = _metabolite("Glycogen", conc_mM=310.0)
    original.tissue_compartment = "intracellular, liver parenchyma"
    report = LiteratureReport(body_part="liver", metabolites=[
        original, dup, _metabolite("Taurine"), _metabolite("Lactate"), _metabolite("Alanine"),
    ], search_summary="s")

    complaint = literature_agent._check_output(report)

    assert complaint and "Glycogen" in complaint and "GlycoCEST" in complaint


def test_check_output_does_not_flag_coincidentally_similar_but_distinct_metabolites():
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Glucose", conc_mM=5.0), _metabolite("Glycogen", conc_mM=250.0),
        _metabolite("Taurine", conc_mM=2.0), _metabolite("Lactate", conc_mM=1.5),
        _metabolite("Alanine", conc_mM=3.0),
    ], search_summary="s")

    assert literature_agent._check_output(report) is None


def test_flag_remaining_eligibility_issues_notes_a_surviving_duplicate():
    dup = _metabolite("GlycoCEST (Glycogen)", conc_mM=310.0)
    dup.tissue_compartment = "liver"
    original = _metabolite("Glycogen", conc_mM=310.0)
    original.tissue_compartment = "liver"
    report = LiteratureReport(body_part="liver", metabolites=[original, dup], search_summary="s")

    literature_agent._flag_remaining_eligibility_issues(report)

    assert "[Eligibility]" in original.notes and "GlycoCEST" in original.notes
    assert "[Eligibility]" in dup.notes and "Glycogen" in dup.notes


def test_check_output_rejects_water_as_a_candidate():
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Glucose", conc_mM=5.0), _metabolite("water", conc_mM=55.5),
        _metabolite("Taurine", conc_mM=2.0), _metabolite("Lactate", conc_mM=1.5), _metabolite("Alanine", conc_mM=3.0),
    ], search_summary="s")
    complaint = literature_agent._check_output(report)
    assert complaint and "solvent" in complaint
    literature_agent._flag_remaining_eligibility_issues(report)
    assert "[Eligibility]" in report.metabolites[1].notes and "solvent" in report.metabolites[1].notes
    assert report.metabolites[0].notes == ""


def test_identical_round_estimates_for_unrelated_metabolites_are_not_duplicates():
    # Observed: glutamine and alanine both estimated at 5 mM in the same compartment.
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Glutamine (GLN)", conc_mM=5.0), _metabolite("Alanine (Ala)", conc_mM=5.0),
        _metabolite("Glycogen", conc_mM=260.0), _metabolite("Taurine", conc_mM=2.0), _metabolite("Lactate", conc_mM=1.5),
    ], search_summary="s")
    assert literature_agent._find_likely_duplicates(report.metabolites) == []
    assert literature_agent._check_output(report) is None
    assert literature_agent._find_likely_duplicates([
        _metabolite("Glycogen", conc_mM=310.0), _metabolite("GlycoCEST (Glycogen)", conc_mM=310.0),
    ]) == [("Glycogen", "GlycoCEST (Glycogen)")]
    assert literature_agent._find_likely_duplicates([
        _metabolite("Mobile protein amide pool", conc_mM=300.0), _metabolite("APT (mobile protein amides)", conc_mM=300.0),
    ]) != []


def test_check_output_flags_glycogen_reported_at_whole_molecule_molarity():
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Glycogen", conc_mM=5.15), _metabolite("Glutamine", conc_mM=4.4),
        _metabolite("Taurine", conc_mM=2.0), _metabolite("Lactate", conc_mM=1.5), _metabolite("Alanine", conc_mM=0.8),
    ], search_summary="s")
    complaint = literature_agent._check_output(report)
    assert complaint and "Glycogen" in complaint and "glucosyl" in complaint
    report.metabolites[0].typical_concentration_mM = 260.0
    assert literature_agent._check_output(report) is None


def test_check_output_rejects_plural_class_labels():
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Tricarboxylic Acid (TCA) Cycle Metabolites", conc_mM=5.0), _metabolite("Glutamate", conc_mM=10.0),
        _metabolite("Taurine", conc_mM=2.0), _metabolite("Lactate", conc_mM=1.5), _metabolite("Alanine", conc_mM=0.8),
    ], search_summary="s")
    complaint = literature_agent._check_output(report)
    assert complaint and "Tricarboxylic Acid (TCA) Cycle Metabolites" in complaint


def test_check_output_flags_an_amide_proton_pool_at_whole_molecule_molarity():
    # Observed: "Amide Proton Pool" at 8 mM slipped past the hints that only knew "amide proton transfer".
    report = LiteratureReport(body_part="liver", metabolites=[
        _metabolite("Amide Proton Pool", conc_mM=8.0), _metabolite("Glutamate", conc_mM=10.0),
        _metabolite("Taurine", conc_mM=2.0), _metabolite("Lactate", conc_mM=1.5), _metabolite("Alanine", conc_mM=0.8),
    ], search_summary="s")
    complaint = literature_agent._check_output(report)
    assert complaint and "Amide Proton Pool" in complaint
