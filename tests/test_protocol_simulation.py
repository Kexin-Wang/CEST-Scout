"""The verification scan is simulated as it would be run -- pulse train, readout, TR steady state
-- and every verdict is graded against the stated uncertainty of its inputs."""
from __future__ import annotations

import numpy as np

from cest_scout.skills import bloch_bridge as bb
from cest_scout.skills.bloch_mcconnell import PulseSegment, z_spectrum

from cest_scout.pipeline import (
    MetaboliteSimulation,
    _robustness,
    _simulate_organ_predictions,
    parse_concentration_range,
    pipeline_settings,
)
from cest_scout.schemas import (
    InVivoVerificationPlan,
    OrganMetabolitePrediction,
    ScanProtocol,
    SequenceRecommendation,
    SimulationParameters,
)
from cest_scout.skills.exchange_reference import ReferenceEntry, lookup


def _pools():
    return [bb.Pool("water", 1.3, 0.08, float("inf"), 0.0, 1.0),
            bb._metabolite_pool("t", 3.5, 30.0, 200.0, 1, 1.0, 0.01),
            bb._mt_pool(-2.4, 30.0, 0.05, 1.0, 10e-6),
            bb._metabolite_pool("bg", 2.0, 500.0, 8.0, 2, 1.0, 0.01)]


def test_affine_map_single_shot_reproduces_the_shared_engine_exactly():
    pools = _pools()
    offs = np.linspace(-6, 6, 13)
    b1_hz = 1.0 * bb.GYRO_1H_MHZ_PER_T
    engine = z_spectrum(offs, pools, [PulseSegment(b1_hz, 0.0, 2.0)], field_t=3.0)
    ours, model = bb._z_spectrum_protocol(offs, pools, 3.0, b1_hz, 2.0)
    assert np.max(np.abs(engine - ours)) < 1e-12
    assert "single shot" in model


def _sim(**over):
    args = dict(shift_ppm=3.5, exchange_rate_per_s=30.0, pool_t1_s=1.0, pool_t2_s=0.01, concentration_mM=200.0,
                n_exchangeable_protons=1, field_strength_T=3.0, b1_uT=1.0, saturation_time_s=2.0)
    args.update(over)
    return bb.simulate_with_without_metabolite(**args)


def test_long_tr_steady_state_converges_to_the_single_shot_result():
    single = _sim()
    long_tr = _sim(tr_s=60.0)
    assert abs(single["contrast_diff_percent"] - long_tr["contrast_diff_percent"]) < 0.01
    assert "steady state" in long_tr["saturation_model"]


def test_short_tr_and_low_duty_cycle_each_cost_contrast_and_z_stays_bounded():
    cw = _sim()
    short_tr = _sim(tr_s=2.5)
    pulsed = _sim(n_pulses=20, pulse_duration_s=0.05, interpulse_delay_s=0.05, tr_s=5.0)
    assert short_tr["contrast_diff_percent"] < cw["contrast_diff_percent"]
    assert pulsed["contrast_diff_percent"] < cw["contrast_diff_percent"]
    assert abs(pulsed["b1_rms_uT"] - 1.0 * np.sqrt(0.5)) < 1e-3   # reported to 4 decimals
    assert "20 x 50 ms" in pulsed["saturation_model"] and "duty cycle 50%" in pulsed["saturation_model"]
    for r in (short_tr, pulsed):
        assert all(-1e-9 <= z <= 1.0 + 1e-6 for z in r["z_with_metabolite"] + r["z_without_metabolite"])


def test_call_accepts_protocol_arguments_and_rejects_bad_ones():
    ok = bb.call({"shift_ppm": 2.0, "exchange_rate_per_s": 500.0, "pool_t1_s": 1.0, "pool_t2_s": 0.01,
                  "concentration_mM": 8.0, "n_exchangeable_protons": 2, "field_strength_T": 3.0, "b1_uT": 1.0,
                  "saturation_time_s": 1.0, "n_pulses": 10, "pulse_duration_s": 0.05, "interpulse_delay_s": 0.05,
                  "tr_s": 3.0, "metabolite_label": "liver::creatine"})
    assert "pulsed train of 10" in ok["saturation_model"]
    try:
        bb.validate_call_args({"concentration_mM": 8.0, "n_exchangeable_protons": 2, "n_pulses": 0})
    except ValueError as exc:
        assert "n_pulses" in str(exc)
    else:
        raise AssertionError("n_pulses=0 should be rejected")


def test_concentration_range_parsing_is_tolerant_but_never_guesses():
    assert parse_concentration_range("1-5 mM", 3.0) == (1.0, 5.0)
    assert parse_concentration_range("200–400 mM", 300.0) == (200.0, 400.0)
    assert parse_concentration_range("~1-7 mM intracellular (hepatocyte)", 4.0) == (1.0, 7.0)
    assert parse_concentration_range("0.5 to 2 mM", 5.0) == (0.5, 5.0)   # widened to include the point estimate
    assert parse_concentration_range("I don't know", 3.0) is None
    assert parse_concentration_range("~5 mM", 5.0) is None
    assert parse_concentration_range("0-0.1 mM", 0.05) == (0.0, 0.1)
    assert parse_concentration_range("0.001-10 mM", 1.0) is None       # thousand-fold span: not a usable range


def test_lookup_reports_the_kex_spread_of_the_tier():
    entries = (
        ReferenceEntry(group_class="amide", metabolite="", aliases=[], shift_ppm=3.5, shift_range_ppm=None, kex_per_s=30.0,
                       kex_range_per_s=[20.0, 45.0], tissue="brain", species="rat", measurement_type="animal in vivo",
                       conditions="", pmid="1", title="a", quote="q"),
        ReferenceEntry(group_class="amide", metabolite="", aliases=[], shift_ppm=3.5, shift_range_ppm=None, kex_per_s=95.0,
                       kex_range_per_s=None, tissue="brain", species="rat", measurement_type="animal in vivo",
                       conditions="", pmid="2", title="b", quote="q"),
        ReferenceEntry(group_class="guanidinium", metabolite="creatine", aliases=[], shift_ppm=2.0, shift_range_ppm=None,
                       kex_per_s=950.0, kex_range_per_s=None, tissue="phantom", species="", measurement_type="phantom/solution",
                       conditions="", pmid="3", title="c", quote="q"),
    )
    amide = lookup("amide", "protein amide pool", [], tissue="liver", entries=entries)
    assert (amide.kex_low_per_s, amide.kex_high_per_s, amide.n_kex_measurements) == (20.0, 95.0, 2)
    assert "20-95 s^-1" in amide.provenance
    cr = lookup("guanidinium", "creatine", [], tissue="liver", entries=entries)
    assert cr.kex_low_per_s == cr.kex_high_per_s == 950.0 and "uncertainty is unknown" in cr.provenance


def _param(**over):
    base = dict(metabolite_name="X", group_class="amide", group_text="amide NH", n_exchangeable_protons=1,
                concentration_mM=100.0, shift_ppm=3.5, kex_per_s=30.0, status="simulated")
    base.update(over)
    return SimulationParameters(**base)


def test_robustness_grades_against_the_low_and_high_corners():
    p = _param(concentration_low_mM=50.0, concentration_high_mM=200.0, kex_low_per_s=20.0, kex_high_per_s=95.0)
    robust = MetaboliteSimulation(name="X", parameters=[p], contrast_range_by_field={"3.0": [0.7, 2.0], "7.0": [1.5, 4.0]},
                                  best_group_by_field={"3.0": p, "7.0": p})
    assert _robustness(robust, True)[0] == "robust"
    fragile = MetaboliteSimulation(name="X", parameters=[p], contrast_range_by_field={"3.0": [0.2, 2.0]},
                                   best_group_by_field={"3.0": p})
    grade, note = _robustness(fragile, True)
    assert grade == "fragile" and "50-200 mM" in note and "20-95 s^-1" in note
    assert _robustness(MetaboliteSimulation(name="X", parameters=[p], contrast_range_by_field={"3.0": [0.1, 0.9]},
                                            best_group_by_field={"3.0": p}), False)[0] == "upper-bound only"
    assert _robustness(MetaboliteSimulation(name="X", parameters=[p]), True)[0] == "unknown"
    assert _robustness(None, False)[0] == "no"


def test_organ_predictions_use_the_protocol_pulse_train_and_report_a_b1_range():
    p = _param(metabolite_name="Creatine", group_class="guanidinium", group_text="guanidinium NH2",
               n_exchangeable_protons=2, concentration_mM=8.0, shift_ppm=2.0, kex_per_s=500.0)
    sim = MetaboliteSimulation(name="Creatine", parameters=[p], best_group_by_field={"3.0": p},
                               recommendations=[SequenceRecommendation(field_strength_T=3.0, b1_uT=1.0, saturation_time_ms=1000,
                                                                       duty_cycle_or_pulses="cw", rationale="")])
    plan = InVivoVerificationPlan(
        scan_rationale="t",
        scan_protocol=ScanProtocol(field_strength_T=3.0, pulse_shape="Gaussian train", b1_uT=1.0, n_pulses=20,
                                   pulse_duration_ms=50, interpulse_delay_ms=50, duty_cycle_percent=50,
                                   total_saturation_time_ms=1950, tr_ms=4000, sar_rationale="s", compromise_rationale="c"),
        zspectrum_offsets=[], fat_suppression="f", motion_mitigation="m", slice_coverage="s", organs_in_fov=["liver", "muscle"],
        organ_predictions=[OrganMetabolitePrediction(organ="liver", metabolite_name="Creatine", organ_concentration_mM=8.0,
                                                     concentration_rationale="r"),
                           OrganMetabolitePrediction(organ="paraspinal muscle", metabolite_name="Creatine",
                                                     organ_concentration_mM=30.0, concentration_rationale="r")],
        differential_analysis_plan="d", caveats="",
    )
    _simulate_organ_predictions(plan, {"Creatine": sim}, 3.0)
    liver, muscle = plan.organ_predictions
    assert "pulsed train of 20 x 50 ms" in liver.simulation_note and "TR 4000 ms" in liver.simulation_note
    assert liver.b1_scale_range == [0.7, 1.3] and len(liver.predicted_contrast_range_percent) == 2
    lo, hi = liver.predicted_contrast_range_percent
    assert lo <= liver.predicted_contrast_percent <= hi
    assert muscle.predicted_contrast_percent > liver.predicted_contrast_percent  # more creatine, more contrast


def test_pipeline_settings_are_recorded_for_comparability():
    s = pipeline_settings()
    assert s["contrast_threshold_percent"] == 0.5 and s["b1_inhomogeneity_fraction"] == 0.3
    assert "steady state" in s["verification_scan_model"] and s["field_strengths_T"] == [0.5, 1.5, 3.0, 5.0, 7.0]
