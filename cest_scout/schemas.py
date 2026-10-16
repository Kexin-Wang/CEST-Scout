"""Structured-output schemas exchanged between the four agents."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Citation(BaseModel):
    pmid: str = Field(description="PubMed ID")
    title: str
    year: int | None = None
    species: str = Field(
        description="The specific organism(s) studied, as precisely as the source supports, e.g. "
        "'human', 'Sprague-Dawley rat', 'C57BL/6 mouse', 'New Zealand White rabbit'. Never just "
        "'animal' -- use the MeSH species heading / abstract wording to be concrete."
    )
    n_subjects: int | None = Field(
        default=None, description="Number of human subjects or animals used in the study, if stated."
    )
    study_setting: str = Field(
        description="One of 'human in vivo', 'animal in vivo', 'ex vivo tissue/phantom', "
        "'in vitro/solution phantom', or 'unspecified' -- how the measurement was actually made."
    )


class ReasoningStep(BaseModel):
    """One step of a concentration derivation, with its source attached to the number itself.

    Exists because a free-text rationale lets a model state "liver glucose is about 5 mM" with no
    indication of where 5 came from. Forcing per-step attribution makes an unsourced number
    visible as an explicit assumption rather than letting it hide inside a fluent paragraph.
    """
    statement: str = Field(description="The step, e.g. 'whole-blood glucose in fasted adults is 4.9 mM'.")
    value: str = Field(default="", description="The quantity asserted in this step, with units, if any.")
    source_type: str = Field(
        description="'reference_database' (HMDB/BioNumbers/PubChem, with the record id), "
        "'reference_work' (a named textbook/handbook such as Geigy Scientific Tables), "
        "'journal_article' (a PubMed-indexed paper), "
        "'assumption' (no source -- you are asserting it; say so plainly), or "
        "'calculation' (derived arithmetically from previous steps)."
    )
    source_detail: str = Field(
        default="", description="The actual identifier: a BNID, an HMDB accession, a PMID, or the "
        "reference work's title and table/page. Leave blank ONLY when source_type='assumption' or "
        "'calculation'."
    )


class CandidateMetabolite(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    typical_concentration_mM: float = Field(description="Best single representative estimate, mM")
    concentration_range_mM: str = Field(description="e.g. '1-3 mM', as reported across sources")
    tissue_compartment: str = Field(description="e.g. 'intracellular, gray matter'")
    animal_evidence_only: bool = Field(
        description="True if no cited human evidence was found and support rests on animal studies."
    )
    citations: list[Citation]
    concentration_evidence_quote: str = Field(
        default="", description="The exact sentence or table row/cell (verbatim, quoted) from the "
        "source article that states the concentration or concentration range -- copy it directly, "
        "do not paraphrase. This is what gets highlighted next to the concentration in the report. "
        "Leave blank rather than inventing one -- see concentration_basis."
    )
    concentration_evidence_pmid: str = Field(
        default="", description="The PMID (from citations) that concentration_evidence_quote was taken from."
    )
    concentration_basis: str = Field(
        default="unknown", description="'direct_literature_quote' ONLY if concentration_evidence_quote "
        "is a real verbatim excerpt you copied from the abstract/data search_pubmed returned -- it will "
        "be checked against the actual cited text and cleared if it doesn't match. 'estimated' if no "
        "direct literature number was found and typical_concentration_mM is instead inferred -- in that "
        "case concentration_reasoning is REQUIRED. Never claim 'direct_literature_quote' to make an "
        "estimate look more solid than it is."
    )
    concentration_reasoning: str = Field(
        default="", description="Required when concentration_basis='estimated': the explicit "
        "calculation/reasoning chain used to arrive at the estimate (e.g. unit conversion from a "
        "related reported value, order-of-magnitude analogy to a comparable tissue/species, a cited "
        "textbook physiological range) -- must not be blank in that case. State plainly that this is "
        "an estimate, not a measured value."
    )
    reasoning_steps: list[ReasoningStep] = Field(
        default_factory=list,
        description="Required when concentration_basis='estimated': the derivation broken into "
        "steps, with a source attached to EVERY step that states a number. A narrative paragraph "
        "with unattributed numbers in it is not acceptable -- each quantity you assert must carry "
        "the reference it came from, or be explicitly marked as an assumption.",
    )
    # Populated deterministically by literature_agent from the curated HMDB index -- a measured
    # value with its citation, shown ALONGSIDE the agent's estimate rather than replacing it,
    # because HMDB quantifies biofluids and the pipeline reports tissue.
    reference_concentrations: str = ""
    notes: str = ""


class LiteratureReport(BaseModel):
    body_part: str
    metabolites: list[CandidateMetabolite]
    search_summary: str = Field(description="Short account of the search strategy and queries used.")
    # Filled deterministically after the agent's output: candidates removed because they are not
    # stored in the tissue (constituents of a flowing or secreted fluid), each with the reason.
    excluded: list[str] = Field(default_factory=list)


class FrequencyTableRow(BaseModel):
    field_strength_T: float
    larmor_frequency_MHz: float
    offset_from_water_Hz: float


class ExchangeableProton(BaseModel):
    group: str = Field(description="Functional group, e.g. 'amide NH', 'amine NH2', 'hydroxyl OH'")
    group_class: str = Field(
        default="",
        description="EXACTLY one of: 'amide', 'guanidinium', 'amine', 'hydroxyl', 'imidazole', 'thiol', "
                    "'phosphate', 'rnoe', 'other'. This class, not your ppm/exchange estimates, selects the exchange "
                    "rate and chemical shift the simulation uses from a cited reference table. 'rnoe' is for "
                    "NON-exchangeable aliphatic/aromatic C-H protons of mobile macromolecules (lipids, proteins, "
                    "glycogen, GAG) whose saturation reaches water through the relayed nuclear Overhauser effect, "
                    "observed UPFIELD of water (negative ppm, e.g. -3.5 or -1.6 ppm).",
    )
    n_protons: int
    ppm_shift_estimate: float = Field(description="Chemical shift vs. bulk water, ppm (literature/knowledge based); "
                                                  "negative for rNOE pools upfield of water")
    exchange_regime: str = Field(description="'slow', 'intermediate', or 'fast' exchange, with brief justification; "
                                             "'rNOE (dipolar, relayed)' for an rnoe group")
    is_exchangeable: bool = Field(description="True if the protons exchange chemically with water. False for an "
                                              "rnoe group (they do not exchange; they transfer saturation by "
                                              "cross-relaxation) -- such a group is still simulated when the "
                                              "reference table has a verified rNOE transfer rate.")
    reasoning: str
    # Populated deterministically by cest_scout after parsing (from ppm_shift_estimate via
    # frequency_calc), not trusted from the model's own JSON -- see chemist_agent.run().
    frequency_table: list[FrequencyTableRow] = Field(default_factory=list)


class ChemistFinding(BaseModel):
    metabolite_name: str
    smiles: str | None = None
    structure_description: str
    exchangeable_protons: list[ExchangeableProton]
    overall_argument: str = Field(description="The chemist's case for why/whether this metabolite is CEST-visible.")
    # Populated deterministically by cest_scout after parsing (see chemist_agent.run()'s
    # verification of `smiles` against PubChem's real structure for this name), not trusted from
    # the model's own JSON. Empty means either verification confirmed a match, or the model gave
    # no smiles at all.
    structure_verification_note: str = ""


class ChemistReport(BaseModel):
    findings: list[ChemistFinding]


class SequenceRecommendation(BaseModel):
    field_strength_T: float
    b1_uT: float
    saturation_time_ms: float
    duty_cycle_or_pulses: str
    rationale: str
    # Everything below is populated deterministically by pipeline.py from a Bloch-McConnell
    # B1 x Tsat sweep (bloch_bridge.optimize_saturation) -- b1_uT and saturation_time_ms above are
    # OVERWRITTEN with the sweep's answer, because a model asked to name an "optimized" B1/Tsat
    # reliably just emits its favourite constant (measured: Tsat=2000 ms in 84% of recommendations
    # across every run) rather than anything optimized for the metabolite in front of it.
    derived_from: str = Field(
        default="llm_assertion",
        description="'bloch_mcconnell_sweep' once the deterministic optimizer has replaced the "
                    "model's numbers; 'llm_assertion' means the sweep could not run for this entry.",
    )
    contrast_at_optimum_percent: float = Field(
        default=0.0, description="What the recommended Tsat achieves; equal to "
                    "max_achievable_contrast_percent (the recommended Tsat is the grid optimum).",
    )
    max_achievable_contrast_percent: float = Field(
        default=0.0, description="The true contrast ceiling at this B1, over every Tsat on the "
                    "grid -- what pipeline.py compares against CONTRAST_THRESHOLD_PERCENT.",
    )
    # Legacy fields from a version that capped B1 at an assumed "SAR ceiling"; no SAR model is
    # applied any more (bloch_bridge.NO_SAR_MODEL_NOTE), so new runs leave these at their defaults.
    sar_b1_ceiling_uT: float = 0.0
    sar_limited: bool = False
    unconstrained_b1_uT: float = 0.0
    unconstrained_contrast_percent: float = 0.0
    fraction_of_achievable_contrast_retained: float = 1.0
    # The sweep surface this recommendation was chosen from, kept so the report can PLOT the
    # choice rather than just assert it -- a reader can then see whether the peak is sharp or
    # flat, and how much contrast the SAR cap costs. Keyed "b1|tsat" because JSON keys are strings.
    sweep_b1_grid_uT: list[float] = Field(default_factory=list)
    sweep_tsat_grid_ms: list[float] = Field(default_factory=list)
    sweep_contrast: dict[str, float] = Field(default_factory=dict)
    # Structured pulse-train timing, used to draw an explicit saturation-scheme diagram (see
    # cest_scout/report/sequence_plots.py) rather than just describing it in prose. Default 0
    # means "not specified"; renderers fall back to treating the whole saturation_time_ms as one
    # continuous pulse rather than crashing on a zero-duration pulse.
    n_pulses: int = Field(default=1, description="Number of discrete saturation pulses; 1 means effectively continuous-wave.")
    pulse_duration_ms: float = Field(default=0.0, description="Duration of each individual saturation pulse, ms.")
    interpulse_delay_ms: float = Field(default=0.0, description="Gap between consecutive pulses, ms.")


class PhysicistVerdict(BaseModel):
    metabolite_name: str
    verified_by_mrs_or_cest_literature: bool
    verifying_citations: list[Citation]
    simulated_contrast_percent: dict[str, float] = Field(
        description="field strength (as string, e.g. '3.0') -> the with-vs-without-metabolite Z-spectrum "
        "difference (%) from the multi-pool (water+MT+metabolite+background) Bloch-McConnell simulation. "
        "This, not any MTRasym number quoted in the literature, is the authoritative detectability signal."
    )
    critique_of_biologist: str = ""
    critique_of_chemist: str = ""
    in_vivo_frequency_note: str = Field(
        default="", description="Any documented in-vivo shift vs. the chemist's in-vitro/theoretical estimate."
    )
    neglected_exchangeable_protons: str = ""
    frequency_collision_analysis: str = Field(
        default="", description="Critical assessment of whether this metabolite's exchangeable-proton "
        "offset(s) risk collapsing into another candidate metabolite's offset(s) at the field strengths "
        "considered (accounting for exchange-broadened linewidth, not just the bare ppm gap), and whether "
        "the metabolite itself has multiple exchangeable-proton groups at distinct offsets that could be "
        "confused with separate metabolites. Ground this in the overlap diagnostics returned by the "
        "simulation tool, don't just eyeball ppm values."
    )
    direct_water_saturation_note: str = Field(
        default="", description="Whether the proposed B1 at each field strength would itself directly "
        "saturate bulk water enough to spill into/mask the metabolite's exchange-mediated contrast."
    )
    b1_tsat_extraction_method_note: str = Field(
        default="", description="How the optimized B1/Tsat choice relates to the CEST quantification/"
        "extraction method used to pull the effect out of the Z-spectrum (e.g. simple MTRasym, "
        "Lorentzian-difference/multi-pool fitting, AREX, omega-plot exchange-rate fitting) -- different "
        "extraction methods tolerate or require different B1/Tsat, cite the literature that establishes this."
    )
    extraction_method_citations: list[Citation] = Field(default_factory=list)
    ex_vivo_in_vivo_exchange_caveat: str = Field(
        default="", description="If any supporting citation (this agent's or upstream agents') is an ex "
        "vivo tissue/phantom or in vitro/solution study, note explicitly that the measured exchange rate "
        "there may not equal the in vivo rate (pH, temperature, protein binding, metabolite concentration "
        "in vivo all shift k_ex), and how that affects confidence in the simulated contrast."
    )
    sequence_recommendations: list[SequenceRecommendation]
    final_confidence: str = Field(description="'high', 'moderate', or 'low', with justification folded into critique fields")
    # Populated deterministically by cest_scout after parsing (see physicist_agent.run()'s
    # citation verification against real PubMed titles), not trusted from the model's own JSON.
    citation_verification_note: str = ""
    # Populated deterministically by pipeline.py when the agent's own simulate calls didn't cover
    # every field strength in FIELD_STRENGTHS_T, so a missing field strength reads as "not
    # simulated" rather than silently as "no contrast".
    simulation_coverage_note: str = ""
    # Populated deterministically by pipeline.py's B1/Tsat sweep when the in vivo SAR ceiling,
    # rather than the metabolite's exchange physics, is what limits achievable contrast.
    saturation_optimizer_note: str = ""


class PhysicistReport(BaseModel):
    verdicts: list[PhysicistVerdict]


class ZSpectrumCurve(BaseModel):
    """One field strength's simulated Z-spectra, read back verbatim from the physicist's own
    tool calls (see pipeline._apply_authoritative_contrast) -- never regenerated from the LLM's
    JSON, since the model can call the tool correctly and still misreport numbers.
    """
    field_strength_T: float
    offsets_ppm: list[float]
    z_with_metabolite: list[float]
    z_without_metabolite: list[float]
    contrast_diff_percent: float
    # The drop in Z at the contrast offset split into what water alone loses (direct saturation),
    # what the tissue's semisolid MT pool and background pools add, and the metabolite's own
    # contrast -- so a report shows the background the tissue imposes, not only the difference.
    direct_saturation_percent: float | None = None
    mt_background_percent: float | None = None
    mt_fraction_of_water: float | None = None
    overlap_warnings: list[str] = Field(default_factory=list)
    # The pool the physicist agent actually simulated, copied from its tool-call arguments. Kept
    # so a report (and a cross-model comparison) can show when the simulated concentration
    # disagrees with the literature agent's number for the same metabolite.
    simulated_concentration_mM: float = 0.0
    n_exchangeable_protons: int = 0
    exchange_rate_per_s: float = 0.0
    shift_ppm: float = 0.0


class SimulationParameters(BaseModel):
    """The pool actually simulated for one exchangeable-proton group, with where every number came
    from. Filled deterministically by pipeline.run_reference_simulations from the chemist's group
    assignment, the literature agent's concentration and the cited reference table -- never from
    a model's own k_ex/shift guess."""
    metabolite_name: str
    group_class: str
    group_text: str = Field(default="", description="The chemist's own wording for this group.")
    n_exchangeable_protons: int = 0
    concentration_mM: float = 0.0
    shift_ppm: float | None = None
    # The chemist's own ppm estimate for this group, kept only so a report can show it next to the
    # reference shift that was simulated; it never enters the simulation.
    chemist_shift_ppm: float | None = None
    kex_per_s: float | None = None
    status: str = Field(
        description="'simulated', or why not: 'unknown_group_class', 'no_reference_for_group', "
                    "'not_exchangeable', 'no_concentration'."
    )
    mechanism: str = Field(
        default="chemical exchange",
        description="'chemical exchange' (downfield, positive ppm) or 'rNOE' (relayed nuclear Overhauser effect, "
                    "upfield, negative ppm). Set from the group class; rNOE pools are simulated with the same "
                    "two-pool transfer physics using the reference table's effective transfer rate.",
    )
    provenance: str = Field(
        default="",
        description="Plain statement of the measurement the k_ex/shift were taken from (tissue, species, "
                    "in vivo/phantom, pH/temperature, PMIDs) and whether it is borrowed from another tissue.",
    )
    borrowed_from_other_tissue: bool = False
    source_pmids: list[str] = Field(default_factory=list)
    contrast_percent_by_field: dict[str, float] = Field(default_factory=dict)
    # Uncertainty inputs and their effect. The literature agent's concentration_range_mM is parsed
    # into a low/high bound and the reference tier's k_ex spread is taken from the table; the
    # simulation is repeated at every corner of that box at the swept B1/Tsat, and the min/max
    # contrast per field strength is kept here. None / {} when no range was available.
    concentration_low_mM: float | None = None
    concentration_high_mM: float | None = None
    kex_low_per_s: float | None = None
    kex_high_per_s: float | None = None
    contrast_range_percent_by_field: dict[str, list[float]] = Field(default_factory=dict)


class ClinicalStudy(BaseModel):
    """One published application of this metabolite's CEST/NOE signal to a disease, as stated in
    the PubMed abstract. Numbers are copied only when they appear in `quote`; the pipeline nulls
    any that do not."""
    pmid: str
    title: str
    year: str = ""
    evidence_type: str = Field(
        default="cest_application",
        description="'cest_application' when the study measured this pool's CEST/NOE signal in the disease; "
                    "'metabolite_change' when it showed the metabolite itself changes in the disease by any other "
                    "method (MRS, metabolomics, histology, biochemistry, a mechanistic or animal model).",
    )
    measurement: str = Field(default="", description="For metabolite_change evidence: the method and quantity, e.g. '1H-MRS glutamate (mM)', "
                                                     "'LC-MS tissue lactate', 'immunoassay creatine kinase'.")
    disease: str = Field(description="The disease or condition studied, as the abstract names it.")
    disease_category: str = Field(
        default="other",
        description="EXACTLY one of: tumor, stroke, neurodegeneration, epilepsy, psychiatric, metabolic, "
                    "musculoskeletal, cardiovascular, inflammation, other.",
    )
    cest_metric: str = Field(default="", description="e.g. 'MTRasym at 3.5 ppm', 'APTw signal', 'NOE(-3.5 ppm)', 'gluCEST contrast'.")
    species: str = Field(default="", description="'human', 'rat', 'mouse', ... as stated.")
    study_setting: str = Field(default="", description="'human in vivo', 'animal in vivo', 'ex vivo tissue', 'phantom'.")
    n_disease: int | None = Field(default=None, description="Subjects or animals in the disease group, if stated in the abstract.")
    n_control: int | None = Field(default=None, description="Controls (or contralateral/normal-appearing regions), if stated.")
    disease_mean: float | None = Field(default=None, description="Group mean of the CEST metric in disease, if stated.")
    disease_sd: float | None = None
    control_mean: float | None = None
    control_sd: float | None = None
    spread_is_sem: bool = Field(default=False, description="True if the +/- values in the abstract are SEM rather than SD.")
    unit: str = Field(default="%", description="Unit of the metric as stated, usually '%'.")
    direction: str = Field(default="not stated", description="'higher in disease', 'lower in disease', 'no difference', 'not stated'.")
    p_value_text: str = Field(default="", description="The p value as written, e.g. 'p < 0.001', or ''.")
    field_strength_T: float | None = None
    quote: str = Field(description="VERBATIM sentence(s) from the abstract carrying the result and the numbers.")
    note: str = Field(default="", description="One short caveat about the comparison, e.g. 'median, not mean +/- SD', "
                                               "'contralateral region as control', or ''.")
    # Set by the pipeline's deterministic checks; whatever the model writes here is overwritten.
    title_verified: bool = False
    quote_verified: bool = False
    numbers_note: str = ""
    stats_source: str = "abstract"          # or "full text (PMC...)" when missing statistics were filled from PMC
    fulltext_quote: str = ""                # the verbatim full-text passage those statistics came from
    fulltext_note: str = ""                 # why full text could not be used, if it could not


class PooledEffect(BaseModel):
    evidence_type: str = "cest_application"
    disease_category: str
    n_studies: int
    n_disease: int
    n_control: int
    hedges_g: float
    ci_low: float
    ci_high: float
    i2_percent: float
    direction: str
    pmids: list[str] = Field(default_factory=list)
    note: str = ""


class ClinicalEvidence(BaseModel):
    metabolite_name: str
    pool: str = Field(default="", description="The CEST/NOE signal used clinically, e.g. 'amide (APT, 3.5 ppm)', 'rNOE (-3.5 ppm)', 'guanidinium (CrCEST, 2 ppm)'.")
    studies: list[ClinicalStudy] = Field(default_factory=list)
    summary: str = Field(default="", description="At most 80 words: which diseases, what the signal did, how strong the evidence is.")
    search_note: str = Field(default="", description="What was searched and what was not found, in one or two sentences.")
    # Computed deterministically after verification; the model's values are overwritten.
    n_human_studies: int = 0
    n_animal_studies: int = 0
    n_humans: int = 0
    n_animals: int = 0
    n_cest_studies: int = 0
    n_change_studies: int = 0
    n_studies_dropped: int = 0
    pooled_effects: list[PooledEffect] = Field(default_factory=list)
    verification_note: str = ""


class ClinicalReport(BaseModel):
    entries: list[ClinicalEvidence]


class FinalMetabolite(BaseModel):
    name: str
    typical_concentration_mM: float
    concentration_range_mM: str
    animal_evidence_only: bool
    concentration_evidence_quote: str = ""
    concentration_evidence_pmid: str = ""
    concentration_basis: str = "unknown"
    concentration_reasoning: str = ""
    reasoning_steps: list[ReasoningStep] = Field(default_factory=list)
    reference_concentrations: str = ""
    detectable_by_cest: bool
    # Three-way verdict, because 'not detectable' and 'we could not simulate it' are different
    # findings: 'detectable', 'not detectable', or 'unknown' (no group had a verified reference
    # rate, so nothing was simulated and no contrast can be claimed either way). detectable_by_cest
    # stays False for 'unknown' so older readers keep working; new readers use this field.
    detectability: str = ""
    # Which saturation-transfer mechanism produced the detectable contrast: 'chemical exchange'
    # (downfield, +ppm), 'rNOE' (upfield, -ppm), 'both', or '' when nothing was detectable.
    detection_mechanism: str = ""
    field_strengths_recommended_T: list[float]
    best_sequence_by_field: list[SequenceRecommendation]
    exchangeable_protons_summary: str
    simulated_contrast_percent: dict[str, float]
    zspectra_by_field: list[ZSpectrumCurve] = Field(default_factory=list)
    smiles: str | None = None
    structure_verification_note: str = ""
    exchangeable_protons_detail: list[ExchangeableProton] = Field(default_factory=list)
    frequency_collision_analysis: str = ""
    direct_water_saturation_note: str = ""
    b1_tsat_extraction_method_note: str = ""
    extraction_method_citations: list[Citation] = Field(default_factory=list)
    ex_vivo_in_vivo_exchange_caveat: str = ""
    key_citations: list[Citation]
    caveats: str
    simulation_parameters: list[SimulationParameters] = Field(default_factory=list)
    clinical_evidence: ClinicalEvidence | None = None
    # How much of the detectability verdict survives the stated uncertainty in its inputs:
    # 'robust' (the LOW corner of the concentration x k_ex box still clears the threshold at some
    # field), 'fragile' (only the point estimate does), 'upper-bound only' (only the high corner
    # does), 'no', or 'unknown' (no range information to test with). Filled by pipeline._merge.
    detectability_robustness: str = "unknown"
    contrast_range_percent_by_field: dict[str, list[float]] = Field(default_factory=dict)
    robustness_note: str = ""


class ScanProtocol(BaseModel):
    field_strength_T: float = Field(
        description="Clinical field strength for this scan, Tesla. Overwritten deterministically "
        "to the project's fixed clinical field strength -- see "
        "experimentalist_agent.SCAN_FIELD_STRENGTH_T -- not left to the model."
    )
    pulse_shape: str = Field(description="RF saturation pulse train shape, e.g. 'pulsed Gaussian train' or 'continuous wave (CW)'.")
    b1_uT: float = Field(description="Single compromise saturation B1 amplitude (uT) chosen to work across the targeted metabolites.")
    n_pulses: int
    pulse_duration_ms: float
    interpulse_delay_ms: float
    duty_cycle_percent: float
    total_saturation_time_ms: float
    tr_ms: float
    sar_rationale: str = Field(description="Why this B1/duty-cycle/Tsat is SAR-feasible for this anatomy at this field strength.")
    compromise_rationale: str = Field(
        description="Why this single B1/Tsat was chosen given the different individually-optimal "
        "values the physicist agent recommended per metabolite -- which metabolites it favors and "
        "which it undersaturates/oversaturates as a result."
    )


class ZSpectrumOffset(BaseModel):
    offset_ppm: float
    targets: list[str] = Field(
        description="What this offset is for: metabolite name(s), 'water (0 ppm)', 'symmetric "
        "reference', or 'B0-correction (WASSR) reference point'."
    )


class RoiDefinition(BaseModel):
    """One region of interest of the verification scan. The agent chooses the set for the tissue
    at hand; the pipeline requires the target tissue to be among them."""
    name: str = Field(description="Short anatomical name, used verbatim as organ_predictions[*].organ.")
    anatomical_definition: str = Field(description="Where exactly the ROI is drawn on the prescribed slice.")
    is_target_tissue: bool = Field(description="True for the ROI(s) inside the tissue this whole report is about.")
    role: str = Field(default="", description="Why this ROI is in the set: target, comparison tissue with a "
                                              "known different concentration, reference for B0/B1, ...")


class OrganMetabolitePrediction(BaseModel):
    organ: str = Field(description="One ROI name from this plan's roi_set, copied verbatim.")
    metabolite_name: str
    organ_concentration_mM: float = Field(
        description="This organ's literature-supported concentration of the metabolite, used for "
        "the simulation below -- may differ from the concentration Agent 1 reported for the "
        "originally-researched tissue."
    )
    concentration_rationale: str
    citations: list[Citation] = Field(default_factory=list)
    # Populated deterministically by pipeline.py: a Bloch-McConnell simulation of this
    # (organ, metabolite) pair at organ_concentration_mM, with the organ's measured water T1/T2/MT
    # parameters and the reference-table exchange rate/shift, under the chosen compromise
    # scan_protocol -- not trusted from the model's own JSON.
    predicted_contrast_percent: float = 0.0
    simulation_note: str = Field(default="", description="What was simulated, or why nothing was.")
    # The same simulation repeated at (1 - f) x B1 and (1 + f) x B1 (f = bloch_bridge.
    # B1_INHOMOGENEITY_FRACTION), i.e. the contrast a voxel with an uncalibrated transmit field
    # would show. Filled deterministically by pipeline.py; [] when nothing was simulated.
    predicted_contrast_range_percent: list[float] = Field(default_factory=list)
    b1_scale_range: list[float] = Field(default_factory=list)


class InVivoVerificationPlan(BaseModel):
    scan_rationale: str = Field(
        description="Why THIS scan (plane, slice, ROI set) can distinguish real metabolite-specific "
        "contrast from generic background (MT/NOE) effects, by comparing Z-spectra across ROIs with "
        "different known metabolite concentrations."
    )
    imaging_plane: str = Field(
        default="", description="sagittal / axial / coronal / oblique, and what the slice or slab is centred on."
    )
    roi_set: list[RoiDefinition] = Field(
        default_factory=list,
        description="3-5 ROIs chosen for this tissue: the target tissue itself (mandatory) plus comparison "
                    "tissues in the same field of view with literature-known different concentrations.",
    )
    scan_protocol: ScanProtocol
    zspectrum_offsets: list[ZSpectrumOffset]
    fat_suppression: str
    motion_mitigation: str
    slice_coverage: str = Field(description="The slice/slab prescription: how it is positioned so that it cuts "
                                            "through the target tissue and still contains every ROI.")
    organs_in_fov: list[str] = Field(description="Anatomical structures inside the prescribed slice/slab.")
    organ_predictions: list[OrganMetabolitePrediction]
    differential_analysis_plan: str = Field(
        description="Concrete post-scan analysis: how ROI-averaged Z-spectra/MTRasym or "
        "Lorentzian-difference curves from each organ are compared at each metabolite's offset to "
        "justify which metabolite explains an observed organ-to-organ difference."
    )
    excluded_metabolites_note: str = Field(
        default="", description="Which candidate metabolites were not actively targeted in this "
        "scan design and why (e.g. no detectable simulated contrast at any field strength)."
    )
    caveats: str


class AgentRunStats(BaseModel):
    """What one agent stage actually cost and which model produced it. Filled by pipeline.py from
    the counters llm.run_research_agent keeps, so a report can be compared against another
    model's run of the same tissue on equal terms.
    """
    agent: str
    model: str
    endpoint: str = Field(description="Host:port of the OpenAI-compatible endpoint used; never the key.")
    thinking_disabled: bool = False
    elapsed_s: float = 0.0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    tool_error_samples: list[str] = Field(
        default_factory=list, description="Up to five of the tool error messages, to show HOW a model misused a tool."
    )
    nudges: int = Field(default=0, description="Times the coverage check sent the model back to work after an early stop.")
    json_repairs: int = Field(default=0, description="Structured-output attempts that were not valid JSON for the schema.")
    output_retries: int = Field(default=0, description="Schema-valid outputs rejected by the agent's completeness check.")
    output_check_passed: bool | None = Field(
        default=None,
        description="True if the final output satisfied the agent's completeness check, False if the "
                    "retries ran out and an incomplete output was kept, None if the agent has no check.",
    )


class RunProvenance(BaseModel):
    started_at_utc: str
    finished_at_utc: str
    elapsed_s: float
    code_version: str = Field(default="", description="git commit of cest_scout at run time, if available.")
    reference_table: str = Field(
        default="", description="Build timestamp and size of the exchange-rate reference table the simulation used."
    )
    resimulated_at_utc: str = Field(
        default="", description="Set when the deterministic stage was re-run offline on this report's saved agent "
                                "outputs with a newer reference table (cest_scout.resimulate)."
    )
    stages: list[AgentRunStats] = Field(default_factory=list)
    # The deterministic settings that shaped this run's numbers (threshold, sweep grids, SAR
    # anchors, saturation/readout model, B1-inhomogeneity fraction, sampling), so two runs can be
    # checked for comparability from their JSON alone.
    settings: dict = Field(default_factory=dict)


class TissueParameterRow(BaseModel):
    """One water/MT parameter value the simulation used for one tissue (the target tissue at each
    field strength, or a verification-scan ROI at the scan field), with where it came from."""
    tissue: str
    role: str = Field(default="", description="'target tissue' or 'verification ROI'.")
    field_T: float
    parameter: str = Field(description="water T1 / water T2 / MT fraction / MT k_ex / MT T2b")
    value: str = Field(description="Value with unit, as used by the simulator.")
    source: str = Field(default="", description="PMID(s) and setting, 'not abstract-verified' where a table value, "
                                                "or 'generic default'.")
    verified: str = Field(default="", description="'abstract' / 'table' / 'default'")


class FinalReport(BaseModel):
    body_part: str
    metabolites: list[FinalMetabolite]
    in_vivo_verification_plan: InVivoVerificationPlan | None = None
    provenance: RunProvenance | None = None
    tissue_parameters_note: str = Field(
        default="", description="Where the target tissue's water T1/T2 and MT parameters used in the "
                                "simulations came from (cited table entry, or the stated generic defaults)."
    )
    tissue_parameters: list[TissueParameterRow] = Field(
        default_factory=list, description="The same information as a table: every water/MT parameter used, per "
                                          "tissue and field strength, with its source and verification level.",
    )
    excluded_candidates: list[str] = Field(
        default_factory=list, description="Candidates the literature stage excluded before simulation, with reasons "
                                          "(e.g. a constituent of a secreted fluid, not stored in the tissue).",
    )
