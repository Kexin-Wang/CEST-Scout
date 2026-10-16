"""Deterministic verification of a chemist-proposed molecular structure against PubChem.

Not exposed as an LLM tool (no TOOL_SPEC/call here) -- this is a post-hoc check the chemist agent
applies to its own output after the LLM call, in the same spirit as
cest_scout.skills.pubmed.verify_titles: a model can propose a chemically wrong SMILES for a
well-known metabolite (observed in practice: an incorrect glucose structure) and still look
complete/confident, so structure identity is checked against a real database rather than trusted
on the model's word.
"""
from __future__ import annotations

import requests
from rdkit import Chem
from rdkit.Chem.MolStandardize import rdMolStandardize

_UNCHARGER = rdMolStandardize.Uncharger()

PUBCHEM_REST = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"


def _skeleton_key(smiles: str) -> str | None:
    """First block of the InChIKey: encodes molecular connectivity/skeleton only, ignoring
    stereochemistry and protonation-state differences that are not what this check cares about
    (the chemist agent is not expected to get stereochemistry perfectly right; a wrong skeleton
    -- wrong atoms, wrong connectivity -- is the kind of error this exists to catch).
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None
    # Neutralize first so a model writing the ionized form of the right compound (carboxylate,
    # guanidinium) is compared on its skeleton alone, not penalized for the charge state.
    try:
        mol = _UNCHARGER.uncharge(mol)
    except Exception:
        pass
    inchikey = Chem.MolToInchiKey(mol)
    return inchikey.split("-")[0] if inchikey else None


def lookup_pubchem_smiles(name: str) -> str | None:
    """Best-effort PubChem lookup of a compound's isomeric SMILES by name. Returns None if
    PubChem has no record for this exact name (an unrecognized/obscure name is not itself
    evidence the proposed structure is wrong -- see verify_structure's "unverifiable" status).
    """
    resp = requests.get(
        f"{PUBCHEM_REST}/compound/name/{requests.utils.quote(name)}/property/IsomericSMILES/JSON",
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    props = data.get("PropertyTable", {}).get("Properties", [])
    return props[0]["SMILES"] if props and "SMILES" in props[0] else None


def verify_structure(name: str, claimed_smiles: str, aliases: list[str] | None = None) -> dict:
    """Compare a claimed SMILES against PubChem's record for `name` (falling back through
    `aliases` if the primary name has no record). Returns a dict with:
      status: "confirmed_match" | "confirmed_mismatch" | "unverifiable"
      pubchem_smiles: the PubChem SMILES actually compared against, or None
      matched_name: which name/alias produced a PubChem record, or None

    "unverifiable" (no PubChem record for any name tried) is NOT the same as "confirmed_mismatch"
    -- an obscure metabolite name failing to resolve on PubChem is not evidence the chemist's
    structure is wrong, so callers must not treat the two the same way.
    """
    claimed_skeleton = _skeleton_key(claimed_smiles)
    if claimed_skeleton is None:
        return {"status": "confirmed_mismatch", "pubchem_smiles": None, "matched_name": None,
                "reason": f"claimed SMILES {claimed_smiles!r} does not even parse as a valid molecule"}

    for candidate_name in [name, *(aliases or [])]:
        if not candidate_name:
            continue
        try:
            pubchem_smiles = lookup_pubchem_smiles(candidate_name)
        except Exception:
            continue
        if not pubchem_smiles:
            continue
        pubchem_skeleton = _skeleton_key(pubchem_smiles)
        if pubchem_skeleton is None:
            continue
        if pubchem_skeleton == claimed_skeleton:
            return {"status": "confirmed_match", "pubchem_smiles": pubchem_smiles,
                    "matched_name": candidate_name, "reason": None}
        return {"status": "confirmed_mismatch", "pubchem_smiles": pubchem_smiles,
                "matched_name": candidate_name,
                "reason": f"PubChem's structure for {candidate_name!r} does not match the claimed SMILES"}

    return {"status": "unverifiable", "pubchem_smiles": None, "matched_name": None,
            "reason": "no PubChem record found for this name or its aliases"}
