"""Agent 2: the chemist. Given Agent 1's candidate metabolites, works out
each molecule's structure, argues which protons are exchangeable with bulk
water, and tabulates the resulting resonance offset at 0.5/1.5/3/5/7 T.
"""
from __future__ import annotations

import json
import re

from openai import OpenAI

from cest_scout.llm import ToolCallback, run_research_agent
from cest_scout.schemas import ChemistReport, FrequencyTableRow, LiteratureReport
from cest_scout.skills import frequency_calc, pubchem

SYSTEM_PROMPT = """\
You are an organic/biophysical chemist supporting an MRI physics team building CEST (Chemical \
Exchange Saturation Transfer) contrast agents.

You will be given a list of candidate endogenous metabolites (with their tissue and typical \
concentration) found by a biology literature agent. For EACH metabolite:

1. Give its chemical structure: a SMILES string, plus a short structure_description (functional \
   groups present). Your SMILES WILL be checked atom-for-atom against PubChem's real structure for \
   this metabolite and replaced if it is wrong, so write it carefully -- get the ring size, the \
   number of carbons, and the placement of every OH/NH/COOH right, and double-check sugars and \
   other stereochemically dense molecules in particular. If you genuinely do not know the exact \
   structure, leave smiles null and describe the structure in words instead: a null SMILES is \
   fine, a confidently wrong one poisons everything downstream (the drawn structure and the \
   exchangeable-proton matching are both derived from it).
2. Identify every plausible EXCHANGEABLE proton group (amide NH, amine NH2/NH3+, hydroxyl OH, \
   guanidinium NH, imidazole NH, etc.) on the molecule. For each group, argue explicitly whether \
   it is exchangeable with bulk water protons under physiological pH/temperature, state the \
   number of protons in that group, your best estimate of its chemical shift vs. water in ppm, \
   and classify its likely exchange regime (slow/intermediate/fast) with brief reasoning (fast- \
   exchanging groups like free OH/NH3+ are often not cleanly CEST-visible in vivo due to line \
   broadening merging into the water peak, unless especially slowed by pH/temperature). \
   Do NOT list acid protons as exchangeable pools: carboxylic acid -COOH, sulfonic acid -SO3H and \
   phosphate P-OH groups are deprotonated at pH 7.4 (pKa well below 7) and carry no proton, so \
   they give no CEST signal; the pipeline marks such groups not exchangeable. Alcoholic/sugar \
   -OH groups are the hydroxyl class. \
   Set group_class to EXACTLY one of: amide, guanidinium, amine, hydroxyl, imidazole, thiol, \
   phosphate, rnoe, rnoe16, other. This matters more than your ppm or regime estimate: the \
   simulation takes its exchange rate and chemical shift from a cited reference table keyed by \
   this class, not from your numbers, so a group classed wrongly is simulated with the wrong \
   physics. For a polymer or macromolecular pool, describe the repeating unit's groups (glycogen: \
   the glucosyl unit's three hydroxyls; a protein amide pool: one backbone amide per unit; GAG: \
   the disaccharide unit's hydroxyls and its N-acetyl amide). n_protons is per molecule or per \
   repeating unit, never a whole-polymer count. The report draws the repeating units of glycogen, \
   glycosaminoglycans and the protein backbone itself, so for those a null SMILES is fine.
   rNOE POOLS (upfield, negative ppm): a macromolecule's NON-exchangeable C-H protons (sugar ring \
   C-H and N-acetyl CH3 of glycogen/GAG, aliphatic side chains of mobile proteins, lipid CH2) \
   transfer saturation to water through the relayed nuclear Overhauser effect and appear UPFIELD \
   of water. For every macromolecular or lipid pool, ALSO list one such group: group e.g. \
   'aliphatic C-H (rNOE)', group_class 'rnoe' (the ~-3.5 ppm aliphatic pool; glycogen's glycoNOE \
   sits near -1 ppm) or 'rnoe16' (the distinct -1.6 ppm membrane-lipid pool), is_exchangeable \
   false, a NEGATIVE ppm_shift_estimate, exchange_regime 'rNOE (dipolar, relayed)', and n_protons \
   = the C-H protons per repeating unit (glucosyl unit: 7; GAG disaccharide: about 13 including \
   the N-acetyl CH3). Small metabolites (mM solutes) do not get an rNOE group.
3. Call the ppm_to_frequency_table tool for every distinct ppm shift you propose, to sanity-check \
   the exact Larmor frequency and offset-from-water in Hz at 0.5, 1.5, 3, 5, and 7 T (the exact \
   per-group frequency table in your final answer will be recomputed deterministically from your \
   ppm_shift_estimate values, so focus your effort on getting the ppm shift right).
4. Give an overall_argument: your net judgment on whether/why this metabolite should be \
   CEST-visible, referencing the specific exchangeable groups that matter most.

Be rigorous and specific with ppm shifts (use real biochemistry/NMR knowledge, e.g. amide NH \
~3.5 ppm, guanidinium ~2 ppm, hydroxyl ~0.5-1.5 ppm, amine ~2-3 ppm, aliphatic rNOE ~-3.5 ppm -- \
adjust per molecule). If a metabolite has no plausible exchangeable protons, say so plainly and \
argue it is not CEST-visible. Keep every free-text field under 120 words; short sentences.
"""


def run(
    client: OpenAI, model: str, literature_report: LiteratureReport, on_tool_call: ToolCallback | None = None,
    extra_body: dict | None = None, stats_sink: dict | None = None,
) -> ChemistReport:
    metabolite_summaries = [
        {
            "name": m.name,
            "aliases": m.aliases,
            "typical_concentration_mM": m.typical_concentration_mM,
            "tissue_compartment": m.tissue_compartment,
        }
        for m in literature_report.metabolites
    ]
    user_prompt = (
        f"Body part / tissue: {literature_report.body_part}\n\n"
        "Candidate metabolites from the literature agent:\n"
        f"{json.dumps(metabolite_summaries, indent=2)}\n\n"
        "Produce a ChemistFinding for every metabolite listed above."
    )
    report = run_research_agent(
        client=client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        skills=[frequency_calc],
        schema_model=ChemistReport,
        max_tool_iters=max(10, 2 * len(metabolite_summaries) + 4),
        on_tool_call=on_tool_call,
        extra_body=extra_body,
        output_check=_make_completeness_check([m["name"] for m in metabolite_summaries]),
        stats_sink=stats_sink,
    )
    _fill_frequency_tables(report)
    _deprotonated_acid_groups(report)
    _verify_structures(report, literature_report)
    return report


_ACID_GROUP_RE = re.compile(r"carboxyl|carboxylic|\bcooh\b|\bco2h\b|sulfon|sulfate|sulphate|phosph[oa][a-z]*\s*(?:oh|acid|hydroxyl)|\bso3h\b|p-?oh", re.I)


def _deprotonated_acid_groups(report: ChemistReport) -> None:
    """Acid protons (carboxyl, sulfonate, phosphate P-OH) are gone at pH 7.4, so a chemist entry
    that lists one as an exchangeable pool is corrected deterministically rather than simulated
    with hydroxyl physics (observed: a 'hydroxyl' at 3 ppm on glutathione)."""
    for finding in report.findings:
        for ep in finding.exchangeable_protons:
            if ep.is_exchangeable and _ACID_GROUP_RE.search(ep.group or ""):
                ep.is_exchangeable = False
                ep.reasoning = (f"{ep.reasoning} [Corrected] '{ep.group}' is an acid group, deprotonated at pH 7.4 "
                                "(no proton to exchange), so it is not simulated.").strip()


def _make_completeness_check(expected_names: list[str]):
    """Require a ChemistFinding for every metabolite handed to this agent.

    The agent is asked to produce one per input, but a schema-valid answer covering a subset
    passes validation just as happily, and the metabolites it omits then vanish from the rest of
    the pipeline without trace.
    """
    def check(report: ChemistReport) -> str | None:
        got = {f.metabolite_name.strip().lower() for f in report.findings}
        missing = [n for n in expected_names if n.strip().lower() not in got]
        if not missing:
            return None
        return (
            f"Your JSON is missing a ChemistFinding for: {', '.join(missing)}. Every metabolite in "
            "the input list needs one, including any you judge NOT to be CEST-visible -- for those, "
            "say so in overall_argument and give an empty exchangeable_protons list. Re-emit the "
            "complete JSON with all of them."
        )
    return check


def _verify_structures(report: ChemistReport, literature_report: LiteratureReport) -> None:
    """Check every proposed SMILES against PubChem's real structure for that metabolite name --
    a model can confidently emit a chemically wrong structure for even a textbook metabolite
    (observed in practice: a wrong glucose structure), and every downstream artifact (the drawn
    2D structure, the exchangeable-proton substructure matching) inherits that error silently.

    A confirmed mismatch is REPLACED with PubChem's structure rather than just flagged, since
    here -- unlike a fabricated citation -- the database hands us the correct answer directly.
    A name PubChem simply doesn't know is left alone with an honest "not verified" note: failing
    to resolve a name is not evidence the chemist was wrong.
    """
    aliases_by_name = {m.name: m.aliases for m in literature_report.metabolites}

    for finding in report.findings:
        # This field is in the schema the model fills, so clear whatever prose it put there:
        # only this verification writes it.
        finding.structure_verification_note = ""
        if not finding.smiles:
            continue
        aliases = aliases_by_name.get(finding.metabolite_name, [])
        try:
            result = pubchem.verify_structure(finding.metabolite_name, finding.smiles, aliases)
        except Exception as exc:  # network unavailable -- don't fail the whole run over this
            finding.structure_verification_note = f"Structure verification skipped: {exc}"
            continue

        if result["status"] == "confirmed_match":
            continue
        if result["status"] == "unverifiable":
            finding.structure_verification_note = (
                f"Structure NOT independently verified ({result['reason']}) -- shown as the chemist "
                "agent proposed it."
            )
            continue

        claimed = finding.smiles
        if result["pubchem_smiles"]:
            finding.smiles = result["pubchem_smiles"]
            finding.structure_verification_note = (
                f"Chemist agent's proposed structure was WRONG and has been replaced with PubChem's "
                f"verified structure for {result['matched_name']!r}. Rejected SMILES: {claimed!r}; "
                f"PubChem: {result['pubchem_smiles']!r}. The exchangeable-proton groups below are "
                "still the chemist's own analysis and may have been reasoned from the wrong structure."
            )
        else:
            finding.smiles = None
            finding.structure_verification_note = (
                f"Chemist agent's proposed structure was rejected ({result['reason']}) and no "
                f"replacement was available, so no structure is shown. Rejected SMILES: {claimed!r}."
            )


def _fill_frequency_tables(report: ChemistReport) -> None:
    """Overwrite every exchangeable proton's frequency table with an exact, deterministic
    recomputation from its ppm_shift_estimate -- never trust the model's own Hz arithmetic,
    even when it did call the ppm_to_frequency_table tool (it can still transcribe the wrong
    numbers into the final JSON).
    """
    for finding in report.findings:
        for ep in finding.exchangeable_protons:
            ep.frequency_table = [
                FrequencyTableRow(**row) for row in frequency_calc.ppm_to_frequency_table(ep.ppm_shift_estimate)
            ]
