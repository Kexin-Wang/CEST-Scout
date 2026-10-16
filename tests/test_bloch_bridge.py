from cest_scout.skills.bloch_bridge import simulate_with_without_metabolite


def _sim(field_t, exchange_rate=30.0, shift_ppm=3.5, b1_uT=2.0, background_metabolites=None):
    return simulate_with_without_metabolite(
        shift_ppm=shift_ppm,
        exchange_rate_per_s=exchange_rate,
        pool_t1_s=1.0,
        pool_t2_s=0.01,
        concentration_mM=50.0,
        n_exchangeable_protons=1,
        field_strength_T=field_t,
        b1_uT=b1_uT,
        background_metabolites=background_metabolites,
    )


def test_higher_field_increases_spectral_separation():
    low = _sim(0.5)
    high = _sim(7.0)
    assert high["delta_omega_rad_per_s"] > low["delta_omega_rad_per_s"]


def test_z_spectrum_values_bounded():
    r = _sim(3.0)
    assert 0.0 <= r["z_with_at_plus_shift"] <= 1.0
    assert 0.0 <= r["z_without_at_plus_shift"] <= 1.0
    assert all(0.0 <= z <= 1.0 for z in r["z_with_metabolite"])
    assert all(0.0 <= z <= 1.0 for z in r["z_without_metabolite"])


def test_very_slow_exchange_labeled_slow_regime():
    r = _sim(3.0, exchange_rate=1.0)
    assert "slow" in r["exchange_regime"]


def test_metabolite_pool_makes_a_difference_vs_baseline():
    r = _sim(3.0)
    # With the metabolite pool present, water Mz at its own offset should drop measurably
    # relative to the no-metabolite baseline.
    assert r["contrast_diff_percent"] > 0.05


def test_negligible_shift_gives_negligible_contrast_diff():
    r = _sim(3.0, shift_ppm=0.0001)
    assert abs(r["contrast_diff_percent"]) < 0.05


def test_background_metabolite_close_to_target_flags_overlap():
    r = _sim(1.5, shift_ppm=2.0, background_metabolites=[
        {"label": "neighbor", "shift_ppm": 2.05, "exchange_rate_per_s": 30.0,
         "concentration_mM": 50.0, "n_exchangeable_protons": 1},
    ])
    assert any("neighbor" in w for w in r["overlap_warnings"])


def test_high_b1_close_to_water_flags_direct_saturation():
    r = _sim(3.0, shift_ppm=0.3, b1_uT=10.0)
    assert any("direct" in w.lower() for w in r["overlap_warnings"])


def _call_args(**overrides):
    args = dict(shift_ppm=1.0, exchange_rate_per_s=1000.0, pool_t1_s=1.0, pool_t2_s=0.01, concentration_mM=300.0,
                n_exchangeable_protons=3, field_strength_T=3.0, b1_uT=1.0, metabolite_label="glycogen")
    args.update(overrides)
    return args


def test_call_accepts_a_per_unit_glycogen_pool():
    from cest_scout.skills import bloch_bridge
    bloch_bridge.validate_call_args(_call_args())
    result = bloch_bridge.call(_call_args())
    assert "contrast_diff_percent" in result and result["metabolite_label"] == "glycogen"


def test_call_rejects_whole_molecule_proton_counts_and_absurd_b1():
    import pytest
    from cest_scout.skills import bloch_bridge
    # A run modelled glycogen as 2 mM x 1000 exchangeable protons at up to 24 uT and got a 57%
    # "contrast" that was reported as real.
    with pytest.raises(ValueError, match="per-molecule"):
        bloch_bridge.call(_call_args(concentration_mM=2.0, n_exchangeable_protons=1000))
    with pytest.raises(ValueError, match="exchangeable protons"):
        bloch_bridge.call(_call_args(concentration_mM=2000.0, n_exchangeable_protons=6))
    with pytest.raises(ValueError, match="b1_uT"):
        bloch_bridge.call(_call_args(b1_uT=24.0))
    with pytest.raises(ValueError, match="background"):
        bloch_bridge.call(_call_args(background_metabolites=[{
            "label": "x", "shift_ppm": 2.0, "exchange_rate_per_s": 500.0, "concentration_mM": 5.0,
            "n_exchangeable_protons": 500,
        }]))


def test_decomposition_splits_the_drop_at_the_offset():
    r = simulate_with_without_metabolite(
        shift_ppm=3.5, exchange_rate_per_s=30.0, pool_t1_s=1.0, pool_t2_s=0.01, concentration_mM=50.0,
        n_exchangeable_protons=1, field_strength_T=3.0, b1_uT=1.0, decompose=True)
    ds, mt, cest = r["direct_saturation_percent"], r["mt_background_percent"], r["contrast_diff_percent"]
    assert ds > 0 and mt > 0 and cest > 0
    # water alone -> plus MT/background -> plus the metabolite: the pieces add up to the whole drop
    z_with = r["z_with_at_plus_shift"] if r["decomposed_at_ppm"] > 0 else r["z_with_at_minus_shift"]
    assert abs(100.0 * (1.0 - z_with) - (ds + mt + cest)) < 0.01
    assert "direct_saturation_percent" not in _sim(3.0)   # off by default: the sweep never pays for it
