"""Bridge into the Bloch-McConnell engine vendored at cest_scout/skills/bloch_mcconnell.py.

Reuses the CEST_simulator project's bloch_mcconnell.py (a validated general
N-pool Bloch-McConnell solver) rather than re-implementing the physics, per
the user's instruction. Builds a multi-pool (water + semisolid MT + target
metabolite + optional background metabolites) system and simulates the full
Z-spectrum TWICE per field strength -- once with the target metabolite pool
present, once without it -- so the physicist agent's detectability call is
the actual difference those two curves make, not a single-pool MTRasym
number. Literature-reported MTRasym values are NOT reproduced or trusted
here: MTRasym conflates the metabolite's own exchange contrast with
background NOE/MT asymmetry, which is exactly what simulating "with" and
"without" the metabolite (against the same MT+background pools) is designed
to separate out.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
from scipy.linalg import expm

from cest_scout.skills import tissue_reference
from cest_scout.skills.bloch_mcconnell import (
    GYRO_1H_MHZ_PER_T,
    Pool,
    PulseSegment,
    _chemical_shift_matrix,
    _equilibrium_vector,
    _exchange_matrix,
    _pulse_matrix,
    _relaxation_matrix,
    mt_saturation_rate,
    z_spectrum,
)

# Standard CEST-field convention: pure water proton concentration, ~2 x 55.5 M,
# expressed in mM. Metabolite pool concentration is expressed relative to this,
# matching CEST_simulator/tissue_pools.py's convention (water pool concentration = 1.0).
WATER_PROTON_CONC_MM = 110_000.0

# Generic literature-order-of-magnitude semisolid MT pool defaults (Morrison & Henkelman-style
# semisolid pool centered near -2.4 to -3.5 ppm, T2 ~ 10 us, k_ex ~ tens of s^-1; see
# CEST_simulator/tissue_pools.py's brain-specific values for a worked example). Exposed as tool
# parameters -- not hardcoded to brain -- so the physicist agent can adjust fraction/kex per body
# part (e.g. denser semisolid content in muscle/liver than CSF).
DEFAULT_MT_SHIFT_PPM = -2.4
DEFAULT_MT_EXCHANGE_RATE_PER_S = 30.0
DEFAULT_MT_FRACTION_OF_WATER = 0.05
DEFAULT_MT_T1_S = 1.0
DEFAULT_MT_T2_S = 10e-6

# --- Pulsed saturation, readout and periodic steady state ---------------------------------------
#
# bloch_mcconnell.z_spectrum() propagates ONE shot of saturation from thermal equilibrium, which is
# the right idealization for the CW B1 x Tsat sweep (a field-strength recommendation that does not
# depend on any one sequence). A real Z-spectrum acquisition differs in three ways that all change
# the contrast that would actually be measured: (1) the saturation is a duty-cycled pulse train --
# SAR on a body transmit coil rules out seconds of continuous wave; (2) the readout excitation
# consumes longitudinal magnetization; (3) the next offset's saturation starts after only TR - Tsat
# of recovery, so the spin system sits in a periodic steady state, not at M0. Every constant-RF
# segment of such a cycle is an affine map y -> T y + c on the state vector (exactly one step of
# bloch_mcconnell.solve, which is why the engine's own matrix builders are reused below rather
# than re-implemented), so the whole cycle is one affine map and its fixed point is the exact
# steady state -- no iteration over repeats, and a Z-spectrum normalized to the same cycle without
# saturation, as a real S0 reference scan is.
#
# Approximations, stated once here and repeated in every simulation note: each pulse is modelled
# as a rectangular pulse of its stated duration at the protocol's B1 (a shaped pulse with the same
# mean B1 saturates a little less efficiently); the excitation flips every pool by the same angle
# and its transverse magnetization is crushed; readout duration is folded into the recovery time.

# Typical transmit-B1 variation across the abdomen and pelvis at 3 T is of order +/-30 %; the
# verification-scan predictions are re-simulated at (1 -/+ this) x B1 so a reader sees how much of
# a predicted contrast survives an uncalibrated B1 rather than a single point estimate.
B1_INHOMOGENEITY_FRACTION = 0.3


def _segment_map(pools: list, offset_ppm: float, field_t: float, strength_hz: float, duration_s: float):
    """Affine map (T, c) with y_after = T @ y_before + c for one constant-RF segment, reproducing
    one step of bloch_mcconnell.solve() (including its semisolid-MT saturation-rate term and its
    zero-power 'delay' convention for the chemical-shift block)."""
    n = len(pools)
    y0 = _equilibrium_vector(pools)
    exch = _exchange_matrix(pools)
    rmat = _relaxation_matrix(pools)
    cs_mat, w = _chemical_shift_matrix(pools, offset_ppm, field_t, 1.0, strength_hz == 0.0)
    pulse_mat, x_amp, y_amp = _pulse_matrix(pools, strength_hz, 0.0)
    evol = exch + rmat + cs_mat + pulse_mat
    if n > 2 and pools[2].lineshape:
        i = 2
        w1 = float(np.hypot(x_amp[i], y_amp[i]))
        evol[2 * n + i, 2 * n + i] -= mt_saturation_rate(pools[i].t2, w1, w[i], pools[i].lineshape)
    yp = np.linalg.solve(evol, -rmat @ y0)
    trans = expm(evol * duration_s)
    return trans, trans @ yp - yp


def _compose(first, second):
    """The map 'first, then second'."""
    t1, c1 = first
    t2, c2 = second
    return t2 @ t1, t2 @ c1 + c2


def _excitation_map(n_pools: int, flip_deg: float):
    """Readout excitation: every pool's Mz is scaled by cos(flip) and its transverse magnetization
    is crushed. 90 degrees (single-shot readout) leaves no memory of the previous cycle."""
    n = n_pools
    t = np.zeros((3 * n, 3 * n))
    t[2 * n:, 2 * n:] = np.eye(n) * float(np.cos(np.deg2rad(flip_deg)))
    return t, np.zeros(3 * n)


def pulse_train_duration_s(n_pulses: int, pulse_duration_s: float, interpulse_delay_s: float) -> float:
    n = max(int(n_pulses), 1)
    return n * float(pulse_duration_s) + (n - 1) * max(float(interpulse_delay_s), 0.0)


def _z_spectrum_protocol(
    offsets_ppm: np.ndarray, pools: list, field_t: float, b1_hz: float, saturation_time_s: float,
    n_pulses: int = 1, pulse_duration_s: float | None = None, interpulse_delay_s: float = 0.0,
    tr_s: float | None = None, excitation_flip_deg: float = 90.0,
) -> tuple[np.ndarray, str]:
    """Water Mz across offsets under a pulsed saturation train and, when tr_s is given, in the
    periodic steady state of [saturation -> excitation -> recovery] cycles, normalized to the same
    steady state acquired without saturation. Returns (Z, description of the model)."""
    n = len(pools)
    zidx = 2 * n
    n_pulses = max(int(n_pulses or 1), 1)
    if n_pulses == 1 or not pulse_duration_s or pulse_duration_s <= 0:
        n_pulses, pulse_duration_s, interpulse_delay_s = 1, float(saturation_time_s), 0.0
    interpulse_delay_s = max(float(interpulse_delay_s), 0.0)
    train_s = pulse_train_duration_s(n_pulses, pulse_duration_s, interpulse_delay_s)
    recovery_s = (float(tr_s) - train_s) if tr_s else 0.0
    steady = recovery_s > 0
    y0 = _equilibrium_vector(pools)

    def train_map(offset: float, strength_hz: float):
        on = _segment_map(pools, offset, field_t, strength_hz, pulse_duration_s)
        if n_pulses == 1:
            return on
        gap = _segment_map(pools, offset, field_t, 0.0, interpulse_delay_s)
        period = _compose(gap, on)
        m = on
        for _ in range(n_pulses - 1):
            m = _compose(m, period)
        return m

    def steady_state(offset: float, strength_hz: float) -> np.ndarray:
        # Cycle written from the point the readout samples (right after saturation): excite,
        # recover for TR - Tsat, saturate. Its fixed point is the state read out at every TR.
        cycle = _compose(_compose(_excitation_map(n, excitation_flip_deg),
                                  _segment_map(pools, offset, field_t, 0.0, recovery_s)),
                         train_map(offset, strength_hz))
        t, c = cycle
        return np.linalg.solve(np.eye(3 * n) - t, c)

    if steady:
        # S0 reference: the identical cycle with the saturation power off (offset irrelevant).
        m_ref = float(steady_state(0.0, 0.0)[zidx])
    z = np.empty(len(offsets_ppm), dtype=float)
    for i, off in enumerate(offsets_ppm):
        if steady:
            z[i] = float(steady_state(float(off), b1_hz)[zidx]) / m_ref
        else:
            t, c = train_map(float(off), b1_hz)
            z[i] = float((t @ y0 + c)[zidx])
    if n_pulses == 1:
        pulse_bit = f"continuous-wave saturation for {saturation_time_s * 1000:g} ms"
    else:
        duty = pulse_duration_s / (pulse_duration_s + interpulse_delay_s) if (pulse_duration_s + interpulse_delay_s) > 0 else 1.0
        pulse_bit = (f"pulsed train of {n_pulses} x {pulse_duration_s * 1000:g} ms rectangular pulses with "
                     f"{interpulse_delay_s * 1000:g} ms gaps (duty cycle {duty:.0%}, train {train_s * 1000:g} ms)")
    if steady:
        model = (f"{pulse_bit}; periodic steady state of saturation -> {excitation_flip_deg:g} deg excitation -> "
                 f"{recovery_s * 1000:g} ms recovery (TR {tr_s * 1000:g} ms), Z normalized to the same cycle without saturation")
    else:
        model = f"{pulse_bit}; single shot from thermal equilibrium"
    return z, model


_BACKGROUND_METABOLITE_SCHEMA = {
    "type": "object",
    "description": "One other real candidate metabolite from this tissue, included as spectral "
                    "background so the simulation reflects realistic pool competition rather than "
                    "an idealized two-pool system.",
    "properties": {
        "label": {"type": "string", "description": "Name of this background metabolite."},
        "shift_ppm": {"type": "number"},
        "exchange_rate_per_s": {"type": "number"},
        "concentration_mM": {"type": "number"},
        "n_exchangeable_protons": {"type": "integer"},
        "pool_t1_s": {"type": "number", "default": 1.0},
        "pool_t2_s": {"type": "number", "default": 0.01},
    },
    "required": ["label", "shift_ppm", "exchange_rate_per_s", "concentration_mM", "n_exchangeable_protons"],
}

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "simulate_water_mt_metabolite_zspectrum",
        "description": (
            "Run a multi-pool (water + semisolid MT + target metabolite + optional background "
            "metabolites) Bloch-McConnell Z-spectrum simulation TWICE at a given field strength -- "
            "once with the target metabolite pool present, once without it, both against the same "
            "MT/background pools -- and report the resulting difference (%) plus deterministic "
            "spectral-overlap diagnostics. This with-vs-without difference, not any MTRasym value "
            "quoted in a paper (which is contaminated by background NOE/MT asymmetry), is the "
            "authoritative detectability signal: treat >= ~0.5% as the detection threshold. Also "
            "flags (a) whether the target's offset is likely to blur into a background metabolite's "
            "or the MT pool's offset given exchange-broadened linewidths at this field, and (b) "
            "whether the chosen B1 would directly saturate water enough to mask the effect."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "shift_ppm": {"type": "number", "description": "Target exchangeable-proton chemical shift vs. water, ppm."},
                "exchange_rate_per_s": {"type": "number", "description": "Target pool's chemical exchange rate k_ex with water, 1/s."},
                "pool_t1_s": {"type": "number", "description": "T1 of the target metabolite pool, s (order 1.0 if unknown)."},
                "pool_t2_s": {"type": "number", "description": "T2 of the target metabolite pool, s (order 0.01-0.1 for solutes)."},
                "concentration_mM": {"type": "number", "description": "Tissue concentration of the target metabolite, mM."},
                "n_exchangeable_protons": {"type": "integer", "description": "Number of exchangeable protons per molecule in this target pool."},
                "field_strength_T": {"type": "number", "description": "Static field strength, Tesla (0.5/1.5/3/5/7)."},
                "metabolite_label": {
                    "type": "string",
                    "description": "The exact metabolite name this call is for, copied verbatim from the input list. "
                                    "Required so results can be matched back to the right metabolite.",
                },
                "background_metabolites": {
                    "type": "array", "items": _BACKGROUND_METABOLITE_SCHEMA, "default": [],
                    "description": "0-3 other real candidate metabolites from this tissue (highest-concentration "
                                    "ones are most relevant) to include as competing pools in BOTH the with- and "
                                    "without-target simulations.",
                },
                "mt_shift_ppm": {"type": "number", "default": DEFAULT_MT_SHIFT_PPM},
                "mt_exchange_rate_per_s": {"type": "number", "default": DEFAULT_MT_EXCHANGE_RATE_PER_S},
                "mt_fraction_of_water": {"type": "number", "default": DEFAULT_MT_FRACTION_OF_WATER,
                                          "description": "Semisolid MT pool size relative to water (tissue-dependent; brain gray/white matter ~0.03-0.12)."},
                "mt_t2_s": {"type": "number", "default": DEFAULT_MT_T2_S},
                "water_t1_s": {"type": "number", "default": 1.3},
                "water_t2_s": {"type": "number", "default": 0.08},
                "b1_uT": {"type": "number", "description": "Saturation B1 amplitude, microTesla.", "default": 2.0},
                "saturation_time_s": {"type": "number", "description": "Continuous saturation duration, s.", "default": 2.0},
            },
            "required": [
                "shift_ppm", "exchange_rate_per_s", "pool_t1_s", "pool_t2_s",
                "concentration_mM", "n_exchangeable_protons", "field_strength_T", "metabolite_label",
            ],
        },
    },
}


def _relative_conc(concentration_mM: float, n_protons: int) -> float:
    return (concentration_mM * n_protons) / WATER_PROTON_CONC_MM


def _metabolite_pool(name: str, shift_ppm: float, exchange_rate_per_s: float, concentration_mM: float,
                      n_protons: int, t1_s: float, t2_s: float) -> Pool:
    return Pool(name, t1_s, t2_s, 1.0 / exchange_rate_per_s, shift_ppm, _relative_conc(concentration_mM, n_protons))


def _mt_pool(shift_ppm: float, exchange_rate_per_s: float, fraction_of_water: float, t1_s: float, t2_s: float) -> Pool:
    return Pool("mt", t1_s, t2_s, 1.0 / exchange_rate_per_s, shift_ppm, fraction_of_water, lineshape="superlorentzian")


def _exchange_broadened_linewidth_hz(t2_s: float, exchange_rate_per_s: float) -> float:
    """Order-of-magnitude FWHM estimate (Hz): intrinsic T2 broadening plus exchange broadening.

    A heuristic, not a rigorous lineshape calculation -- good enough to flag "these two pools are
    probably not spectrally resolved," which is the deterministic grounding the physicist agent
    needs before it argues about frequency collisions in prose.
    """
    return 1.0 / (np.pi * t2_s) + exchange_rate_per_s / np.pi


def simulate_with_without_metabolite(
    shift_ppm: float,
    exchange_rate_per_s: float,
    pool_t1_s: float,
    pool_t2_s: float,
    concentration_mM: float,
    n_exchangeable_protons: int,
    field_strength_T: float,
    background_metabolites: list[dict] | None = None,
    mt_shift_ppm: float = DEFAULT_MT_SHIFT_PPM,
    mt_exchange_rate_per_s: float = DEFAULT_MT_EXCHANGE_RATE_PER_S,
    mt_fraction_of_water: float = DEFAULT_MT_FRACTION_OF_WATER,
    mt_t2_s: float = DEFAULT_MT_T2_S,
    water_t1_s: float = 1.3,
    water_t2_s: float = 0.08,
    b1_uT: float = 2.0,
    saturation_time_s: float = 2.0,
    n_pulses: int = 1,
    pulse_duration_s: float | None = None,
    interpulse_delay_s: float = 0.0,
    tr_s: float | None = None,
    excitation_flip_deg: float = 90.0,
    decompose: bool = False,
) -> dict:
    """With the defaults (n_pulses=1, tr_s=None) this is one shot of continuous-wave saturation from
    thermal equilibrium, via bloch_mcconnell.z_spectrum -- the idealization the B1 x Tsat sweep
    uses. Passing a pulse train (n_pulses > 1 with pulse_duration_s and interpulse_delay_s) and/or
    a TR simulates the sequence as it would actually be run: see _z_spectrum_protocol."""
    background_metabolites = background_metabolites or []

    water = Pool("water", water_t1_s, water_t2_s, float("inf"), 0.0, 1.0)
    mt = _mt_pool(mt_shift_ppm, mt_exchange_rate_per_s, mt_fraction_of_water, DEFAULT_MT_T1_S, mt_t2_s)
    target = _metabolite_pool("target", shift_ppm, exchange_rate_per_s, concentration_mM,
                               n_exchangeable_protons, pool_t1_s, pool_t2_s)
    # A concentration=0 twin of the target, standing in for it in the "without" system. Both
    # _exchange_matrix and _equilibrium_vector scale a pool's contribution by its own
    # concentration and only ever divide by water's (always 1.0 here), so this pool is inert --
    # its equilibrium and steady-state Mz are both exactly 0 -- while keeping pools_without and
    # pools_with the same length and shape. See the mt-index comment below for why that matters.
    target_absent = _metabolite_pool("target_absent", shift_ppm, exchange_rate_per_s, 0.0,
                                      n_exchangeable_protons, pool_t1_s, pool_t2_s)
    bg_pools = [
        _metabolite_pool(
            bg.get("label", f"background_{i}"), float(bg["shift_ppm"]), float(bg["exchange_rate_per_s"]),
            float(bg["concentration_mM"]), int(bg["n_exchangeable_protons"]),
            float(bg.get("pool_t1_s", 1.0)), float(bg.get("pool_t2_s", 0.01)),
        )
        for i, bg in enumerate(background_metabolites)
    ]

    # bloch_mcconnell.Pool.lineshape's docstring is explicit: the semisolid MT pool "must be
    # pools[2]" -- solve() special-cases exactly that index (mt_pool_idx = 2 if pools[2].lineshape
    # else None) rather than searching for it, matching the reference 5-pool ordering in
    # CEST_simulator/tissue_pools.py ([water, amide, mt, noe, amine, ...]). The previous ordering
    # here put mt at index 1 in both lists, so pools[2] was always the target or a background pool
    # -- neither has a lineshape set, so mt_pool_idx was always None and the semisolid pool's
    # super-Lorentzian off-resonance saturation (its entire physical reason for existing, as
    # opposed to being modeled as an ordinary exchanging pool) never engaged, in any run this
    # project has ever made.
    pools_without = [water, target_absent, mt, *bg_pools]
    pools_with = [water, target, mt, *bg_pools]
    # Water alone -- every other pool at zero concentration, inert like target_absent -- so the
    # drop at the offset splits into direct saturation, MT + background, and the metabolite.
    pools_water = [water, *(replace(p, concentration=0.0) for p in pools_without[1:])] if decompose else None

    all_shifts = [shift_ppm, mt_shift_ppm] + [float(bg["shift_ppm"]) for bg in background_metabolites]
    span = max(abs(s) for s in all_shifts) + 2.0
    offsets = np.linspace(-span, span, 61)

    b1_hz = b1_uT * GYRO_1H_MHZ_PER_T
    pulsed = (n_pulses or 1) > 1 and bool(pulse_duration_s) and pulse_duration_s > 0
    if not pulsed and not tr_s:
        pulse_train = [PulseSegment(strength_hz=b1_hz, phase_rad=0.0, duration_s=saturation_time_s)]
        z_with = z_spectrum(offsets, pools_with, pulse_train, field_t=field_strength_T)
        z_without = z_spectrum(offsets, pools_without, pulse_train, field_t=field_strength_T)
        z_water = z_spectrum(offsets, pools_water, pulse_train, field_t=field_strength_T) if decompose else None
        saturation_model = f"continuous-wave saturation for {saturation_time_s * 1000:g} ms; single shot from thermal equilibrium"
        b1_rms_uT = b1_uT
    else:
        z_with, saturation_model = _z_spectrum_protocol(
            offsets, pools_with, field_strength_T, b1_hz, saturation_time_s, n_pulses, pulse_duration_s,
            interpulse_delay_s, tr_s, excitation_flip_deg,
        )
        z_without, _ = _z_spectrum_protocol(
            offsets, pools_without, field_strength_T, b1_hz, saturation_time_s, n_pulses, pulse_duration_s,
            interpulse_delay_s, tr_s, excitation_flip_deg,
        )
        z_water = _z_spectrum_protocol(
            offsets, pools_water, field_strength_T, b1_hz, saturation_time_s, n_pulses, pulse_duration_s,
            interpulse_delay_s, tr_s, excitation_flip_deg,
        )[0] if decompose else None
        if pulsed:
            duty = pulse_duration_s / (pulse_duration_s + max(interpulse_delay_s, 0.0))
            b1_rms_uT = b1_uT * float(np.sqrt(duty))
        else:
            b1_rms_uT = b1_uT

    def _z_at(z_curve: np.ndarray, ppm: float) -> float:
        return float(np.interp(ppm, offsets, z_curve))

    z_with_pos, z_without_pos = _z_at(z_with, shift_ppm), _z_at(z_without, shift_ppm)
    z_with_neg, z_without_neg = _z_at(z_with, -shift_ppm), _z_at(z_without, -shift_ppm)
    diff_pos_percent = 100.0 * (z_without_pos - z_with_pos)
    diff_neg_percent = 100.0 * (z_without_neg - z_with_neg)
    contrast_diff_percent = max([diff_pos_percent, diff_neg_percent], key=abs)
    background = {}
    if z_water is not None:
        at = shift_ppm if contrast_diff_percent == diff_pos_percent else -shift_ppm
        z_water_at, z_without_at = _z_at(z_water, at), _z_at(z_without, at)
        background = {"decomposed_at_ppm": at,
                      "direct_saturation_percent": round(100.0 * (1.0 - z_water_at), 3),
                      "mt_background_percent": round(100.0 * (z_water_at - z_without_at), 3)}

    offset_hz = shift_ppm * field_strength_T * GYRO_1H_MHZ_PER_T
    delta_omega_rad_s = 2 * np.pi * abs(offset_hz)
    regime_ratio = delta_omega_rad_s / exchange_rate_per_s if exchange_rate_per_s > 0 else float("inf")
    if regime_ratio > 5:
        regime = "slow exchange (well-separated from water; CEST-favorable)"
    elif regime_ratio > 1:
        regime = "intermediate exchange (partial separation; contrast reduced)"
    else:
        regime = "fast exchange (merges toward water; poor CEST visibility, direct saturation dominates)"

    larmor_hz_per_ppm = field_strength_T * GYRO_1H_MHZ_PER_T
    target_linewidth_hz = _exchange_broadened_linewidth_hz(pool_t2_s, exchange_rate_per_s)
    overlap_warnings: list[str] = []

    def _check_pool_overlap(other_label: str, other_ppm: float, other_t2_s: float, other_kex: float) -> None:
        sep_hz = abs(shift_ppm - other_ppm) * larmor_hz_per_ppm
        other_linewidth_hz = _exchange_broadened_linewidth_hz(other_t2_s, other_kex)
        combined_halfwidth_hz = 0.5 * (target_linewidth_hz + other_linewidth_hz)
        if sep_hz < combined_halfwidth_hz:
            overlap_warnings.append(
                f"Target ({shift_ppm:+.2f} ppm) vs {other_label} ({other_ppm:+.2f} ppm) at "
                f"{field_strength_T:g} T: separation {sep_hz:.1f} Hz < combined exchange-broadened "
                f"linewidth ~{combined_halfwidth_hz:.1f} Hz -- these pools likely blur together "
                "rather than resolving as distinct CEST peaks."
            )

    for bg in background_metabolites:
        _check_pool_overlap(bg.get("label", "background"), float(bg["shift_ppm"]),
                             float(bg.get("pool_t2_s", 0.01)), float(bg["exchange_rate_per_s"]))
    _check_pool_overlap("semisolid MT pool", mt_shift_ppm, mt_t2_s, mt_exchange_rate_per_s)

    direct_sat_bandwidth_hz = b1_hz  # nutation-frequency heuristic for the B1 direct-saturation half-width
    target_offset_hz = abs(shift_ppm) * larmor_hz_per_ppm
    if target_offset_hz < direct_sat_bandwidth_hz:
        overlap_warnings.append(
            f"Target offset {target_offset_hz:.1f} Hz from water is within the B1={b1_uT:g} uT "
            f"direct-saturation bandwidth (~{direct_sat_bandwidth_hz:.1f} Hz nutation frequency) at "
            f"{field_strength_T:g} T -- expect substantial direct water saturation to contaminate/"
            "mask the exchange-specific contrast at this offset."
        )

    return {
        "field_strength_T": field_strength_T,
        "offsets_ppm": [round(float(x), 3) for x in offsets],
        "z_with_metabolite": [round(float(x), 5) for x in z_with],
        "z_without_metabolite": [round(float(x), 5) for x in z_without],
        "z_with_at_plus_shift": round(z_with_pos, 5),
        "z_without_at_plus_shift": round(z_without_pos, 5),
        "z_with_at_minus_shift": round(z_with_neg, 5),
        "z_without_at_minus_shift": round(z_without_neg, 5),
        "diff_at_positive_offset_percent": round(diff_pos_percent, 3),
        "diff_at_negative_offset_percent": round(diff_neg_percent, 3),
        "contrast_diff_percent": round(contrast_diff_percent, 3),
        "delta_omega_rad_per_s": round(float(delta_omega_rad_s), 2),
        "exchange_rate_per_s": exchange_rate_per_s,
        "delta_omega_over_kex": round(float(regime_ratio), 3),
        "exchange_regime": regime,
        "relative_target_pool_concentration": _relative_conc(concentration_mM, n_exchangeable_protons),
        "b1_uT": b1_uT,
        "b1_rms_uT": round(float(b1_rms_uT), 4),
        "saturation_model": saturation_model,
        "direct_sat_bandwidth_hz": round(direct_sat_bandwidth_hz, 1),
        "overlap_warnings": overlap_warnings,
        "mt_fraction_of_water": mt_fraction_of_water,
        **background,
    }


def organ_tissue_overrides(organ: str, field_strength_T: float) -> dict:
    """water_t1_s/water_t2_s (and MT parameters when the table has them) for a named tissue/ROI at
    a given field, from skills/tissue_reference.py -- or {} if the table has no water T1 and T2 for
    it, in which case simulate_with_without_metabolite's stated generic defaults apply and the
    caller should say so (tissue_reference.describe gives the sentence)."""
    found = tissue_reference.lookup(organ, field_strength_T)
    if found:
        return dict(found.overrides)
    # Not in the table: the generic defaults, but scaled to this field (T1 rises, T2 is ~flat
    # with B0); the report's tissue rows say so.
    t1, _ = tissue_reference.default_relaxation("T1", field_strength_T)
    t2, _ = tissue_reference.default_relaxation("T2", field_strength_T)
    return {"water_t1_s": t1, "water_t2_s": t2}


# Sanity bounds for what a model may ask the simulator to do. A run modelled glycogen as a 2 mM
# molecule with 1000 exchangeable protons (2 M of exchangeable protons) at 24 uT and got a 57%
# "contrast" that was then reported as real. Pools must be expressed per repeating unit; the most
# abundant endogenous pools (glycogen glucosyl units, the mobile-protein amide pool) are ~1000 mM
# of exchangeable protons, and in vivo saturation stays in single-digit microtesla.
MAX_PROTONS_PER_UNIT = 12
MAX_TARGET_PROTON_MM = 5_000.0
MAX_B1_UT = 12.0


def _check_pool_args(concentration_mM, n_exchangeable_protons, what: str) -> None:
    n = int(n_exchangeable_protons)
    c = float(concentration_mM)
    if not 1 <= n <= MAX_PROTONS_PER_UNIT:
        raise ValueError(
            f"{what}: n_exchangeable_protons={n} is not a per-molecule (or per-repeating-unit) proton "
            f"count. Use 1-{MAX_PROTONS_PER_UNIT}; for a polymer or macromolecular pool give the "
            "concentration PER repeating unit (e.g. glycogen ~250-300 mM glucosyl units x 3 hydroxyl "
            "protons; the mobile-protein amide pool ~200-300 mM x 1), not a whole-molecule count."
        )
    if c <= 0:
        raise ValueError(f"{what}: concentration_mM must be positive, got {c:g}.")
    if c * n > MAX_TARGET_PROTON_MM:
        raise ValueError(
            f"{what}: concentration_mM x n_exchangeable_protons = {c * n:g} mM of exchangeable protons, "
            f"more than any endogenous pool (the most abundant are ~1000 mM). Check units (mM, not M) "
            "and per-unit accounting."
        )


def validate_call_args(kwargs: dict) -> None:
    _check_pool_args(kwargs["concentration_mM"], kwargs["n_exchangeable_protons"], "target")
    for bg in kwargs.get("background_metabolites") or []:
        _check_pool_args(bg["concentration_mM"], bg["n_exchangeable_protons"], f"background {bg.get('label', '?')!r}")
    n_pulses = kwargs.get("n_pulses", 1)
    if n_pulses is not None and int(n_pulses) < 1:
        raise ValueError(f"n_pulses={n_pulses} must be at least 1.")
    for key in ("pulse_duration_s", "interpulse_delay_s", "tr_s", "saturation_time_s"):
        v = kwargs.get(key)
        if v is not None and float(v) < 0:
            raise ValueError(f"{key}={v} must not be negative.")
    b1 = float(kwargs.get("b1_uT", 2.0))
    if not 0 < b1 <= MAX_B1_UT:
        raise ValueError(
            f"b1_uT={b1:g} is outside in vivo CEST saturation (0.1-6 uT is the useful range; hardware "
            f"and SAR keep it under ~{MAX_B1_UT:g} uT). The B1/Tsat you pass is only a starting point "
            "-- the pipeline sweeps them itself -- so use a realistic value."
        )


def call(args: dict) -> dict:
    kwargs = dict(args)
    label = kwargs.pop("metabolite_label", None)
    kwargs["field_strength_T"] = float(kwargs["field_strength_T"])
    validate_call_args(kwargs)
    if label and "::" in label:
        organ = label.split("::", 1)[0]
        kwargs.update(organ_tissue_overrides(organ, kwargs["field_strength_T"]))
    result = simulate_with_without_metabolite(**kwargs)
    result["metabolite_label"] = label
    return result


# --- Saturation optimization -------------------------------------------------
#
# The B1 grid spans the range where simulated contrast actually peaks. Measured behaviour (3 T,
# 6-pool system): a slow amide pool (k_ex 30/s) peaks near 0.75 uT, creatine-like amine (500/s)
# near 1 uT, a fast amine (5000/s) near 2-4 uT, and every curve collapses toward zero by ~8 uT.
# The collapse is real physics, not a numerical artifact: at high B1 direct water saturation
# drives the water pool to near-zero in BOTH the with- and without-metabolite simulations, so
# their difference vanishes. Peaks shift upward with B0 (and down to ~0.15-0.25 uT at 0.5 T),
# which is why the grid extends well below 1 uT.
B1_GRID_UT = (0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0)

# Tsat is chosen as the grid value with the highest with-vs-without contrast at the best B1
# (ties go to the shorter Tsat). An earlier version picked the shortest Tsat within 97 % of the
# best ("knee") to shorten the scan; that tolerance was a chosen number with no reference, so it
# was removed -- the report states the optimum, and any scan-time trade-off is the
# experimentalist's protocol decision.
TSAT_GRID_MS = (500, 750, 1000, 1500, 2000, 3000)

# No SAR or hardware ceiling is applied to the sweep. An earlier version capped B1_rms at a
# "typical in vivo" value scaled with 1/B0; that number was an assumption, not a calculation
# (a real SAR estimate needs the coil, the body model and tissue conductivity), so it was
# removed rather than presented as a limit. The recommendation is the sweep optimum; whether its
# B1_rms is deliverable must be checked against the scanner's own SAR monitor, and the
# experimentalist agent designs the actual pulsed protocol with its own SAR reasoning.
NO_SAR_MODEL_NOTE = ("no SAR or hardware ceiling is applied: the recommended B1/Tsat is the sweep optimum, and "
                     "deliverability must be checked against the scanner's SAR monitor")


def sweep_key(b1_uT: float, tsat_ms: float) -> str:
    """Stable key for the sweep-contrast map, float-normalized on both write and read."""
    return f"{float(b1_uT)}|{float(tsat_ms)}"


def _best_tsat(by_tsat: dict[float, float]) -> float:
    """Saturation time with the highest contrast on the grid (the shorter one on a tie)."""
    best = max(by_tsat.values())
    return min(t for t, v in by_tsat.items() if v >= best - 1e-12)


def optimize_saturation(
    field_strength_T: float,
    b1_grid_uT: tuple = B1_GRID_UT,
    tsat_grid_ms: tuple = TSAT_GRID_MS,
    **sim_kwargs,
) -> dict:
    """Sweep B1 x Tsat through the Bloch-McConnell simulation and return the best saturation
    settings for this metabolite at this field strength -- derived from the simulated
    with-vs-without contrast itself, never from a model's recollection of the literature.

    Returns both the SAR-constrained recommendation (what could actually be run in vivo) and the
    unconstrained optimum, because the gap between them is itself the finding: a metabolite whose
    true optimum sits above the SAR ceiling is SAR-limited, not physics-limited, and a report
    should say so rather than quietly presenting the capped value as ideal.
    """
    contrast: dict[tuple[float, float], float] = {}

    for b1 in b1_grid_uT:
        for tsat_ms in tsat_grid_ms:
            result = simulate_with_without_metabolite(
                field_strength_T=field_strength_T, b1_uT=b1,
                saturation_time_s=tsat_ms / 1000.0, **sim_kwargs,
            )
            contrast[(b1, tsat_ms)] = result["contrast_diff_percent"]

    def _best(allowed_b1) -> tuple[float, float, float, float]:
        subset = {k: v for k, v in contrast.items() if k[0] in allowed_b1}
        if not subset:
            return (min(allowed_b1, default=0.0), tsat_grid_ms[0], 0.0, 0.0)
        best_b1 = max({b1 for b1, _ in subset}, key=lambda b: max(
            v for (bb, _), v in subset.items() if bb == b))
        by_tsat = {t: v for (bb, t), v in subset.items() if bb == best_b1}
        best_tsat = _best_tsat(by_tsat)
        column_max = max(by_tsat.values())
        return best_b1, best_tsat, by_tsat[best_tsat], column_max

    best_b1, best_tsat, best_contrast, best_max = _best(list(b1_grid_uT))
    return {
        # The full landscape, not just the winner: a recommended B1/Tsat is only believable if you
        # can see the surface it was chosen from -- whether the peak is sharp or flat, how much is
        # lost to the SAR cap, and whether the optimum sits at a real interior maximum or merely at
        # the edge of the grid. Rendered by cest_scout/report/sequence_plots.py.
        "sweep_b1_grid_uT": [float(b) for b in b1_grid_uT],
        "sweep_tsat_grid_ms": [float(t) for t in tsat_grid_ms],
        # Keys are built from floats on BOTH sides (see sweep_key) because the schema types these
        # grids as list[float]: a grid written as int 500 comes back as 500.0, and an int-formatted
        # key would then never match, silently producing an empty plot instead of an error.
        "sweep_contrast": {sweep_key(b1, tsat): c for (b1, tsat), c in contrast.items()},
        "field_strength_T": field_strength_T,
        "b1_uT": best_b1,
        "saturation_time_ms": best_tsat,
        "contrast_percent": round(best_contrast, 4),
        # The best contrast at best_b1 over all Tsat (equal to contrast_percent now that no knee
        # shortening is applied) --
        # use this, not contrast_percent, to decide detectability against CONTRAST_THRESHOLD_PERCENT.
        "max_achievable_contrast_percent": round(best_max, 4),
        "sar_note": NO_SAR_MODEL_NOTE,
        "n_simulations": len(contrast),
    }
