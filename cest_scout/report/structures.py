"""Deterministic 2D structure rendering with exchangeable-proton and rNOE-proton labels.

RDKit is used purely as a drawing/substructure-matching tool -- it never invents chemistry the
chemist agent didn't propose. Given a SMILES and the chemist's ExchangeableProton entries
(free-text group names like "amide NH", "guanidinium NH2", "aliphatic C-H rNOE pool"), each group
is matched to a SMARTS substructure so the actual proton(s) can be drawn and labeled:

- exchangeable protons (chemical exchange, downfield): drawn explicitly on their N/O/S, red
  highlight, labeled with the chemist's ppm estimate;
- rNOE protons (non-exchangeable C-H whose saturation reaches water by the relayed NOE, upfield):
  their carbons are highlighted blue and one representative is labeled with the chemist's
  (negative) ppm estimate.

Groups that match no SMARTS pattern are returned as text so no chemist-identified group silently
disappears -- see the `unmatched` return value.

Large molecules (glycogen, glycosaminoglycans, the mobile-protein amide pool) have no single
"structure"; for those the report shows their basic repeating unit(s) instead -- every type when
there are several (hyaluronan, chondroitin 4-/6-sulfate, dermatan, keratan and heparan sulfate for
GAG; the alpha-1,4 chain unit and the alpha-1,6 branch point for glycogen) -- with the same proton
labels applied to each unit. The unit SMILES below were checked residue by residue against
PubChem's monosaccharide records (each glycosidic bond cleaved, every residue's full InChIKey
compared), which is recorded in each unit's note.
"""
from __future__ import annotations

import base64
import re

from rdkit import Chem
from rdkit.Chem import AllChem
from rdkit.Chem.Draw import rdMolDraw2D

from cest_scout.skills.exchange_reference import RNOE_CLASSES, classify_group

# Keyword -> SMARTS for the heteroatom bearing the exchangeable proton(s). Matched by substring
# against the chemist agent's free-text `group` field (lowercased); first match wins, most
# specific patterns listed first so e.g. "guanidinium" isn't caught by a generic "amine" pattern.
_GROUP_SMARTS: list[tuple[str, str]] = [
    ("guanidin", "[NX3][CX3](=[NX2+0,NX3+1])[NX3]"),
    ("amidin", "[NX3][CX3]=[NX2]"),
    ("imidazole", "[nH]1cncc1"),
    ("phosphoramid", "[NX3]P"),
    ("sulfonamide", "[NX3]S(=O)(=O)"),
    ("carboxamide", "[NX3][CX3]=O"),
    ("acetamid", "[NX3][CX3](=O)[CH3]"),
    ("n-acetyl", "[NX3][CX3](=O)[CH3]"),
    ("amide", "[NX3][CX3]=O"),
    ("thiol", "[SX2H]"),
    ("sulfhydryl", "[SX2H]"),
    ("enol", "[OX2H][#6]=[#6]"),
    ("phosphate", "[OX2H]P"),
    ("sulfate", "[OX2H]S(=O)(=O)"),
    ("carboxyl", "[OX2H][CX3]=O"),
    ("hydroxyl", "[OX2H;!$(O[CX3]=O);!$(OS(=O)=O)]"),
    ("alcohol", "[OX2H;!$(O[CX3]=O);!$(OS(=O)=O)]"),
    ("ammonium", "[NX4+]"),
    ("amine", "[NX3;!$(N-C=O);!$(N-S(=O)=O);!$(N=*)]"),
    ("imino", "[NX2H]"),
]

# Carbons bearing the protons that give rise to the relayed NOE: any sp3 C-H (ring C-H of sugars,
# the N-acetyl CH3, lipid/protein aliphatic CH/CH2/CH3). Aromatic C-H is the -1.6/-aromatic case.
_RNOE_SMARTS = {"aliphatic": "[CX4;!H0]", "aromatic": "[c;!H0]"}


# --- Repeating units of the macromolecular pools ---------------------------------------------
# (label, SMILES with '*' attachment points, how it was verified)
_GLYCOGEN_UNITS = [
    ("glycogen: alpha-1,4-linked glucosyl unit (chain)",
     "OC[C@H]1O[C@H](O*)[C@H](O)[C@@H](O)[C@@H]1O*",
     "residue verified against PubChem alpha-D-glucose (full InChIKey match)"),
    ("glycogen: alpha-1,4 / alpha-1,6 branch-point glucosyl unit",
     "*OC[C@H]1O[C@H](O*)[C@H](O)[C@@H](O)[C@@H]1O*",
     "residue verified against PubChem alpha-D-glucose (full InChIKey match)"),
]
_GAG_UNITS = [
    ("hyaluronan: [GlcA-beta1,3-GlcNAc-beta1,4] disaccharide",
     "OC[C@H]1O[C@@H](O*)[C@H](NC(C)=O)[C@@H](O[C@@H]2O[C@@H]([C@@H](O)[C@H](O)[C@H]2O)C(O)=O)[C@@H]1O*",
     "residues verified against PubChem N-acetyl-beta-D-glucosamine and beta-D-glucuronic acid"),
    ("chondroitin 4-sulfate: [GlcA-beta1,3-GalNAc(4S)-beta1,4] disaccharide",
     "OC[C@H]1O[C@@H](O*)[C@H](NC(C)=O)[C@@H](O[C@@H]2O[C@@H]([C@@H](O)[C@H](O)[C@H]2O)C(O)=O)[C@H]1OS(O)(=O)=O",
     "residues verified against PubChem N-acetyl-beta-D-galactosamine and beta-D-glucuronic acid (sulfate position by construction)"),
    ("chondroitin 6-sulfate: [GlcA-beta1,3-GalNAc(6S)-beta1,4] disaccharide",
     "O=S(=O)(O)OC[C@H]1O[C@@H](O*)[C@H](NC(C)=O)[C@@H](O[C@@H]2O[C@@H]([C@@H](O)[C@H](O)[C@H]2O)C(O)=O)[C@H]1O*",
     "residues verified against PubChem N-acetyl-beta-D-galactosamine and beta-D-glucuronic acid (sulfate position by construction)"),
    ("dermatan sulfate: [IdoA-alpha1,3-GalNAc(4S)-beta1,4] disaccharide",
     "*O[C@@H]1O[C@H](CO)[C@H](OS(=O)(=O)O)[C@H](O[C@@H]2O[C@@H](C(=O)O)[C@@H](O)[C@H](O)[C@H]2O)[C@H]1NC(C)=O",
     "residues verified against PubChem N-acetyl-beta-D-galactosamine and alpha-L-iduronic acid (sulfate position by construction)"),
    ("keratan sulfate: [Gal-beta1,4-GlcNAc(6S)-beta1,3] disaccharide",
     "O=S(=O)(O)OC[C@H]1O[C@@H](O*)[C@H](NC(C)=O)[C@@H](O*)[C@@H]1O[C@@H]1O[C@H](CO)[C@H](O)[C@H](O)[C@H]1O",
     "residues verified against PubChem N-acetyl-beta-D-glucosamine and beta-D-galactose (sulfate position by construction)"),
    ("heparan sulfate (unsulfated backbone): [GlcA-beta1,4-GlcNAc-alpha1,4] disaccharide",
     "OC[C@H]1O[C@H](O*)[C@H](NC(C)=O)[C@@H](O)[C@@H]1O[C@@H]1O[C@@H]([C@@H](O*)[C@H](O)[C@H]1O)C(O)=O",
     "residues verified against PubChem N-acetyl-alpha-D-glucosamine and beta-D-glucuronic acid; N-/O-sulfation varies and is not drawn"),
]
_PROTEIN_UNITS = [
    ("mobile protein / peptide backbone: one amide N-H per residue (Gly-Ala-Ser fragment shown)",
     "*NCC(=O)N[C@@H](C)C(=O)N[C@@H](CO)C(=O)*",
     "generic backbone fragment; not a PubChem compound"),
]
POLYMER_UNITS: list[tuple[re.Pattern, str, list[tuple[str, str, str]]]] = [
    (re.compile(r"glycogen|glycocest|glyconoe"), "glycogen", _GLYCOGEN_UNITS),
    (re.compile(r"glycosaminoglycan|gagcest|\bgags?\b|chondroitin|hyaluron|keratan|dermatan|heparan|proteoglycan|aggrecan"),
     "glycosaminoglycan", _GAG_UNITS),
    (re.compile(r"amide proton transfer|\bapt\b|mobile[- ]protein|protein amide|peptide backbone|mobile[- ]peptide|backbone amide"),
     "mobile protein amide pool", _PROTEIN_UNITS),
]


def polymer_units(metabolite_name: str) -> tuple[str, list[tuple[str, str, str]]] | None:
    """(family, [(label, smiles, verification note), ...]) for a macromolecular pool, else None."""
    lowered = (metabolite_name or "").lower()
    for pattern, family, units in POLYMER_UNITS:
        if pattern.search(lowered):
            return family, units
    return None


def _is_rnoe_group(ep) -> bool:
    cls = classify_group(getattr(ep, "group", ""), getattr(ep, "group_class", ""))
    return cls in RNOE_CLASSES


def _match_group_atoms(mol: Chem.Mol, group_name: str) -> tuple[int, ...]:
    name = group_name.lower()
    for keyword, smarts in _GROUP_SMARTS:
        if keyword not in name:
            continue
        patt = Chem.MolFromSmarts(smarts)
        if patt is None:
            continue
        matches = mol.GetSubstructMatches(patt)
        if matches:
            atoms: set[int] = set()
            for m in matches:
                atoms.update(m)
            # Keep only the heteroatom(s) (N/O/S) that actually carry a proton -- an amide's
            # carbonyl oxygen matched the pattern too and used to get a ppm label. A charged
            # N+ with no H in this SMILES is kept so the estimate is not silently lost.
            hetero = [a for a in atoms if mol.GetAtomWithIdx(a).GetAtomicNum() in (7, 8, 16)]
            with_h = [a for a in hetero if mol.GetAtomWithIdx(a).GetTotalNumHs() > 0]
            return tuple(with_h or hetero)
    return ()


def _rnoe_atoms(mol: Chem.Mol, group_name: str) -> tuple[int, ...]:
    kind = "aromatic" if "aromatic" in group_name.lower() else "aliphatic"
    patt = Chem.MolFromSmarts(_RNOE_SMARTS[kind])
    return tuple(sorted({m[0] for m in mol.GetSubstructMatches(patt)}))


# Basic nitrogens are drawn in their physiological (pH ~7.4) protonation state when the chemist
# counts the extra proton: a guanidine's imine N becomes =NH2+ (creatine's guanidinium then shows
# all four N-H), a primary/secondary amine becomes NH3+/NH2+. PubChem's SMILES are neutral, so
# without this the drawing showed one proton fewer than the pool that was simulated.
_PROTONATABLE = {
    "guanidin": "[NX2;H1,H0;!+]=[CX3]([NX3])[NX3]",   # imine N of a neutral guanidine
    "amidin": "[NX2;H1;!+]=[CX3][NX3]",
    "amine": "[NX3;H2,H1;!+;!$(N-C=O);!$(N-S(=O)=O);!$(N=*);!$(N-[CX3]=[NX2])]",
    "imidazole": "[nX2;!+]1cncc1",
}


def _drawn_h(mol: Chem.Mol, atoms: tuple[int, ...]) -> int:
    return sum(mol.GetAtomWithIdx(a).GetTotalNumHs() for a in atoms)


def _protonate_to_match(mol: Chem.Mol, group_name: str, atoms: tuple[int, ...], wanted: int) -> tuple[Chem.Mol, tuple[int, ...]]:
    """Add protons to basic N atoms of this group until the drawn N-H count reaches the chemist's
    n_protons (never beyond one extra proton per N). Returns the (possibly new) mol and atoms."""
    name = group_name.lower()
    key = next((k for k in _PROTONATABLE if k in name), None)
    if key is None or wanted <= _drawn_h(mol, atoms):
        return mol, atoms
    rw = Chem.RWMol(mol)
    patt = Chem.MolFromSmarts(_PROTONATABLE[key])
    for match in rw.GetSubstructMatches(patt):
        n_idx = match[0]
        if n_idx not in atoms and key != "amine":
            continue
        at = rw.GetAtomWithIdx(n_idx)
        if at.GetFormalCharge() != 0:
            continue
        at.SetFormalCharge(1)
        at.SetNumExplicitHs(at.GetTotalNumHs() + 1)
        at.SetNoImplicit(True)
        try:
            Chem.SanitizeMol(rw)
        except Exception:
            return mol, atoms
        atoms = tuple(sorted(set(atoms) | {n_idx}))
        if wanted <= _drawn_h(rw, atoms):
            break
    return rw.GetMol(), atoms


# Okabe-Ito colour-blind-safe highlight fills: orange for exchangeable protons, sky blue for
# rNOE carbons.
_EXCH_COLOR = (0.98, 0.78, 0.45)
_RNOE_COLOR = (0.60, 0.80, 0.95)


def _label_for(ep, sim_by_group: dict | None) -> str:
    """The shift written on the drawing is the one the simulation used (reference table), never
    the chemist's own estimate: 'not simulated' when the group had no verified reference."""
    if sim_by_group is None:
        return f"{ep.ppm_shift_estimate:+.2f} ppm (est.)"
    sp = sim_by_group.get(ep.group.strip().lower())
    if sp is None:
        return "not simulated"
    if sp.status == "simulated" and sp.shift_ppm is not None:
        return f"{sp.shift_ppm:+.1f} ppm"
    return "not simulated"


def render_structure_png_base64(
    smiles: str, exchangeable_protons: list, width: int = 440, height: int = 330, legend: str = "",
    simulation_parameters: list | None = None,
) -> tuple[str, list[str]]:
    """Returns (base64-encoded PNG, list of human-readable notes for chemist-identified groups
    that could not be matched to any SMARTS pattern and so are not highlighted on the drawing).
    Labels carry the reference shift the simulation used (or 'not simulated') when
    simulation_parameters are given; the chemist's estimate only when they are not. The PNG
    carries at most a short one-line legend (the unit's name); the colour key is written once in
    the HTML instead of into every picture."""
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"RDKit could not parse SMILES: {smiles!r}")
    sim_by_group = ({sp.group_text.strip().lower(): sp for sp in simulation_parameters}
                    if simulation_parameters is not None else None)

    hetero_to_label: dict[int, str] = {}
    rnoe_to_label: dict[int, str] = {}
    unmatched: list[str] = []
    for ep in exchangeable_protons:
        label = _label_for(ep, sim_by_group)
        if _is_rnoe_group(ep):
            atoms = _rnoe_atoms(mol, ep.group)
            if not atoms:
                unmatched.append(f"{ep.group} ({label}, rNOE) -- no C-H found to mark")
                continue
            for a in atoms:
                rnoe_to_label[a] = f"rNOE {label}"
            continue
        if not ep.is_exchangeable:
            continue
        atoms = _match_group_atoms(mol, ep.group)
        if not atoms:
            unmatched.append(f"{ep.group} ({label}) -- not shown on structure, no substructure match")
            continue
        mol, atoms = _protonate_to_match(mol, ep.group, atoms, int(getattr(ep, "n_protons", 0) or 0))
        for a in atoms:
            hetero_to_label[a] = label if a not in hetero_to_label else f"{hetero_to_label[a]} / {label}"

    mol_h = Chem.AddHs(mol, onlyOnAtoms=list(hetero_to_label.keys())) if hetero_to_label else Chem.Mol(mol)
    AllChem.Compute2DCoords(mol_h)

    highlight: dict[int, tuple] = {}
    for hetero_idx, label in hetero_to_label.items():
        h_neighbors = [nbr.GetIdx() for nbr in mol_h.GetAtomWithIdx(hetero_idx).GetNeighbors() if nbr.GetAtomicNum() == 1]
        if h_neighbors:
            for h_idx in h_neighbors:
                mol_h.GetAtomWithIdx(h_idx).SetProp("atomNote", label)
                highlight[h_idx] = _EXCH_COLOR
        else:
            # No explicit H even after AddHs (e.g. a charged N+ with no attached H in this SMILES) --
            # label the heteroatom itself so the ppm estimate isn't silently lost.
            mol_h.GetAtomWithIdx(hetero_idx).SetProp("atomNote", label)
        highlight[hetero_idx] = _EXCH_COLOR
    if rnoe_to_label:
        # One label per drawing keeps the picture readable; every rNOE carbon is still coloured.
        first = min(rnoe_to_label)
        mol_h.GetAtomWithIdx(first).SetProp("atomNote", rnoe_to_label[first])
        for a in rnoe_to_label:
            highlight.setdefault(a, _RNOE_COLOR)

    drawer = rdMolDraw2D.MolDraw2DCairo(width, height)
    opts = drawer.drawOptions()
    opts.legendFontSize = 12
    opts.annotationFontScale = 0.7
    opts.padding = 0.16              # room for atom notes at the edge: units were cut off before
    rdMolDraw2D.PrepareAndDrawMolecule(
        drawer, mol_h, legend=legend[:60], highlightAtoms=list(highlight), highlightAtomColors=highlight,
    )
    drawer.FinishDrawing()
    png_bytes = drawer.GetDrawingText()
    return base64.b64encode(png_bytes).decode("ascii"), unmatched


def render_polymer_units_png_base64(
    metabolite_name: str, exchangeable_protons: list, width: int = 420, height: int = 300,
    simulation_parameters: list | None = None,
) -> list[tuple[str, str, str, list[str]]]:
    """For a macromolecular pool: [(unit label, base64 PNG, verification note, unmatched)] for
    every repeating-unit type; [] when the name is not a known polymer. The picture carries only
    the unit's short name; the full label and note go in the HTML caption."""
    found = polymer_units(metabolite_name)
    if not found:
        return []
    _, units = found
    out = []
    for label, smiles, note in units:
        short = label.split(":")[0].strip()
        png, unmatched = render_structure_png_base64(smiles, exchangeable_protons, width, height, legend=short,
                                                     simulation_parameters=simulation_parameters)
        out.append((label, png, note, unmatched))
    return out
