"""The simulation inputs come from the reference table and the literature agent, never from a
model: group class -> cited k_ex/shift with provenance, or an explicit 'unknown'."""
from __future__ import annotations

from cest_scout.agents import experimentalist_agent
from cest_scout.pipeline import (
    _apply_reference_simulations,
    _merge,
    _simulate_organ_predictions,
    build_simulation_parameters,
    run_reference_simulations,
    simulation_summary,
)
from cest_scout.schemas import (
    CandidateMetabolite,
    ChemistFinding,
    ChemistReport,
    ExchangeableProton,
    InVivoVerificationPlan,
    LiteratureReport,
    OrganMetabolitePrediction,
    PhysicistReport,
    PhysicistVerdict,
    ScanProtocol,
    SequenceRecommendation,
)
from cest_scout.skills import exchange_reference
from cest_scout.skills.exchange_reference import ReferenceEntry, classify_group, lookup

ENTRIES = (
    ReferenceEntry(group_class="guanidinium", metabolite="creatine", aliases=["Cr"], shift_ppm=1.9, shift_range_ppm=None,
                   kex_per_s=950.0, kex_range_per_s=None, tissue="phantom", species="", measurement_type="phantom/solution",
                   conditions="pH 7.0, 37 C", pmid="11111111", title="Creatine phantom", quote="q"),
    ReferenceEntry(group_class="amide", metabolite="", aliases=[], shift_ppm=3.5, shift_range_ppm=None,
                   kex_per_s=28.0, kex_range_per_s=None, tissue="brain", species="rat", measurement_type="animal in vivo",
                   conditions="4.7 T, 37 C", pmid="22222222", title="APT rat brain", quote="q"),
    ReferenceEntry(group_class="hydroxyl", metabolite="glycogen", aliases=[], shift_ppm=1.0, shift_range_ppm=None,
                   kex_per_s=None, kex_range_per_s=None, tissue="liver", species="mouse", measurement_type="animal in vivo",
                   conditions="", pmid="33333333", title="glycoCEST shift only", quote="q"),
)


def test_classify_group_prefers_the_declared_class_and_falls_back_to_text():
    assert classify_group("guanidinium NH2", "") == "guanidinium"
    assert classify_group("weird", "amine") == "amine"
    assert classify_group("backbone amide NH", "not-a-class") == "amide"
    assert classify_group("alpha-amino NH3+", "") == "amine"
    assert classify_group("ring hydroxyl OH", "") == "hydroxyl"
    assert classify_group("something else", "") == "other"


def test_lookup_states_borrowing_and_returns_none_when_no_rate_exists():
    creatine = lookup("guanidinium", "Creatine", ["Cr"], tissue="liver", entries=ENTRIES)
    assert creatine.kex_per_s == 950.0 and creatine.shift_ppm == 1.9
    assert creatine.metabolite_specific and creatine.borrowed_from_other_tissue
    assert "phantom" in creatine.provenance and "PMID 11111111" in creatine.provenance
    assert "no liver-specific" in creatine.provenance

    amide = lookup("amide", "Mobile protein amide pool", [], tissue="brain", entries=ENTRIES)
    assert not amide.borrowed_from_other_tissue and not amide.metabolite_specific
    assert "class-level amide value" in amide.provenance

    # Shift known but no exchange rate for hydroxyls: that is "we don't know", not a guess.
    assert lookup("hydroxyl", "Glycogen", [], tissue="liver", entries=ENTRIES) is None
    assert lookup("thiol", "Glutathione", [], tissue="liver", entries=ENTRIES) is None


def test_shift_is_not_ranked_by_setting_but_kex_is():
    """Two in vivo papers stating the 2 ppm offset they saturated at must not outvote three
    phantom resonances near 1.2 ppm; the in vivo k_ex still beats the phantom one."""
    def glucose(shift, kex, mtype, pmid):
        return ReferenceEntry(group_class="hydroxyl", metabolite="glucose", aliases=[], shift_ppm=shift,
                              shift_range_ppm=None, kex_per_s=kex, kex_range_per_s=None, tissue="brain", species="",
                              measurement_type=mtype, conditions="", pmid=pmid, title="t", quote="q")
    entries = (glucose(2.0, None, "human in vivo", "1"), glucose(2.0, 2000.0, "human in vivo", "2"),
               glucose(1.2, 500.0, "phantom/solution", "3"), glucose(0.9, None, "phantom/solution", "4"),
               glucose(1.2, None, "phantom/solution", "5"))
    ref = lookup("hydroxyl", "Glucose", [], tissue="intervertebral disc", entries=entries)
    assert ref.shift_ppm == 1.2 and ref.kex_per_s == 2000.0
    assert "median of 5 values from every tissue and setting" in ref.provenance


def test_chemist_shift_is_shown_beside_the_simulated_one_only_when_they_differ():
    from cest_scout.pipeline import chemist_shift_note
    from cest_scout.schemas import SimulationParameters
    p = SimulationParameters(metabolite_name="Glucose", group_class="hydroxyl", status="simulated",
                             shift_ppm=1.2, chemist_shift_ppm=1.0)
    assert chemist_shift_note(p) == " (chemist estimate +1.0 ppm, not used)"
    p.chemist_shift_ppm = 1.2
    assert chemist_shift_note(p) == ""


def _lit(name, conc, aliases=()):
    return CandidateMetabolite(name=name, aliases=list(aliases), typical_concentration_mM=conc, concentration_range_mM="x",
                               tissue_compartment="c", animal_evidence_only=False, citations=[])


def _proton(group, cls, n=2, exchangeable=True):
    return ExchangeableProton(group=group, group_class=cls, n_protons=n, ppm_shift_estimate=2.0,
                              exchange_regime="intermediate", is_exchangeable=exchangeable, reasoning="r")


def _reports():
    lit = LiteratureReport(body_part="liver", metabolites=[
        _lit("Creatine", 8.0, ["Cr"]), _lit("Glycogen", 250.0), _lit("Mobile protein amide pool", 250.0),
    ], search_summary="s")
    chem = ChemistReport(findings=[
        ChemistFinding(metabolite_name="Creatine", smiles=None, structure_description="d", overall_argument="a",
                       exchangeable_protons=[_proton("guanidinium NH2", "guanidinium", 2), _proton("amine NH", "amine", 1)]),
        ChemistFinding(metabolite_name="Glycogen", smiles=None, structure_description="d", overall_argument="a",
                       exchangeable_protons=[_proton("ring hydroxyl OH", "hydroxyl", 3)]),
        ChemistFinding(metabolite_name="Mobile protein amide pool", smiles=None, structure_description="d", overall_argument="a",
                       exchangeable_protons=[_proton("backbone amide NH", "amide", 1), _proton("carboxyl", "", 1, exchangeable=False)]),
    ])
    return lit, chem


def test_build_simulation_parameters_records_status_and_provenance_per_group():
    lit, chem = _reports()
    params = build_simulation_parameters(lit, chem, "liver", entries=ENTRIES)
    creatine = {p.group_class: p for p in params["Creatine"]}
    assert creatine["guanidinium"].status == "simulated" and creatine["guanidinium"].kex_per_s == 950.0
    assert creatine["guanidinium"].borrowed_from_other_tissue and creatine["guanidinium"].source_pmids == ["11111111"]
    assert creatine["amine"].status == "no_reference_for_group" and "don't know" in creatine["amine"].provenance
    glycogen = params["Glycogen"][0]
    assert glycogen.status == "no_reference_for_group" and glycogen.kex_per_s is None
    amide = {p.group_class: p for p in params["Mobile protein amide pool"]}
    assert amide["amide"].status == "simulated" and amide["amide"].kex_per_s == 28.0
    assert amide["other"].status == "not_exchangeable"


def test_run_reference_simulations_sweeps_only_reference_backed_groups():
    lit, chem = _reports()
    sims = run_reference_simulations(lit, chem, "liver", entries=ENTRIES)
    cr = sims["Creatine"]
    assert set(cr.contrast_by_field) == {"0.5", "1.5", "3.0", "5.0", "7.0"}
    assert all(r.derived_from == "bloch_mcconnell_sweep" for r in cr.recommendations)
    assert len(cr.zspectra) == 5 and cr.zspectra[0].exchange_rate_per_s == 950.0 and cr.zspectra[0].shift_ppm == 1.9
    assert cr.best_group_by_field["3.0"].group_class == "guanidinium"
    assert "amine NH [amine]" in cr.note and "don't know" in cr.note
    gly = sims["Glycogen"]
    assert gly.contrast_by_field == {} and gly.recommendations == [] and gly.note.startswith("Not simulated")
    summary = {s["name"]: s for s in simulation_summary(sims)}
    assert summary["Creatine"]["swept_saturation_by_field"]["3.0"]["b1_uT"] > 0
    assert summary["Glycogen"]["groups"][0]["status"] == "no_reference_for_group"


def test_apply_reference_simulations_overwrites_numbers_but_keeps_rationale():
    lit, chem = _reports()
    sims = run_reference_simulations(lit, chem, "liver", entries=ENTRIES)
    phys = PhysicistReport(verdicts=[
        PhysicistVerdict(metabolite_name="creatine", verified_by_mrs_or_cest_literature=True, verifying_citations=[],
                         simulated_contrast_percent={"3.0": 99.0}, final_confidence="high",
                         sequence_recommendations=[SequenceRecommendation(field_strength_T=3.0, b1_uT=9.0, saturation_time_ms=1,
                                                                          duty_cycle_or_pulses="x", rationale="physics reasoning")]),
        PhysicistVerdict(metabolite_name="Glycogen", verified_by_mrs_or_cest_literature=True, verifying_citations=[],
                         simulated_contrast_percent={"3.0": 42.0}, final_confidence="high", sequence_recommendations=[]),
    ])
    _apply_reference_simulations(phys, sims)
    cr, gly = phys.verdicts
    assert cr.simulated_contrast_percent == sims["Creatine"].contrast_by_field
    by_field = {r.field_strength_T: r for r in cr.sequence_recommendations}
    assert by_field[3.0].rationale == "physics reasoning" and by_field[3.0].b1_uT != 9.0
    assert by_field[7.0].rationale == ""
    assert gly.simulated_contrast_percent == {} and gly.sequence_recommendations == []
    assert "Not simulated" in gly.simulation_coverage_note

    final = _merge(lit, chem, phys, sims)
    by_name = {m.name: m for m in final.metabolites}
    assert by_name["creatine"].detectable_by_cest == (max(cr.simulated_contrast_percent.values()) >= 0.5)
    assert {p.group_class for p in by_name["creatine"].simulation_parameters} == {"guanidinium", "amine"}
    assert by_name["Glycogen"].detectable_by_cest is False and by_name["Glycogen"].simulation_parameters[0].kex_per_s is None


def test_simulate_organ_predictions_uses_organ_concentration_and_reports_unsimulated_pairs():
    lit, chem = _reports()
    sims = run_reference_simulations(lit, chem, "liver", entries=ENTRIES)
    plan = InVivoVerificationPlan(
        scan_rationale="r", zspectrum_offsets=[], fat_suppression="f", motion_mitigation="m", slice_coverage="s",
        organs_in_fov=["liver"], differential_analysis_plan="d", caveats="",
        scan_protocol=ScanProtocol(field_strength_T=3.0, pulse_shape="cw", b1_uT=1.0, n_pulses=1, pulse_duration_ms=2000,
                                   interpulse_delay_ms=0, duty_cycle_percent=100, total_saturation_time_ms=2000, tr_ms=5000,
                                   sar_rationale="s", compromise_rationale="c"),
        organ_predictions=[
            OrganMetabolitePrediction(organ="paraspinal muscle", metabolite_name="Creatine", organ_concentration_mM=30.0,
                                      concentration_rationale="r", predicted_contrast_percent=77.0),
            OrganMetabolitePrediction(organ="liver", metabolite_name="Creatine", organ_concentration_mM=8.0,
                                      concentration_rationale="r", predicted_contrast_percent=77.0),
            OrganMetabolitePrediction(organ="liver", metabolite_name="Glycogen", organ_concentration_mM=250.0,
                                      concentration_rationale="r", predicted_contrast_percent=77.0),
        ],
    )
    _simulate_organ_predictions(plan, sims, experimentalist_agent.SCAN_FIELD_STRENGTH_T)
    muscle, liver, gly = plan.organ_predictions
    assert muscle.predicted_contrast_percent > liver.predicted_contrast_percent > 0
    assert "k_ex 950" in muscle.simulation_note and "tissue table for 'paraspinal muscle'" in muscle.simulation_note
    assert gly.predicted_contrast_percent == 0.0 and gly.simulation_note.startswith("not simulated")
    assert "[Not simulated]" in plan.caveats and "liver/Glycogen" in plan.caveats


def test_load_entries_skips_unverified_records(tmp_path):
    import json
    p = tmp_path / "ref.json"
    p.write_text(json.dumps({"entries": [
        {"group_class": "amide", "kex_per_s": 30, "shift_ppm": 3.5, "pmid": "1", "verification": {"title_verified": True, "quote_verified": True}},
        {"group_class": "amide", "kex_per_s": 999, "shift_ppm": 3.5, "pmid": "2", "verification": {"title_verified": True, "quote_verified": False}},
    ]}))
    exchange_reference.load_entries.cache_clear()
    entries = exchange_reference.load_entries(str(p))
    assert [e.pmid for e in entries] == ["1"]
    exchange_reference.load_entries.cache_clear()


def test_fulltext_verified_records_load_and_say_so(tmp_path):
    import json
    p = tmp_path / "ref.json"
    p.write_text(json.dumps({"entries": [
        {"group_class": "hydroxyl", "metabolite": "glucose", "kex_per_s": 2900, "shift_ppm": None, "pmid": "3",
         "verification": {"title_verified": True, "quote_verified": False, "fulltext_quote_verified": True, "pmcid": "PMC6771698"}},
        {"group_class": "hydroxyl", "metabolite": "glucose", "kex_per_s": 9999, "shift_ppm": None, "pmid": "4",
         "verification": {"title_verified": True, "quote_verified": False, "fulltext_quote_verified": False, "pmcid": "PMC1"}},
    ]}))
    exchange_reference.load_entries.cache_clear()
    entries = exchange_reference.load_entries(str(p))
    exchange_reference.load_entries.cache_clear()
    assert [e.pmid for e in entries] == ["3"]
    assert "from the full text (PMC6771698), not the abstract" in entries[0].describe()


def test_phosphocreatine_does_not_inherit_creatine_entries():
    def guan(metabolite, shift, aliases=()):
        return ReferenceEntry(group_class="guanidinium", metabolite=metabolite, aliases=list(aliases), shift_ppm=shift,
                              shift_range_ppm=None, kex_per_s=100.0, kex_range_per_s=None, tissue="muscle", species="",
                              measurement_type="human in vivo", conditions="", pmid=metabolite, title="t", quote="q")
    entries = (guan("creatine", 2.0), guan("phosphocreatine", 2.6, ["creatine phosphate"]))
    assert lookup("guanidinium", "Phosphocreatine (PCr)", [], entries=entries).shift_ppm == 2.6
    assert lookup("guanidinium", "Creatine phosphate", [], entries=entries).shift_ppm == 2.6
    assert lookup("guanidinium", "Creatine", [], entries=entries).shift_ppm == 2.0
    assert lookup("guanidinium", "Creatine + phosphocreatine", [], entries=entries).shift_ppm == 2.3


def test_aliases_never_widen_a_name_that_matches_the_table():
    def hyd(metabolite, shift, kex):
        return ReferenceEntry(group_class="hydroxyl", metabolite=metabolite, aliases=[], shift_ppm=shift,
                              shift_range_ppm=None, kex_per_s=kex, kex_range_per_s=None, tissue="phantom", species="",
                              measurement_type="phantom/solution", conditions="", pmid=metabolite, title="t", quote="q")
    entries = (hyd("glycogen", 1.0, 31850.0), hyd("glucose", 1.28, 6500.0))
    # Observed: Qwen3.8 aliased glycogen "polyglucose" and glucose's entries were pooled in.
    gly = lookup("hydroxyl", "Glycogen", ["glycoCEST", "polyglucose"], entries=entries)
    assert gly.shift_ppm == 1.0 and gly.kex_per_s == 31850.0 and gly.metabolite_specific
    assert lookup("hydroxyl", "Glycogen (as glucose polymer)", [], entries=entries).kex_per_s == 31850.0
    # A name too short to match still reaches its alias.
    pcr = (ReferenceEntry(group_class="guanidinium", metabolite="phosphocreatine", aliases=[], shift_ppm=2.6,
                          shift_range_ppm=None, kex_per_s=185.0, kex_range_per_s=None, tissue="muscle", species="",
                          measurement_type="human in vivo", conditions="", pmid="p", title="t", quote="q"),)
    assert lookup("guanidinium", "PCr", ["phosphocreatine"], entries=pcr).metabolite_specific


def test_builder_keeps_pools_apart_and_analogues_out_of_glucose():
    from cest_scout.skills.build_exchange_reference import _metabolite_of, parse_number
    assert parse_number("2.6 (PCr); 1.9 (Cr)", ["guanidinium", "phosphocreatine", "pcr"])[0] == 2.6
    assert parse_number("3 (fast amines); 2 (intermediate amines)", ["amine"])[0] is None
    # Labels naming a tissue or method, not a pool, still pool the one pool's values.
    assert parse_number("101 ± 25 (gray matter); 131 ± 27 (white matter)", ["amide"])[0] == 116.0
    assert _metabolite_of("3-O-methyl-D-glucose (3OMG) hydroxyls (glucose analogue)") == "3-o-methyl-glucose"
    assert _metabolite_of("dextran (~1 kDa) glucose-polymer hydroxyls") == "dextran"
    assert _metabolite_of("D-glucose C6-OH (pool B, 0.66 ppm)") == "glucose"


def test_resimulate_recomputes_only_the_deterministic_stage(tmp_path, monkeypatch):
    import json
    from cest_scout import resimulate as rs
    lit, chem = _reports()
    phys = PhysicistReport(verdicts=[
        PhysicistVerdict(metabolite_name="Creatine", verified_by_mrs_or_cest_literature=True, verifying_citations=[],
                         simulated_contrast_percent={"3.0": 99.0}, final_confidence="high", critique_of_chemist="keep me",
                         sequence_recommendations=[]),
    ])
    src = tmp_path / "liver_x_20260101T000000Z.json"
    src.write_text(json.dumps({"literature": lit.model_dump(), "chemist": chem.model_dump(), "physicist": phys.model_dump(),
                               "experimentalist": None,
                               "final": {"body_part": "liver", "metabolites": [], "provenance": {
                                   "started_at_utc": "t", "finished_at_utc": "t", "elapsed_s": 1.0, "stages": []}}}))
    monkeypatch.setattr(exchange_reference, "load_entries", lambda *a, **k: ENTRIES)
    out = rs.resimulate(src, tmp_path)
    assert out.name == "liver_x_resim_20260101T000000Z"
    data = json.loads(out.with_suffix(".json").read_text())
    final = data["final"]
    assert final["provenance"]["resimulated_at_utc"]
    cr = final["metabolites"][0]
    assert cr["simulated_contrast_percent"] != {"3.0": 99.0} and "3.0" in cr["simulated_contrast_percent"]
    assert "keep me" in cr["caveats"]
    assert out.with_suffix(".html").exists() and out.with_suffix(".md").exists()


def test_lookup_matches_metabolite_names_with_suffixes_and_composites():
    plain = lookup("guanidinium", "Creatine", [], tissue="liver", entries=ENTRIES)
    composite = lookup("guanidinium", "Creatine (total: creatine + phosphocreatine)", [], tissue="liver", entries=ENTRIES)
    assert composite.metabolite_specific and composite.kex_per_s == plain.kex_per_s == 950.0
