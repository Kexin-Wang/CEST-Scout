from cest_scout.pipeline import _merge, render_markdown
from cest_scout.schemas import (
    CandidateMetabolite,
    ChemistFinding,
    ChemistReport,
    Citation,
    ExchangeableProton,
    InVivoVerificationPlan,
    LiteratureReport,
    OrganMetabolitePrediction,
    PhysicistReport,
    PhysicistVerdict,
    ScanProtocol,
    SequenceRecommendation,
    ZSpectrumOffset,
)


def _build_reports():
    citation = Citation(
        pmid="12345", title="Creatine in brain", year=2010,
        species="human", n_subjects=12, study_setting="human in vivo",
    )
    lit = LiteratureReport(
        body_part="brain",
        metabolites=[
            CandidateMetabolite(
                name="creatine", aliases=["Cr"], typical_concentration_mM=8.0,
                concentration_range_mM="5-10 mM", tissue_compartment="gray matter",
                animal_evidence_only=False, citations=[citation],
                concentration_evidence_quote="Creatine concentration was 8.0 +/- 1.2 mM.",
                concentration_evidence_pmid="12345",
            )
        ],
        search_summary="test",
    )
    chem = ChemistReport(findings=[
        ChemistFinding(
            metabolite_name="creatine", smiles="NC(=N)N(C)CC(=O)O",
            structure_description="guanidino + carboxyl", exchangeable_protons=[
                ExchangeableProton(group="guanidinium NH2", n_protons=2, ppm_shift_estimate=2.0,
                                    exchange_regime="intermediate", is_exchangeable=True, reasoning="test"),
            ],
            overall_argument="Likely visible via guanidinium CEST.",
        )
    ])
    phys = PhysicistReport(verdicts=[
        PhysicistVerdict(
            metabolite_name="creatine", verified_by_mrs_or_cest_literature=True,
            verifying_citations=[citation],
            simulated_contrast_percent={"1.5": 0.2, "3.0": 0.8, "7.0": 1.5},
            critique_of_biologist="", critique_of_chemist="Reasonable.",
            in_vivo_frequency_note="", neglected_exchangeable_protons="",
            frequency_collision_analysis="No other candidate within 0.3 ppm at any field.",
            direct_water_saturation_note="B1 chosen keeps direct saturation bandwidth well below the offset.",
            b1_tsat_extraction_method_note="Lorentzian-difference fitting supports this B1/Tsat.",
            extraction_method_citations=[citation],
            ex_vivo_in_vivo_exchange_caveat="",
            sequence_recommendations=[
                SequenceRecommendation(field_strength_T=3.0, b1_uT=1.5, saturation_time_ms=1500,
                                        duty_cycle_or_pulses="continuous", rationale="test"),
            ],
            final_confidence="high",
        )
    ])
    return lit, chem, phys


def _build_verification_plan():
    return InVivoVerificationPlan(
        scan_rationale="Comparing Z-spectra across organs with different creatine concentrations "
                       "distinguishes metabolite-specific contrast from generic MT/NOE background.",
        scan_protocol=ScanProtocol(
            field_strength_T=3.0, pulse_shape="pulsed Gaussian train", b1_uT=1.5, n_pulses=20,
            pulse_duration_ms=80, interpulse_delay_ms=20, duty_cycle_percent=80,
            total_saturation_time_ms=2000, tr_ms=4000,
            sar_rationale="Pulsed train keeps whole-body SAR within limits at 3 T.",
            compromise_rationale="1.5 uT favors creatine's intermediate exchange regime.",
        ),
        zspectrum_offsets=[ZSpectrumOffset(offset_ppm=2.0, targets=["creatine"])],
        fat_suppression="SPAIR fat saturation prepulse.",
        motion_mitigation="Respiratory-triggered acquisition.",
        slice_coverage="Axial slab at the liver hilum level.",
        organs_in_fov=["liver", "renal cortex (kidney)", "spleen", "paraspinal muscle"],
        organ_predictions=[
            OrganMetabolitePrediction(
                organ="paraspinal muscle", metabolite_name="creatine", organ_concentration_mM=30.0,
                concentration_rationale="Muscle creatine is much higher than liver.",
                citations=[Citation(pmid="99999", title="Muscle creatine content", species="human",
                                     study_setting="human in vivo")],
                predicted_contrast_percent=2.5,
            ),
            OrganMetabolitePrediction(
                organ="liver", metabolite_name="creatine", organ_concentration_mM=8.0,
                concentration_rationale="Liver creatine is comparatively low.",
                predicted_contrast_percent=0.8,
            ),
        ],
        differential_analysis_plan="Compare ROI-averaged MTRasym at 2.0 ppm between muscle and liver.",
        caveats="One compromise B1 cannot be optimal for every metabolite's exchange regime.",
    )


def test_merge_marks_detectable_fields_above_threshold():
    lit, chem, phys = _build_reports()
    final = _merge(lit, chem, phys, {})
    assert len(final.metabolites) == 1
    m = final.metabolites[0]
    assert m.detectable_by_cest is True
    assert m.field_strengths_recommended_T == [3.0, 7.0]  # 1.5T (0.2%) below the 0.5% threshold
    assert m.typical_concentration_mM == 8.0
    assert m.animal_evidence_only is False
    assert m.concentration_evidence_quote == "Creatine concentration was 8.0 +/- 1.2 mM."
    assert m.smiles == "NC(=N)N(C)CC(=O)O"


def test_render_markdown_runs_and_contains_name():
    lit, chem, phys = _build_reports()
    final = _merge(lit, chem, phys, {})
    md = render_markdown(final)
    assert "creatine" in md
    assert "3 T" in md or "3.0" in md.replace(" ", "")
    assert "human in vivo" in md


def test_merge_attaches_verification_plan_when_given():
    lit, chem, phys = _build_reports()
    plan = _build_verification_plan()
    final = _merge(lit, chem, phys, {}, plan)
    assert final.in_vivo_verification_plan is plan


def test_merge_leaves_verification_plan_none_by_default():
    lit, chem, phys = _build_reports()
    final = _merge(lit, chem, phys, {})
    assert final.in_vivo_verification_plan is None


def test_render_markdown_includes_verification_plan_section():
    lit, chem, phys = _build_reports()
    final = _merge(lit, chem, phys, {}, _build_verification_plan())
    md = render_markdown(final)
    assert "In vivo verification: ROI-differential CEST scan" in md
    assert "paraspinal muscle: 2.500%" in md
    assert "liver: 0.800%" in md
