"""rNOE pools are simulated through the same reference gate as exchange pools, an unsimulated
metabolite is 'unknown' rather than 'not detectable', tissue parameters come out as table rows,
long prose is re-flowed into short bullets, and macromolecules get their repeating units."""
from __future__ import annotations

from cest_scout.pipeline import (
    _merge, build_simulation_parameters, detectability_of, run_reference_simulations, tissue_parameter_rows, verdict_label,
)
from cest_scout.report.prose import bullets, is_long, segments
from cest_scout.report.structures import polymer_units, render_polymer_units_png_base64, render_structure_png_base64
from cest_scout.schemas import (
    CandidateMetabolite, ChemistFinding, ChemistReport, ExchangeableProton, FinalMetabolite, LiteratureReport,
    PhysicistReport, PhysicistVerdict,
)
from cest_scout.skills.exchange_reference import ReferenceEntry, classify_group, lookup, mechanism_of

ENTRIES = (
    ReferenceEntry(group_class="rnoe", metabolite="", aliases=[], shift_ppm=-3.5, shift_range_ppm=None, kex_per_s=None,
                   kex_range_per_s=None, tissue="brain", species="rat", measurement_type="animal in vivo", conditions="",
                   pmid="11111111", title="NOE shift", quote="q"),
    ReferenceEntry(group_class="rnoe", metabolite="", aliases=[], shift_ppm=None, shift_range_ppm=None, kex_per_s=5.0,
                   kex_range_per_s=None, tissue="phantom", species="", measurement_type="phantom/solution", conditions="",
                   pmid="22222222", title="NOE rate", quote="q"),
    ReferenceEntry(group_class="rnoe", metabolite="glycogen", aliases=[], shift_ppm=-1.0, shift_range_ppm=None, kex_per_s=None,
                   kex_range_per_s=None, tissue="muscle", species="mouse", measurement_type="animal in vivo", conditions="",
                   pmid="33333333", title="glycoNOE", quote="q"),
    ReferenceEntry(group_class="rnoe16", metabolite="", aliases=[], shift_ppm=-1.6, shift_range_ppm=None, kex_per_s=None,
                   kex_range_per_s=None, tissue="brain", species="rat", measurement_type="animal in vivo", conditions="",
                   pmid="44444444", title="NOE 1.6", quote="q"),
)


def _citation():
    return {"pmid": "1", "title": "T", "species": "human", "study_setting": "human in vivo"}


def _lit(name, conc):
    return CandidateMetabolite(name=name, typical_concentration_mM=conc, concentration_range_mM="", tissue_compartment="c",
                               animal_evidence_only=False, citations=[])


def _ep(group, cls, ppm, exch=True, n=1):
    return ExchangeableProton(group=group, group_class=cls, n_protons=n, ppm_shift_estimate=ppm, exchange_regime="x",
                              is_exchangeable=exch, reasoning="r")


def test_rnoe_classes_and_lookup():
    assert classify_group("aliphatic C-H (rNOE)", "") == "rnoe"
    assert classify_group("NOE at -1.6 ppm", "") == "rnoe16"
    assert classify_group("rNOE -1.6 ppm membrane lipid", "") == "rnoe16"
    assert classify_group("weird", "rnoe") == "rnoe"
    assert mechanism_of("rnoe") == "rNOE" and mechanism_of("amide") == "chemical exchange"
    gly = lookup("rnoe", "Glycogen", [], tissue="liver", entries=ENTRIES)
    assert gly.shift_ppm == -1.0 and gly.kex_per_s == 5.0 and gly.borrowed_from_other_tissue
    assert "effective rNOE transfer rate" in gly.provenance
    generic = lookup("rnoe", "Glycosaminoglycan", [], tissue="disc", entries=ENTRIES)
    assert generic.shift_ppm == -3.5 and not generic.metabolite_specific
    assert lookup("rnoe16", "lipid", [], tissue="brain", entries=ENTRIES) is None   # offset but no rate


def test_rnoe_group_is_simulated_and_counts_toward_detectability():
    lit = LiteratureReport(body_part="liver", metabolites=[_lit("Glycogen", 300.0), _lit("Lipid", 50.0)], search_summary="")
    chem = ChemistReport(findings=[
        ChemistFinding(metabolite_name="Glycogen", structure_description="d", overall_argument="a",
                       exchangeable_protons=[_ep("ring C-H (rNOE)", "rnoe", -1.0, exch=False, n=7)]),
        ChemistFinding(metabolite_name="Lipid", structure_description="d", overall_argument="a",
                       exchangeable_protons=[_ep("rNOE at -1.6 ppm", "rnoe16", -1.6, exch=False, n=2)]),
    ])
    params = build_simulation_parameters(lit, chem, "liver", entries=ENTRIES)
    gly = params["Glycogen"][0]
    assert gly.status == "simulated" and gly.mechanism == "rNOE" and gly.shift_ppm == -1.0 and gly.kex_per_s == 5.0
    lip = params["Lipid"][0]
    assert lip.status == "no_reference_for_group" and "rNOE" in lip.provenance

    sims = run_reference_simulations(lit, chem, "liver", entries=ENTRIES)
    assert sims["Glycogen"].simulated and sims["Glycogen"].contrast_by_field
    assert all(z.shift_ppm < 0 for z in sims["Glycogen"].zspectra)
    assert not sims["Lipid"].simulated

    phys = PhysicistReport(verdicts=[
        PhysicistVerdict(metabolite_name="Glycogen", verified_by_mrs_or_cest_literature=False, verifying_citations=[],
                         simulated_contrast_percent={}, sequence_recommendations=[], final_confidence="low"),
        PhysicistVerdict(metabolite_name="Lipid", verified_by_mrs_or_cest_literature=False, verifying_citations=[],
                         simulated_contrast_percent={}, sequence_recommendations=[], final_confidence="low"),
    ])
    from cest_scout.pipeline import _apply_reference_simulations
    _apply_reference_simulations(phys, sims)
    final = _merge(lit, chem, phys, sims)
    by = {m.name: m for m in final.metabolites}
    assert by["Lipid"].detectability == "unknown" and not by["Lipid"].detectable_by_cest
    assert by["Lipid"].detectability_robustness == "unknown"
    assert verdict_label(by["Lipid"]) == "unknown (not simulated)"
    assert by["Glycogen"].detectability in ("detectable", "not detectable")
    if by["Glycogen"].detectable_by_cest:
        assert by["Glycogen"].detection_mechanism == "rNOE" and verdict_label(by["Glycogen"]) == "detectable (rNOE)"


def test_old_runs_without_the_field_derive_unknown_from_missing_contrast():
    base = dict(typical_concentration_mM=1.0, concentration_range_mM="", animal_evidence_only=False,
                field_strengths_recommended_T=[], best_sequence_by_field=[], exchangeable_protons_summary="",
                key_citations=[], caveats="")
    assert detectability_of(FinalMetabolite(name="a", detectable_by_cest=False, simulated_contrast_percent={}, **base)) == "unknown"
    assert detectability_of(FinalMetabolite(name="b", detectable_by_cest=False, simulated_contrast_percent={"3.0": 0.1}, **base)) == "not detectable"
    assert detectability_of(FinalMetabolite(name="c", detectable_by_cest=True, simulated_contrast_percent={"3.0": 1.0}, **base)) == "detectable"


def test_tissue_parameter_rows_cover_target_tissue_at_every_field():
    rows = tissue_parameter_rows("liver")
    fields = {r.field_T for r in rows}
    assert fields == {0.5, 1.5, 3.0, 5.0, 7.0}
    params = {r.parameter for r in rows}
    assert {"water T1", "water T2", "MT fraction", "MT k_ex", "MT T2b"} <= params
    assert all(r.verified in ("abstract", "table", "default", "estimated") for r in rows)
    assert any(r.verified == "estimated" for r in rows if r.field_T in (0.5, 7.0))   # scaled, not clamped
    unknown = tissue_parameter_rows("no such tissue xyz")
    assert all(r.verified == "default" for r in unknown)


def test_prose_is_reflowed_into_short_bullets():
    text = " ".join(f"Sentence number {i} is here." for i in range(80))
    assert is_long(text)
    items = bullets(text)
    assert len(items) > 3 and all(len(b.split()) <= 60 for b in items)
    assert " ".join(items) == text            # nothing dropped
    assert segments("First one. Second one | [Tag] Third one.") == ["First one.", "Second one", "[Tag] Third one."]
    assert bullets("First one. Second one | [Tag] Third one.", max_words=2) == ["First one.", "Second one", "[Tag] Third one."]
    # a clause chain with no full stops (reference provenance) is still split at semicolons
    assert len(bullets("; ".join(f"clause {i} with some words in it" for i in range(20)), max_words=30)) > 1


def test_polymer_units_are_drawn_with_exchange_and_rnoe_labels():
    assert polymer_units("Glycosaminoglycans (GAG)")[0] == "glycosaminoglycan"
    assert polymer_units("Glycogen (glycoCEST)")[0] == "glycogen"
    assert polymer_units("Creatine") is None
    eps = [_ep("hydroxyl OH", "hydroxyl", 1.0, n=3), _ep("N-acetyl amide NH", "amide", 3.2),
           _ep("aliphatic C-H (rNOE)", "rnoe", -3.5, exch=False, n=13)]
    units = render_polymer_units_png_base64("GAG", eps)
    assert len(units) == 6 and all(png for _, png, _, _ in units)
    assert all("verified" in note for _, _, note, _ in units)
    # the amide's carbonyl oxygen must not be labelled: only heteroatoms carrying an H are
    png, unmatched = render_structure_png_base64("CC(=O)NC", [_ep("acetamide NH", "amide", 3.2)])
    assert png and not unmatched
