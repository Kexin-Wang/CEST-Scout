"""Report fixes from the 2026-09-28 review: drawn protonation state matches the chemist's proton
count (creatine's guanidinium shows four N-H), constituents of flowing/secreted fluids are
excluded before simulation, and the HTML report carries a side outline with anchors."""
from __future__ import annotations

from rdkit import Chem

from cest_scout.agents.literature_agent import _exclude_flowing_fluid_constituents, _flowing_fluid_reason
from cest_scout.report.html import render_html
from cest_scout.report.structures import _match_group_atoms, _protonate_to_match, render_structure_png_base64
from cest_scout.schemas import CandidateMetabolite, ExchangeableProton, FinalMetabolite, FinalReport, LiteratureReport


def _ep(group, cls, ppm, n, exch=True):
    return ExchangeableProton(group=group, group_class=cls, n_protons=n, ppm_shift_estimate=ppm, exchange_regime="x",
                              is_exchangeable=exch, reasoning="r")


def test_creatine_guanidinium_is_drawn_protonated_with_four_labelled_protons():
    smiles = "CN(CC(=O)O)C(=N)N"                       # PubChem creatine, neutral
    mol = Chem.MolFromSmiles(smiles)
    atoms = _match_group_atoms(mol, "guanidinium NH2/NH")
    assert sum(mol.GetAtomWithIdx(a).GetTotalNumHs() for a in atoms) == 3   # neutral form: 3 N-H
    mol2, atoms2 = _protonate_to_match(mol, "guanidinium NH2/NH", atoms, 4)
    assert sum(mol2.GetAtomWithIdx(a).GetTotalNumHs() for a in atoms2) == 4  # =NH2+ drawn: 4 N-H
    assert any(mol2.GetAtomWithIdx(a).GetFormalCharge() == 1 for a in atoms2)
    png, unmatched = render_structure_png_base64(smiles, [_ep("guanidinium NH2/NH", "guanidinium", 2.0, 4)])
    assert png and not unmatched
    # a primary amine asked for as NH3+ gets its third proton; an amide is never protonated
    glu = Chem.MolFromSmiles("C(CC(=O)O)[C@@H](C(=O)O)N")
    a = _match_group_atoms(glu, "alpha-amine NH3+")
    g2, a2 = _protonate_to_match(glu, "alpha-amine NH3+", a, 3)
    assert sum(g2.GetAtomWithIdx(x).GetTotalNumHs() for x in a2) == 3
    gln = Chem.MolFromSmiles("C(CC(=O)N)[C@@H](C(=O)O)N")
    am = _match_group_atoms(gln, "side-chain amide NH2")
    g3, a3 = _protonate_to_match(gln, "side-chain amide NH2", am, 3)
    assert sum(g3.GetAtomWithIdx(x).GetTotalNumHs() for x in a3) == 2


def _cand(name, compartment):
    return CandidateMetabolite(name=name, typical_concentration_mM=10.0, concentration_range_mM="", tissue_compartment=compartment,
                               animal_evidence_only=False, citations=[])


def test_flowing_fluid_constituents_are_excluded_before_simulation():
    lit = LiteratureReport(body_part="pancreas", search_summary="", metabolites=[
        _cand("Bicarbonate (HCO3-) in pancreatic ductal secretions", "ductal fluid"),
        _cand("Glycogen", "acinar cells"),
        _cand("Taurine", "intracellular"),
    ])
    assert _flowing_fluid_reason(lit.metabolites[0], "pancreas").startswith("'Bicarbonate")
    assert _flowing_fluid_reason(lit.metabolites[1], "pancreas") == ""
    lit.metabolites.append(_cand("Water (bulk H2O pool)", "solvent"))
    lit.excluded = ["Collagen -- rigid, MT pool not CEST"]      # written by the model into the schema field
    _exclude_flowing_fluid_constituents(lit)
    assert [m.name for m in lit.metabolites] == ["Glycogen", "Taurine"]
    assert lit.excluded[0].startswith("[model's own exclusion]")
    assert sum(1 for x in lit.excluded if x.startswith("[pipeline]")) == 2       # bicarbonate + water
    assert any("not stored in the pancreas" in x for x in lit.excluded) and any("solvent" in x for x in lit.excluded)
    # the fluid itself as the target keeps its constituents
    csf = LiteratureReport(body_part="cerebrospinal fluid", search_summary="", metabolites=[_cand("Glucose", "CSF")])
    assert _flowing_fluid_reason(csf.metabolites[0], "cerebrospinal fluid") == ""


def test_html_has_a_side_outline_with_anchors_and_lists_exclusions():
    base = dict(typical_concentration_mM=1.0, concentration_range_mM="", animal_evidence_only=False,
                field_strengths_recommended_T=[], best_sequence_by_field=[], exchangeable_protons_summary="",
                key_citations=[], caveats="")
    report = FinalReport(body_part="pancreas", metabolites=[
        FinalMetabolite(name="Glycogen", detectable_by_cest=True, detectability="detectable", detection_mechanism="rNOE",
                        simulated_contrast_percent={"3.0": 1.0}, **base),
        FinalMetabolite(name="Taurine", detectable_by_cest=False, detectability="unknown", simulated_contrast_percent={}, **base),
    ], excluded_candidates=["'Bicarbonate' (ductal fluid) is a constituent of a flowing or secreted fluid (bicarbonate), not stored in the pancreas itself -- excluded before simulation"])
    html = render_html(report)
    assert "<nav class='toc'>" in html and "href='#m-0'" in html and "href='#m-1'" in html
    assert "<h2 id='m-0'>" in html and "<h2 id='m-1'>" in html
    assert "Excluded before simulation" in html and "Bicarbonate" in html
    assert "Plot simulation</button>" not in html or "water+MT" not in html


def test_structure_labels_use_the_simulated_reference_shift_not_the_chemists_estimate():
    from cest_scout.schemas import SimulationParameters
    from cest_scout.report.structures import _label_for
    ep = _ep("Amide NH", "amide", 5.5, 2)                 # chemist guessed 5.5 ppm
    sims = [SimulationParameters(metabolite_name="g", group_class="amide", group_text="Amide NH", status="simulated", shift_ppm=3.5),
            SimulationParameters(metabolite_name="g", group_class="thiol", group_text="Thiol SH", status="no_reference_for_group")]
    by = {sp.group_text.lower(): sp for sp in sims}
    assert _label_for(ep, by) == "+3.5 ppm"
    assert _label_for(_ep("Thiol SH", "thiol", 3.0, 1), by) == "not simulated"
    assert _label_for(ep, None).endswith("(est.)")


def test_acid_groups_are_marked_not_exchangeable():
    from cest_scout.agents.chemist_agent import _deprotonated_acid_groups
    from cest_scout.schemas import ChemistFinding, ChemistReport
    rep = ChemistReport(findings=[ChemistFinding(metabolite_name="glutathione", structure_description="d", overall_argument="a",
                                                 exchangeable_protons=[_ep("Carboxyl OH", "hydroxyl", 3.0, 2), _ep("Amide NH", "amide", 3.5, 2),
                                                                       _ep("sugar hydroxyl OH", "hydroxyl", 1.0, 3)])])
    _deprotonated_acid_groups(rep)
    eps = rep.findings[0].exchangeable_protons
    assert not eps[0].is_exchangeable and "[Corrected]" in eps[0].reasoning
    assert eps[1].is_exchangeable and eps[2].is_exchangeable


def test_single_reference_value_is_exact_and_its_pmid_stays_with_it():
    from cest_scout.skills.exchange_reference import ReferenceEntry, lookup
    from cest_scout.report.prose import bullets
    e = (ReferenceEntry(group_class="hydroxyl", metabolite="glycogen", aliases=[], shift_ppm=1.0, shift_range_ppm=None,
                        kex_per_s=31850.0, kex_range_per_s=None, tissue="phantom", species="rat", measurement_type="phantom/solution",
                        conditions="9.4 T; 37 C; pH 7", pmid="38933551", title="t", quote="q"),)
    r = lookup("hydroxyl", "Glycogen", [], tissue="liver", entries=e)
    assert r.kex_per_s == 31850.0 and "31850 s^-1 from 31850" in r.provenance
    first = next(b for b in bullets(r.provenance, 40) if "31850" in b)
    assert "PMID 38933551" in first


def test_tissue_table_values_link_to_a_numbered_source_list():
    from cest_scout.report.html import _tissue_parameters_block
    from cest_scout.schemas import TissueParameterRow
    rows = [TissueParameterRow(tissue="liver", role="target tissue", field_T=3.0, parameter="water T1", value="808 ms",
                               source="PMID 1 (human in vivo, human)", verified="abstract")]
    h = _tissue_parameters_block(rows)
    assert "href='#tsrc-1'" in h and "id='tsrc-1'" in h and "class='v-abstract'" in h and "title=" not in h
    assert "pubmed.ncbi.nlm.nih.gov/1/" in h and "Hover" not in h
