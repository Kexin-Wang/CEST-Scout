"""Agent 1: the biologist. Finds candidate CEST-detectable metabolites in a
given body part/tissue by searching PubMed, keeping only those reported at
>1 mM tissue concentration, and flagging when supporting evidence is
primarily from animal models rather than humans.
"""
from __future__ import annotations

import re

from openai import OpenAI

from cest_scout.llm import ToolCallback, run_research_agent
from cest_scout.schemas import LiteratureReport
from cest_scout.skills import pubmed, reference_data

SYSTEM_PROMPT = """\
You are a meticulous biology/biochemistry literature researcher supporting an MRI physics \
team building CEST (Chemical Exchange Saturation Transfer) contrast agents.

Task: given a body part or tissue, search PubMed (via search_pubmed) to identify endogenous \
metabolites/molecules present in that tissue at a typical concentration ABOVE 1 mM. Only \
report metabolites you found explicit or well-supported concentration evidence for -- do not \
invent numbers.

WHAT COUNTS AS A CANDIDATE (this rules out most of what a naive literature search returns):
- It must be a CHEMICALLY DEFINED endogenous species whose exchangeable-proton pool you can \
  quantify -- either a single small molecule, OR a polymer/macromolecular pool defined by a \
  repeating or averaged structural unit (glycogen as its glucosyl unit, glycosaminoglycans as \
  their disaccharide unit, the mobile protein/peptide backbone as its amide unit). For a polymer, \
  report typical_concentration_mM and n_exchangeable_protons PER THAT UNIT, not per whole \
  molecule -- e.g. glycogen's concentration in glucosyl-unit mM, with 3 exchangeable hydroxyls per \
  internal glucosyl unit. structure_description (for the chemist agent) should name the repeating \
  unit; a single SMILES is not required for a polymer.
- Still reject spectroscopic category labels that lump chemically UNRELATED compounds together \
  with no definable repeating unit -- "phosphomonoesters (PME)", "phosphodiesters (PDE)", "total \
  choline", "lipid CH2" and similar are 31P/1H MRS peak groupings, not molecules or polymers, and \
  cannot be simulated as a pool.
- It must be relevant to 1H CEST. CEST saturates EXCHANGEABLE HYDROGEN nuclei, so a species that \
  is only observable by 31P MRS (ATP-alpha/beta/gamma resonances, inorganic phosphate, \
  phosphocreatine as a 31P peak) is off-target here unless you are naming the actual molecule for \
  its exchangeable 1H groups. A 31P paper is still fine as a concentration SOURCE -- just report \
  the molecule, not the phosphorus resonance.
- It must be ENDOGENOUS and normally present. Reject drug metabolites, xenobiotic adducts, \
  toxin conjugates and anything that only appears after an experimental challenge (e.g. a \
  monocrotaline or paracetamol adduct measured in a perfused-organ study).
- It must plausibly HAVE exchangeable protons (-OH, -NH, -NH2, -NH3+, amide, guanidinium). A \
  species with none cannot produce CEST contrast no matter how concentrated it is.
- It must be STORED IN THE TISSUE ITSELF -- in its cells, parenchyma or extracellular matrix. A \
  species that is only present in a fluid flowing through or secreted by the tissue (bicarbonate \
  in pancreatic juice / ductal secretions, bile constituents, urine, blood plasma, CSF) is not a \
  CEST target OF THAT TISSUE however concentrated the fluid is, unless the requested target is \
  that fluid itself. Report the tissue's own concentration, never a secretion's.
MANDATORY per tissue: explicitly search for and either include, or explicitly note as absent with \
a reason, each of these three macromolecular CEST targets if they are physiologically present in \
this tissue: glycogen (glycoCEST), the mobile protein/peptide amide pool (APT), and \
glycosaminoglycans (gagCEST, mainly cartilage/disc but check relevance to this tissue). These are \
real, well-established CEST targets that a plain "<tissue> metabolomics" search will not surface \
-- search "<tissue> glycoCEST", "<tissue> amide proton transfer", "<tissue> glycogen \
concentration" explicitly.
If a search returns mostly non-candidates by the rules above, search again with different terms \
rather than filling the list with them.

STRICT RULE ON HALLUCINATION: "I don't know" or "I couldn't find a direct number" is an \
acceptable, expected answer for some metabolites -- a plausible-looking invented number or quote \
is not. Every PMID you cite and every quote you claim is verbatim WILL be checked against the \
actual PubMed record and stripped out if it doesn't match, so there is no benefit to guessing; \
there is only the cost of your report looking less trustworthy after the fact.

For each candidate metabolite:
- Cite the specific PubMed articles (by PMID) that support its presence and concentration.
- For EVERY citation, fill in species (a specific organism, e.g. "human", "Sprague-Dawley rat", \
  "C57BL/6 mouse" -- never just the word "animal"), n_subjects (how many subjects/animals the \
  study used, if stated), and study_setting ('human in vivo', 'animal in vivo', 'ex vivo tissue/\
  phantom', 'in vitro/solution phantom', or 'unspecified'). search_pubmed returns is_human_evidence/\
  is_animal_evidence (from MeSH headings) plus species_hints_from_mesh and study_setting_hint as \
  starting hints -- verify/refine them against the actual title and abstract text rather than \
  copying them blindly, since MeSH headings can be incomplete.
- If ALL your supporting citations for a metabolite are animal-only, set animal_evidence_only=true.
- Report a typical_concentration_mM (single best estimate) and a concentration_range_mM string \
  reflecting the range seen across sources.
- Set concentration_basis to 'direct_literature_quote' ONLY if you are copying a real sentence you \
  actually read in a search_pubmed result into concentration_evidence_quote verbatim (character for \
  character, not paraphrased, not reconstructed from memory) -- it WILL be checked against the \
  actual cited abstract text and cleared if it doesn't match. If no source gives you an exact \
  quotable number, set concentration_basis='estimated', leave concentration_evidence_quote blank, \
  and show your work instead -- an honest, shown-your-work estimate is far more useful than a \
  fabricated quote.
- WHEN ESTIMATING, EVERY NUMBER NEEDS A SOURCE ON IT. Fill reasoning_steps with the derivation \
  broken into steps, and give each step a source_type and a source_detail identifier. Prefer, in \
  this order: 'reference_database' (a database record you can name -- HMDB accession, BioNumbers \
  BNID, PubChem CID), 'reference_work' (a named handbook/textbook and its table, e.g. "Geigy \
  Scientific Tables", "Tietz Clinical Guide to Laboratory Tests", Williamson & Brosnan's tissue \
  metabolite tables in Bergmeyer's Methods of Enzymatic Analysis), then 'journal_article' (a PMID) \
  as the LOWER-weighted fallback -- reference works and curated databases are preferred over \
  individual papers because they are consensus values rather than one study's cohort. Use \
  'calculation' for a step that is pure arithmetic on earlier steps, and 'assumption' when you are \
  genuinely just asserting something. Marking a step 'assumption' is fine and honest; silently \
  burying an unsourced number inside a fluent sentence is not, and unsourced numeric steps are \
  detected and flagged in the final report. Also write a one-paragraph concentration_reasoning \
  summarizing the same derivation in prose.
- Prefer well-known, quantitatively documented metabolites (e.g. via MRS studies, biochemistry \
  textbook-level tissue assays) over obscure single-paper claims.
- Concentration evidence does not have to be in vivo: ex vivo tissue extracts, biopsy assays, and \
  phantom/solution-based concentration measurements are acceptable supporting evidence too -- just \
  record their study_setting accurately so the physicist agent can weigh them appropriately \
  (ex vivo/phantom concentration numbers are usually fine; it's EXCHANGE RATES from such studies \
  that need caveats, which is the physicist agent's job).

Do several searches (try tissue name + "concentration" + candidate metabolite names, tissue name \
+ "metabolomics", tissue name + "MRS" or "NMR" or "biochemistry"). Aim for 5-15 candidate \
metabolites when the tissue supports that many, fewer if the literature is sparse. Stop \
searching once you have solid coverage -- don't exceed the tool-call budget. Keep every \
free-text field under 120 words, in short sentences.
"""


def run(
    client: OpenAI, model: str, body_part: str, on_tool_call: ToolCallback | None = None,
    extra_body: dict | None = None, stats_sink: dict | None = None,
) -> LiteratureReport:
    user_prompt = (
        f"Body part / tissue: {body_part}\n\n"
        "Find candidate endogenous metabolites in this tissue with typical concentration > 1 mM, "
        "suitable for later evaluation as CEST MRI contrast targets."
    )
    report = run_research_agent(
        client=client,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        skills=[pubmed],
        schema_model=LiteratureReport,
        max_tool_iters=14,
        on_tool_call=on_tool_call,
        extra_body=extra_body,
        output_check=_check_output,
        stats_sink=stats_sink,
    )
    _exclude_flowing_fluid_constituents(report)
    _flag_remaining_eligibility_issues(report)
    _verify_citations(report)
    _verify_quotes(report)
    _attach_reference_concentrations(report)
    _flag_unsourced_reasoning(report)
    return report


# Constituents of fluids that pass through or are secreted by a tissue are not stored in it and
# are excluded before simulation (the user's rule: "bicarbonate is not detectable since it's
# flowing, and not stored in pancreas"). Matched against the name and the stated compartment;
# a fluid that IS the requested target (e.g. "cerebrospinal fluid") is of course kept.
# Whole words only ("taurine" must not match "urine"); blood/plasma/serum are left to the prompt
# rule because a tissue compartment often cites a blood value as a reference.
_FLOWING_FLUID_RE = re.compile(
    r"\b(bicarbonate|hco3-?|ductal (?:fluid|secretions?)|pancreatic (?:juice|secretions?)|exocrine secretions?|gastric (?:juice|acid|secretions?|fluid|lumen)|"
    r"bile|urine|urinary|cerebrospinal|csf|sweat|saliva|lymph)\b"
)


_FLUID_TARGET_WORDS = ("fluid", "csf", "blood", "plasma", "serum", "urine", "bile", "saliva", "lymph", "secretion", "juice")


def _flowing_fluid_reason(m, body_part: str) -> str:
    target = f" {body_part.lower()} "
    if any(w in target for w in _FLUID_TARGET_WORDS):
        return ""            # the requested target IS a fluid: its constituents are the point
    text = f"{m.name.lower()} | {m.tissue_compartment.lower()}"
    hit = _FLOWING_FLUID_RE.search(text)
    if not hit:
        return ""
    return (f"'{m.name}' ({m.tissue_compartment}) is a constituent of a flowing or secreted fluid "
            f"({hit.group(1)}), not stored in the {body_part} itself -- not a CEST target of this tissue; "
            "excluded before simulation")


def _exclude_flowing_fluid_constituents(report: LiteratureReport) -> None:
    """Deterministic exclusions: constituents of flowing/secreted fluids and the solvent. The
    `excluded` field is in the schema the model fills, so anything the model wrote there first
    is kept but labelled as the model's own reasoning, not the pipeline's check."""
    report.excluded = [x if x.startswith("[model") else f"[model's own exclusion] {x}" for x in report.excluded if x]
    kept = []
    for m in report.metabolites:
        reason = _flowing_fluid_reason(m, report.body_part)
        if not reason and _is_solvent(m.name):
            reason = f"'{m.name}' is the solvent whose protons CEST detects, not a metabolite -- excluded before simulation"
        if reason:
            report.excluded.append(f"[pipeline] {reason}")
        else:
            kept.append(m)
    report.metabolites = kept


def _flag_remaining_eligibility_issues(report: LiteratureReport) -> None:
    """Defense in depth for _check_output: if a category label or a macromolecule reported at
    its own molarity survives every retry, say so in the report rather than presenting it as an
    ordinary, trustworthy candidate. Notes only -- nothing is dropped here, since a metabolite
    that fails this heuristic may still have legitimate concentration evidence attached to it.
    """
    for m in report.metabolites:
        if any(p in m.name.lower() for p in _REJECTED_CATEGORY_PATTERNS):
            m.notes = (
                f"{m.notes} [Eligibility] '{m.name}' looks like a spectroscopic category label "
                "(e.g. a 31P MRS peak grouping), not a single molecule or a polymer with a "
                "definable repeating unit -- it survived the re-emission check meant to catch "
                "this, so treat it with more skepticism than the other candidates."
            ).strip()
        elif _is_solvent(m.name):
            m.notes = (
                f"{m.notes} [Eligibility] '{m.name}' is the solvent, not a metabolite -- it survived "
                "the re-emission check meant to remove it; disregard this entry."
            ).strip()
        elif (any(h in f" {m.name.lower()} " for h in _MACROMOLECULAR_NAME_HINTS)
              and m.typical_concentration_mM < _MACROMOLECULAR_MIN_MM):
            m.notes = (
                f"{m.notes} [Eligibility] typical_concentration_mM ({m.typical_concentration_mM:g} "
                "mM) looks like the intact macromolecule's own molarity rather than its "
                "exchangeable-proton pool concentration -- this survived the re-emission check "
                "meant to catch it, so the simulated contrast for this metabolite is likely a "
                "severe underestimate."
            ).strip()

    for name_a, name_b in _find_likely_duplicates(report.metabolites):
        for m in report.metabolites:
            if m.name in (name_a, name_b):
                m.notes = (
                    f"{m.notes} [Eligibility] shares its exact concentration and tissue "
                    f"compartment with {(name_b if m.name == name_a else name_a)!r} -- this "
                    "survived the re-emission check meant to catch duplicate entries, so treat "
                    "these two as likely the same underlying pool named twice."
                ).strip()


# The brief asks for 5-15 candidates where the literature supports it. A run that searched
# thoroughly and then wrote one metabolite into its JSON has lost work between research and
# extraction, not found a sparse tissue -- so ask once for the omission to be made good.
MIN_EXPECTED_CANDIDATES = 5

# Substring match against the lowercased name. Observed in practice: the model can write a note
# explicitly acknowledging "PME is a broad term ... specific molecules need to be identified" and
# include it as a candidate anyway. A prompt asking it not to is not enough on its own -- these
# patterns are checked deterministically rather than trusted to self-police.
_REJECTED_CATEGORY_PATTERNS = (
    "phosphomonoester", "phosphodiester", "total choline", "total creatine", "lipid ch2",
    "lipid ch3", "31p", "mm/mi", "myo-inositol/inositol",  # spectral-region shorthand, not molecules
    "metabolites", "intermediates",  # a plural is a class ("TCA cycle metabolites"), never one molecule
)

# A polymer/macromolecular pool's concentration must be expressed per its exchangeable-proton
# unit, not as the intact macromolecule's own molarity -- a protein or a GAG chain is one huge
# molecule at a tiny molar concentration even when its exchangeable-proton pool is abundant, and
# the model can silently report the wrong one despite the prompt's per-unit instruction (observed:
# an "APT pool" reported at 0.02 mM, i.e. protein molarity, when the amide-proton-equivalent
# concentration relevant to CEST is ~200-300 mM). Names matched heuristically; the floor is a
# sanity check; not a physiological claim about the true minimum for these pools.
# "glycogen" is here because five liver runs across four models reported it at 5.15, 260, 164,
# 100 and 2 mM -- the low values are whole-molecule or mis-unit figures, while the CEST-relevant
# number is ~200-400 mM of glucosyl units (each with 3 hydroxyl protons) in fed liver.
_MACROMOLECULAR_NAME_HINTS = (
    "protein", "peptide", "amide proton", " apt ", "glycosaminoglycan", "gag", "glycogen",
)
_MACROMOLECULAR_MIN_MM = 20.0


def _check_output(report: LiteratureReport) -> str | None:
    complaints = []

    n = len(report.metabolites)
    if n < MIN_EXPECTED_CANDIDATES:
        complaints.append(
            f"Your JSON lists only {n} candidate metabolite(s). The brief asks for 5-15 where the "
            "tissue supports it, and your PubMed searches above surfaced more than that. Include "
            "EVERY candidate your searches actually support -- do not invent any, but do not "
            "silently drop ones you already found. If this tissue genuinely has fewer than "
            f"{MIN_EXPECTED_CANDIDATES} well-supported candidates above 1 mM, leave the list as is "
            "and say why in search_summary."
        )

    rejected = [
        m.name for m in report.metabolites
        if any(p in m.name.lower() for p in _REJECTED_CATEGORY_PATTERNS)
    ]
    if rejected:
        complaints.append(
            f"Remove these -- they are spectroscopic category labels, not molecules or polymers "
            f"with a definable repeating unit, and the eligibility rules already told you to reject "
            f"them: {', '.join(rejected)}. If the literature you found for them actually names a "
            "specific molecule (e.g. a specific phosphomonoester), report that molecule instead."
        )

    flowing = [_flowing_fluid_reason(m, report.body_part) for m in report.metabolites]
    flowing = [r for r in flowing if r]
    if flowing:
        complaints.append(
            "Remove these -- they are constituents of a fluid that flows through or is secreted by the "
            "tissue, not species stored in the tissue, so they are not CEST targets of it: "
            + "; ".join(flowing) + ". Do not replace them with a secretion's concentration of anything else."
        )

    solvents = [m.name for m in report.metabolites if _is_solvent(m.name)]
    if solvents:
        complaints.append(
            f"Remove {', '.join(repr(s) for s in solvents)}: water is the solvent whose protons CEST "
            "detects, not a metabolite dissolved in it. Do not replace it with anything -- just drop it."
        )

    suspect = [
        m.name for m in report.metabolites
        if any(h in f" {m.name.lower()} " for h in _MACROMOLECULAR_NAME_HINTS)
        and m.typical_concentration_mM < _MACROMOLECULAR_MIN_MM
    ]
    if suspect:
        complaints.append(
            f"{', '.join(suspect)}: typical_concentration_mM looks like the intact macromolecule's "
            "own molarity, not its exchangeable-proton pool concentration per unit -- for a protein/"
            "peptide amide pool or a glycosaminoglycan, that is the wrong number for CEST purposes "
            "(a protein is one huge molecule at a tiny molar concentration even when its amide-"
            "proton pool is abundant). Report the exchangeable-proton-equivalent concentration "
            "instead (e.g. the mobile protein/peptide amide pool is typically reported around "
            "200-300 mM of amide-proton-equivalent concentration in tissue, not the protein's own "
            "molarity, with n_exchangeable_protons=1 per that equivalent unit; glycogen is reported "
            "as mM of glucosyl units -- roughly 200-400 mM in fed human liver, ~80-100 mM in muscle "
            "-- with 3 hydroxyl protons per unit, never as mM of glycogen molecules)."
        )

    duplicates = _find_likely_duplicates(report.metabolites)
    if duplicates:
        pairs = "; ".join(f"{a!r} and {b!r}" for a, b in duplicates)
        complaints.append(
            f"{pairs}: these have related names AND report the identical concentration and tissue "
            "compartment -- they look like the same underlying "
            "pool named twice, e.g. a CEST TECHNIQUE name ('glycoCEST', 'gagCEST', 'APT') used as "
            "if it were a separate metabolite from the actual molecule it targets ('glycogen', "
            "a glycosaminoglycan, mobile protein amides). Merge each duplicate pair into ONE "
            "entry, keeping the molecule's own name, not the technique's."
        )

    if not complaints:
        return None
    return "Fix these problems and re-emit the complete JSON:\n- " + "\n- ".join(complaints)


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 5}


def _names_related(a: str, b: str) -> bool:
    """Whether two candidate names plausibly describe the same thing: one contains the other
    ("Glycogen" in "GlycoCEST (Glycogen)"), or they share a substantive word."""
    na, nb = re.sub(r"[^a-z0-9]+", "", a.lower()), re.sub(r"[^a-z0-9]+", "", b.lower())
    if len(na) >= 4 and len(nb) >= 4 and (na in nb or nb in na):
        return True
    return bool(_name_tokens(a) & _name_tokens(b))


def _find_likely_duplicates(metabolites) -> list[tuple[str, str]]:
    """Pairs of candidates that share an identical (concentration, tissue_compartment) AND a
    related name. Observed in practice: "Glycogen" and "GlycoCEST (Glycogen)" reported at the
    same 310.0 mM in the same compartment, the second being the CEST technique applied to
    glycogen rather than a distinct molecule. The name condition exists because identical
    values alone are not enough: a run estimated both glutamine and alanine at a round 5 mM in
    the same compartment, and they are obviously different metabolites.
    """
    pairs = []
    for i, a in enumerate(metabolites):
        for b in metabolites[i + 1:]:
            same_value = (
                round(a.typical_concentration_mM, 3) == round(b.typical_concentration_mM, 3)
                and a.tissue_compartment.strip().lower() == b.tissue_compartment.strip().lower()
            )
            if same_value and _names_related(a.name, b.name):
                pairs.append((a.name, b.name))
    return pairs


# The solvent is not a metabolite: a run listed "water" (55.5 mM, itself off by a factor of a
# thousand) as a CEST candidate. Compared on the normalized name, not as a substring, so
# "water-soluble ..." descriptions are unaffected.
_SOLVENT_NAMES = {"water", "h2o", "bulkwater", "tissuewater", "freewater"}


def _is_solvent(name: str) -> bool:
    """'Water', 'H2O', and descriptive variants such as 'Water (bulk H2O pool -- CEST reference
    solvent)': the name's head (before any parenthesis/dash) is the solvent, or the whole
    squashed name is."""
    squashed = re.sub(r"[^a-z0-9]+", "", name.lower())
    head = re.sub(r"[^a-z0-9]+", "", re.split(r"[(;:\u2014-]", name.lower(), maxsplit=1)[0])
    return squashed in _SOLVENT_NAMES or head in _SOLVENT_NAMES


def _attach_reference_concentrations(report: LiteratureReport) -> None:
    """Attach curated reference-database concentrations to every metabolite, from HMDB's offline
    index, so the report carries a measured value with a citation next to the agent's estimate.

    Deliberately does NOT overwrite typical_concentration_mM. HMDB's quantified records are
    BIOFLUID measurements (blood/urine/CSF -- there is no liver biospecimen in it), and blood and
    tissue concentrations of the same metabolite routinely differ by an order of magnitude, so
    substituting one for the other would trade an unsourced number for a confidently wrong one.
    What it does instead is put the real measured value, its biospecimen and its PMID in front of
    the reader, so an estimate that contradicts the reference data is visible as such.
    """
    for m in report.metabolites:
        parts: list[str] = []

        # Solid tissue first -- it is what this pipeline actually reports, and BioNumbers records
        # frequently trace back to a reference work (e.g. Williamson & Brosnan's tissue metabolite
        # tables) rather than to a single study. Coverage is genuinely thin: most metabolites
        # return nothing, and a miss is reported as a miss rather than papered over with a blood
        # value. Organism is always carried, because the available tissue numbers are often rodent.
        try:
            tissue = reference_data.lookup_tissue_concentration(m.name, report.body_part)
        except Exception as exc:
            tissue = None
            parts.append(f"tissue lookup failed ({exc})")
        if tissue and tissue.get("found") and tissue.get("records"):
            rendered = "; ".join(
                f"{r['value']} in {report.body_part} [{r.get('organism') or 'organism unspecified'}]"
                f" (BNID {r['bnid']}"
                + (f", PMID {r['pubmed_id']}" if r.get("pubmed_id") else "")
                + (f", primary source: {r['primary_source'][:90]}" if r.get("primary_source") else "")
                + ")"
                for r in tissue["records"][:2]
            )
            parts.append(f"TISSUE (BioNumbers) — {rendered}")
        elif tissue is not None:
            parts.append(
                f"TISSUE — no curated {report.body_part} concentration found in BioNumbers for this "
                "metabolite; the tissue number in this report is therefore not reference-grounded"
            )

        try:
            fluid = reference_data.lookup_concentrations(m.name, m.aliases)
        except Exception as exc:  # a missing/corrupt index must not sink the run
            fluid = None
            parts.append(f"biofluid lookup skipped ({exc})")
        if fluid and fluid["found"] and fluid["records"]:
            blood = [r for r in fluid["records"] if "blood" in (r.get("biospecimen") or "").lower()]
            shown = (blood or fluid["records"])[:3]
            rendered = "; ".join(
                f"{r['biospecimen']} {r['value']} {r['units']}"
                + (f" (PMID {r['pubmed_id']})" if r.get("pubmed_id") else f" ({r['reference_text'][:60]})")
                for r in shown
            )
            parts.append(
                f"BIOFLUID (HMDB {fluid['accession']}, {fluid['matched_name']}, "
                f"{len(fluid['records'])} cited records) — {rendered}"
            )

        if parts:
            m.reference_concentrations = " | ".join(parts)


# A step that asserts a number must point at something. 'assumption' is allowed -- an honest
# "I am assuming this" is fine and is the whole point of asking -- but it gets surfaced rather
# than left to look like sourced fact.
_SOURCED_TYPES = {"reference_database", "reference_work", "journal_article"}
_NUMERIC = re.compile(r"\d")


def _flag_unsourced_reasoning(report: LiteratureReport) -> None:
    """Surface estimated concentrations whose derivation asserts numbers without attribution.

    The schema asks for per-step sources, but nothing stops a model from returning one vague step
    or from marking a hard number as an 'assumption'. This makes that visible in the report
    instead of letting a fluent paragraph pass as a sourced derivation.
    """
    for m in report.metabolites:
        if m.concentration_basis != "estimated":
            continue

        problems = []
        if not m.reasoning_steps:
            if m.concentration_reasoning:
                problems.append("derivation was given as free text with no per-step sources")
            else:
                problems.append("no derivation was given at all")
        else:
            unsourced = [
                s for s in m.reasoning_steps
                if _NUMERIC.search(f"{s.value} {s.statement}")
                and (s.source_type in _SOURCED_TYPES) and not s.source_detail.strip()
            ]
            assumed = [s for s in m.reasoning_steps
                        if s.source_type == "assumption" and _NUMERIC.search(f"{s.value} {s.statement}")]
            if unsourced:
                problems.append(
                    f"{len(unsourced)} step(s) claim a sourced type but give no identifier: "
                    + "; ".join(f"{s.statement[:70]!r}" for s in unsourced[:3])
                )
            if assumed:
                problems.append(
                    f"{len(assumed)} numeric step(s) rest on an unsourced assumption: "
                    + "; ".join(f"{s.statement[:70]!r}" for s in assumed[:3])
                )

        if problems:
            m.notes = f"{m.notes} [Unsourced reasoning] {'. '.join(problems)}.".strip()


def _verify_citations(report: LiteratureReport) -> None:
    """Never trust a cited PMID/title pair on the model's word alone: a tool-calling model can
    call search_pubmed correctly and still fabricate a plausible PMID/title when writing its
    final JSON (observed in practice with smaller local models -- see the
    cest-scout-llm-backend skill). Confirm each citation's title against PubMed's actual record;
    a citation that fails is dropped, and the concentration-evidence quote is cleared too if it
    pointed at a dropped citation, rather than silently keeping unverifiable "evidence".
    """
    pairs = [(c.pmid, c.title) for m in report.metabolites for c in m.citations]
    if not pairs:
        return
    try:
        verdicts = pubmed.verify_titles(pairs)
    except Exception as exc:  # network unavailable -- don't fail the whole run over this
        for m in report.metabolites:
            m.notes = f"{m.notes} [Citation verification skipped: {exc}]".strip()
        return

    for m in report.metabolites:
        kept, dropped = pubmed.drop_unverified_citations(m.citations, verdicts)
        m.citations = kept
        note = pubmed.format_drop_notes(dropped, "citations")
        dropped_pmids = {pmid for pmid, _, _ in dropped}
        if m.concentration_evidence_pmid in dropped_pmids:
            m.concentration_evidence_quote = ""
            m.concentration_evidence_pmid = ""
            note = f"{note} concentration_evidence_quote cleared because its source citation was dropped.".strip()
        if note:
            m.notes = f"{m.notes} [Citation verification] {note}".strip()


def _verify_quotes(report: LiteratureReport) -> None:
    """A model can cite a real, title-verified PMID and still invent the "verbatim" sentence
    itself -- citation-identity verification alone doesn't catch that. Check every surviving
    concentration_evidence_quote against its cited PMID's actual title/abstract text and clear it
    (downgrading to an honest "unverified" state rather than a false direct quote) if it doesn't
    match.
    """
    for m in report.metabolites:
        if not m.concentration_evidence_quote or not m.concentration_evidence_pmid:
            continue
        try:
            result = pubmed.verify_quote(m.concentration_evidence_pmid, m.concentration_evidence_quote)
        except Exception as exc:  # network unavailable -- don't fail the whole run over this
            m.notes = f"{m.notes} [Quote verification skipped: {exc}]".strip()
            continue
        if result["verified"]:
            continue
        m.notes = (
            f"{m.notes} [Quote verification] concentration_evidence_quote for PMID "
            f"{m.concentration_evidence_pmid} could not be confirmed ({result['reason']}) -- cleared "
            "as unverified; the model claimed concentration_basis='direct_literature_quote' but no "
            "matching text was actually found, so treat typical_concentration_mM as unverified/"
            "estimated, not a confirmed literature value."
        ).strip()
        m.concentration_evidence_quote = ""
        m.concentration_evidence_pmid = ""
