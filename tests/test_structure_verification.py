"""Tests for the deterministic structure check the chemist agent applies to its own SMILES
(chemist_agent._verify_structures, backed by cest_scout.skills.pubchem). The agent-level tests
stub out the PubChem lookup so they run offline; the live lookup itself is covered separately.
"""
import pytest

from cest_scout.agents import chemist_agent
from cest_scout.schemas import (
    CandidateMetabolite,
    ChemistFinding,
    ChemistReport,
    Citation,
    LiteratureReport,
)
from cest_scout.skills import pubchem

# D-glucose, as PubChem gives it.
GLUCOSE_SMILES = "C([C@@H]1[C@H]([C@@H]([C@H](C(O1)O)O)O)O)O"
# Glyceraldehyde -- a real molecule, but emphatically not glucose.
WRONG_GLUCOSE_SMILES = "C(C(C=O)O)O"


def _lit_report() -> LiteratureReport:
    return LiteratureReport(
        body_part="liver",
        metabolites=[
            CandidateMetabolite(
                name="glucose", aliases=["dextrose"], typical_concentration_mM=5.0,
                concentration_range_mM="4-6 mM", tissue_compartment="cytosol",
                animal_evidence_only=False,
                citations=[Citation(pmid="111", title="t", species="human", study_setting="human in vivo")],
            ),
        ],
        search_summary="test",
    )


def _chem_report(smiles: str | None) -> ChemistReport:
    return ChemistReport(findings=[
        ChemistFinding(metabolite_name="glucose", smiles=smiles,
                        structure_description="aldohexose", exchangeable_protons=[],
                        overall_argument="test"),
    ])


def test_replaces_confirmed_wrong_structure_with_pubchem_one(monkeypatch):
    monkeypatch.setattr(chemist_agent.pubchem, "verify_structure",
                        lambda name, smiles, aliases: {
                            "status": "confirmed_mismatch", "pubchem_smiles": GLUCOSE_SMILES,
                            "matched_name": "glucose", "reason": "mismatch"})
    report = _chem_report(WRONG_GLUCOSE_SMILES)

    chemist_agent._verify_structures(report, _lit_report())

    finding = report.findings[0]
    assert finding.smiles == GLUCOSE_SMILES
    assert "WRONG" in finding.structure_verification_note
    assert WRONG_GLUCOSE_SMILES in finding.structure_verification_note


def test_leaves_confirmed_matching_structure_untouched(monkeypatch):
    monkeypatch.setattr(chemist_agent.pubchem, "verify_structure",
                        lambda name, smiles, aliases: {
                            "status": "confirmed_match", "pubchem_smiles": GLUCOSE_SMILES,
                            "matched_name": "glucose", "reason": None})
    report = _chem_report(GLUCOSE_SMILES)

    chemist_agent._verify_structures(report, _lit_report())

    assert report.findings[0].smiles == GLUCOSE_SMILES
    assert report.findings[0].structure_verification_note == ""


def test_unverifiable_name_keeps_structure_but_says_so(monkeypatch):
    monkeypatch.setattr(chemist_agent.pubchem, "verify_structure",
                        lambda name, smiles, aliases: {
                            "status": "unverifiable", "pubchem_smiles": None,
                            "matched_name": None, "reason": "no PubChem record found"})
    report = _chem_report(WRONG_GLUCOSE_SMILES)

    chemist_agent._verify_structures(report, _lit_report())

    # "Not found on PubChem" is not evidence of being wrong, so the chemist's answer stands.
    assert report.findings[0].smiles == WRONG_GLUCOSE_SMILES
    assert "NOT independently verified" in report.findings[0].structure_verification_note


def test_pubchem_live_catches_a_wrong_glucose_structure():
    try:
        good = pubchem.verify_structure("glucose", GLUCOSE_SMILES)
        bad = pubchem.verify_structure("glucose", WRONG_GLUCOSE_SMILES)
    except Exception as exc:  # network unavailable in this environment
        pytest.skip(f"PubChem unreachable: {exc}")

    assert good["status"] == "confirmed_match"
    assert bad["status"] == "confirmed_mismatch"


def test_skeleton_key_ignores_protonation_state_but_not_connectivity():
    glutamic_acid = pubchem._skeleton_key("C(CC(=O)O)[C@@H](C(=O)O)N")
    assert pubchem._skeleton_key("N[C@@H](CCC(=O)[O-])C(=O)[O-]") == glutamic_acid
    # What one run's chemist offered as "glutamate": one carbon short, i.e. aspartate. The
    # rejection was correct, ionization or not.
    assert pubchem._skeleton_key("N[C@@H](CC(=O)[O-])C(=O)[O-]") != glutamic_acid
    assert pubchem._skeleton_key("CN(CC(=O)[O-])C(=[NH2+])N") == pubchem._skeleton_key("CN(CC(=O)O)C(=N)N")
    # Pyruvate offered as lactate is a different skeleton and must still fail.
    assert pubchem._skeleton_key("CC(=O)C(=O)[O-]") != pubchem._skeleton_key("CC(C(=O)[O-])O")
    # Isopropylamine offered as alanine, as one run did.
    assert pubchem._skeleton_key("CC(C)N") != pubchem._skeleton_key("C[C@@H](C(=O)O)N")
