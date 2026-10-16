"""Tests for the Bloch-McConnell B1/Tsat sweep that replaces the physicist agent's asserted
saturation settings (bloch_bridge.optimize_saturation, used by pipeline.run_reference_simulations).

These call the real simulator rather than stubbing it: the whole point of the feature is that the
numbers come from the physics, so a test that mocks the physics would verify nothing.
"""
from cest_scout.skills import bloch_bridge

# A creatine-like amine pool: intermediate exchange, well clear of water.
CREATINE_LIKE = dict(shift_ppm=2.0, exchange_rate_per_s=500.0, pool_t1_s=1.0, pool_t2_s=0.01,
                      concentration_mM=8.0, n_exchangeable_protons=4)


def test_no_sar_model_is_applied():
    """The earlier B1 cap was an assumption, not a SAR calculation, and was removed: the sweep
    reports its optimum over the whole grid and says so."""
    best = bloch_bridge.optimize_saturation(field_strength_T=3.0, **CREATINE_LIKE)
    assert "sar_note" in best and "no SAR" in best["sar_note"]
    assert not hasattr(bloch_bridge, "sar_b1_ceiling_uT")
    assert best["b1_uT"] in bloch_bridge.B1_GRID_UT


def test_optimum_beats_the_old_hardcoded_setting():
    """The bug this feature fixes: a fixed 3 uT / 2000 ms sits past the contrast peak."""
    swept = bloch_bridge.optimize_saturation(field_strength_T=3.0, **CREATINE_LIKE)
    fixed = bloch_bridge.simulate_with_without_metabolite(
        field_strength_T=3.0, b1_uT=3.0, saturation_time_s=2.0, **CREATINE_LIKE)

    assert swept["contrast_percent"] > fixed["contrast_diff_percent"]
    assert swept["b1_uT"] < 3.0, "optimum for this pool is below the old hardcoded 3 uT"
    assert swept["n_simulations"] == len(bloch_bridge.B1_GRID_UT) * len(bloch_bridge.TSAT_GRID_MS)


def test_optimum_is_the_best_cell_of_the_whole_grid():
    for field in (3.0, 7.0):
        best = bloch_bridge.optimize_saturation(field_strength_T=field, **CREATINE_LIKE)
        column_max = max(v for k, v in best["sweep_contrast"].items() if k.startswith(f"{float(best['b1_uT'])}|"))
        assert abs(best["max_achievable_contrast_percent"] - column_max) < 1e-9
        assert best["max_achievable_contrast_percent"] >= max(best["sweep_contrast"].values()) - 1e-9


def test_tsat_is_the_grid_optimum_shorter_on_ties():
    """No knee tolerance: the recommended Tsat is the highest-contrast cell (shorter on a tie)."""
    assert bloch_bridge._best_tsat({500.0: 0.90, 1000.0: 0.99, 2000.0: 1.00, 3000.0: 1.005}) == 3000.0
    assert bloch_bridge._best_tsat({500.0: 0.9, 1000.0: 1.0, 2000.0: 1.0}) == 1000.0
    assert not hasattr(bloch_bridge, "KNEE_TOLERANCE")


def test_max_achievable_contrast_is_never_below_the_recommended_sequences_own_contrast():
    """Structural invariant, checked against the live simulator: the knee-shortened Tsat can
    never do BETTER than the true column ceiling, by construction."""
    for field in (0.5, 3.0, 7.0):
        r = bloch_bridge.optimize_saturation(field_strength_T=field, **CREATINE_LIKE)
        assert r["max_achievable_contrast_percent"] >= r["contrast_percent"]


def test_recommended_contrast_equals_the_ceiling():
    r = bloch_bridge.optimize_saturation(field_strength_T=3.0, **CREATINE_LIKE)
    assert abs(r["contrast_percent"] - r["max_achievable_contrast_percent"]) < 1e-9


def test_sweep_result_has_no_legacy_sar_fields():
    for shift, kex in [(2.0, 500.0), (3.5, 5000.0)]:
        r = bloch_bridge.optimize_saturation(
            field_strength_T=3.0, shift_ppm=shift, exchange_rate_per_s=kex,
            pool_t1_s=1.0, pool_t2_s=0.01, concentration_mM=10.0, n_exchangeable_protons=2)
        assert "sar_b1_ceiling_uT" not in r and "fraction_of_achievable_contrast_retained" not in r
