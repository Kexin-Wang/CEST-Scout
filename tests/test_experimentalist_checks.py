"""Deterministic checks on the experimentalist agent's output: the ROI set it chooses for the
tissue must include the target tissue, every (ROI, metabolite) pair must be predicted, and the
scan protocol's timing must add up."""
from __future__ import annotations

from cest_scout.agents import experimentalist_agent
from cest_scout.schemas import InVivoVerificationPlan, OrganMetabolitePrediction, RoiDefinition, ScanProtocol


def _protocol(**overrides) -> ScanProtocol:
    base = dict(field_strength_T=3.0, pulse_shape="Gaussian train", b1_uT=1.0, n_pulses=20,
                pulse_duration_ms=50.0, interpulse_delay_ms=50.0, duty_cycle_percent=50.0,
                total_saturation_time_ms=1950.0, tr_ms=4000.0, sar_rationale="s", compromise_rationale="c")
    base.update(overrides)
    return ScanProtocol(**base)


def _roi(name, target=False, definition=""):
    return RoiDefinition(name=name, anatomical_definition=definition or name, is_target_tissue=target, role="r")


DISC_ROIS = [
    _roi("nucleus pulposus", True, "centre of the L4/L5 intervertebral disc on the midsagittal slice"),
    _roi("annulus fibrosus", True, "anterior annulus of the same disc"),
    _roi("vertebral body marrow", False, "L4 vertebral body"),
    _roi("paraspinal muscle", False, "erector spinae at L4"),
]


def _plan(protocol: ScanProtocol, predictions: list[OrganMetabolitePrediction], rois=None) -> InVivoVerificationPlan:
    return InVivoVerificationPlan(
        scan_rationale="r", imaging_plane="sagittal", roi_set=DISC_ROIS if rois is None else rois,
        scan_protocol=protocol, zspectrum_offsets=[], fat_suppression="f",
        motion_mitigation="m", slice_coverage="s", organs_in_fov=["disc"], organ_predictions=predictions,
        differential_analysis_plan="d", caveats="",
    )


def _pred(roi, metabolite, conc=5.0):
    return OrganMetabolitePrediction(organ=roi, metabolite_name=metabolite, organ_concentration_mM=conc, concentration_rationale="r")


def test_consistent_protocol_has_no_problems():
    assert experimentalist_agent.protocol_problems(_protocol()) == []
    assert experimentalist_agent._check_output(_plan(_protocol(), [])) is None


def test_protocol_problems_catch_tsat_over_tr_train_sum_and_duty_cycle():
    problems = experimentalist_agent.protocol_problems(
        _protocol(n_pulses=1800, pulse_duration_ms=1.0, interpulse_delay_ms=2.0, duty_cycle_percent=10.0,
                  total_saturation_time_ms=3000.0, tr_ms=300.0)
    )
    joined = " ".join(problems)
    assert "exceeds tr_ms" in joined
    assert "sums to 5398" in joined
    assert "duty_cycle_percent" in joined
    complaint = experimentalist_agent._check_output(_plan(_protocol(tr_ms=300.0), []))
    assert complaint and "not self-consistent" in complaint


def test_surviving_protocol_problems_are_flagged_in_caveats():
    plan = _plan(_protocol(total_saturation_time_ms=9000.0, tr_ms=4000.0, n_pulses=60, pulse_duration_ms=50.0,
                           interpulse_delay_ms=100.0, duty_cycle_percent=33.0), [])
    experimentalist_agent._flag_protocol_problems(plan)
    assert "[Protocol arithmetic]" in plan.caveats


def test_target_tissue_rois_require_the_flag_and_a_word_of_the_body_part():
    plan = _plan(_protocol(), [])
    assert experimentalist_agent.target_tissue_rois(plan, "intervertebral disc") == ["nucleus pulposus", "annulus fibrosus"]
    # A flag on an unrelated ROI does not count; "liver" is not in "vertebral body marrow".
    fake = _plan(_protocol(), [], rois=[_roi("vertebral body marrow", True, "L4 body"), _roi("paraspinal muscle")])
    assert experimentalist_agent.target_tissue_rois(fake, "liver") == []


def test_output_check_demands_the_target_tissue_every_pair_and_known_roi_names():
    check = experimentalist_agent._make_output_check(["Glycosaminoglycans"], "intervertebral disc")
    # Complete plan passes.
    preds = [_pred(r.name, "Glycosaminoglycans", c) for r, c in zip(DISC_ROIS, (60.0, 30.0, 1.0, 2.0))]
    assert check(_plan(_protocol(), preds)) is None
    # A missing pair is named.
    complaint = check(_plan(_protocol(), preds[:2]))
    assert complaint and "vertebral body marrow::Glycosaminoglycans" in complaint
    # No ROI inside the target tissue: sent back.
    abdominal = [_roi("liver", True, "right lobe"), _roi("spleen"), _roi("paraspinal muscle")]
    plan = _plan(_protocol(), [_pred(r.name, "Glycosaminoglycans", c) for r, c in zip(abdominal, (1.0, 2.0, 3.0))], rois=abdominal)
    complaint = check(plan)
    assert complaint and "cuts into the target tissue 'intervertebral disc'" in complaint
    # A prediction for an ROI that is not in roi_set is flagged.
    stray = _plan(_protocol(), preds + [_pred("spleen", "Glycosaminoglycans")])
    assert "not in roi_set" in (check(stray) or "")
    # Too few ROIs.
    assert "define 2-5" in (check(_plan(_protocol(), [], rois=[DISC_ROIS[0]])) or "")


def test_surviving_missing_target_tissue_is_flagged_in_caveats():
    abdominal = [_roi("liver", True, "right lobe"), _roi("spleen")]
    plan = _plan(_protocol(), [], rois=abdominal)
    experimentalist_agent._flag_target_tissue_missing(plan, "intervertebral disc")
    assert "[Target tissue not sampled]" in plan.caveats
    ok = _plan(_protocol(), [])
    experimentalist_agent._flag_target_tissue_missing(ok, "intervertebral disc")
    assert "[Target tissue not sampled]" not in ok.caveats
