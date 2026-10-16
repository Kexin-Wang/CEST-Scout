"""General N-pool Bloch-McConnell simulation engine.

Vendored unchanged from https://github.com/Kexin-Wang/CEST_simulator (bloch_mcconnell.py,
commit 16ab4d3, 2026-09-14, MIT License) so that CEST_Scout runs without the sibling repository.

This is a Python port of a MATLAB engine (``blochSolve`` / ``blochSimCurve``,
H. Zeng) for CEST/MT saturation-transfer simulation. It supports an
arbitrary number of exchange pools, each defined by (name, T1, T2, exchange
lifetime with water, chemical shift, relative proton concentration), and
arbitrary pulse trains built from RF/delay/crusher/return-to-equilibrium
segments.

Magnetization state vectors are ordered ``[x0..xN, y0..yN, z0..zN]`` with
pool 0 always being free water, matching the MATLAB convention.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np
from scipy.linalg import expm

# 1H gyromagnetic ratio, expressed as "MHz/T" so that
# 2*pi*field_T*GYRO_1H_MHZ_PER_T*shift_ppm gives an angular frequency in
# rad/s directly (the ppm value's implicit 1e-6 cancels the M in MHz).
GYRO_1H_MHZ_PER_T = 267.5154109126 / (2 * np.pi)

CRUSH = -1.0
EQUILIBRIUM = float("inf")


@dataclass(frozen=True)
class Pool:
    name: str
    t1: float  # s
    t2: float  # s
    lifetime: float  # s; mean residence time before exchanging into water. inf = no exchange.
    shift_ppm: float  # chemical shift relative to water, ppm
    concentration: float  # relative proton concentration (pool 0/water is the reference)
    lineshape: str | None = None  # set only on the semisolid MT-type pool (must be pools[2])
    rf_exposed: bool = True  # False for a flowing pool (e.g. blood) that arrives from outside
    # the RF coil/slab and is therefore not directly tipped by the saturation/excitation
    # pulses, only by relaxation and chemical exchange with the irradiated tissue pools.


@dataclass(frozen=True)
class PulseSegment:
    strength_hz: float
    phase_rad: float
    duration_s: float  # CRUSH (-1): crush transverse magnetization; EQUILIBRIUM (inf): reset to M0


def super_lorentzian_g(delta_hz: np.ndarray, t2_s: float, n_theta: int = 512) -> np.ndarray:
    """Super-Lorentzian MT absorption lineshape g(Delta, T2), in seconds.

    Standard closed-form used for semisolid/MT pools (Morrison & Henkelman
    1995; Yarnykh & Yablonskiy 2002). The integrand has an integrable
    singularity at the magic angle (3cos^2(theta) == 1); it is avoided here
    by flooring the denominator rather than by analytic continuation.
    """
    delta_hz = np.atleast_1d(np.asarray(delta_hz, dtype=float))
    theta = (np.arange(n_theta) + 0.5) / n_theta * (np.pi / 2)
    denom = np.abs(3.0 * np.cos(theta) ** 2 - 1.0)
    denom = np.maximum(denom, 1e-3)
    sin_theta = np.sin(theta)
    out = np.empty_like(delta_hz)
    for i, d in enumerate(delta_hz):
        integrand = (t2_s / denom) * np.exp(-2.0 * (2 * np.pi * d * t2_s / denom) ** 2) * sin_theta
        out[i] = np.sqrt(2.0 / np.pi) * np.trapezoid(integrand, theta)
    return out


def lorentzian_g(delta_hz: np.ndarray, t2_s: float) -> np.ndarray:
    delta_hz = np.atleast_1d(np.asarray(delta_hz, dtype=float))
    return (t2_s / np.pi) / (1.0 + (2 * np.pi * delta_hz * t2_s) ** 2)


_LINESHAPES: dict[str, Callable[[np.ndarray, float], np.ndarray]] = {
    "superlorentzian": super_lorentzian_g,
    "lorentzian": lorentzian_g,
}


def mt_saturation_rate(t2c_s: float, w1_rad_s: float, delta_w_rad_s: float, lineshape: str) -> float:
    """RF saturation rate of a semisolid MT pool: R_rfc = pi * w1^2 * g(Delta, T2c).

    This is the standard MT saturation-rate formula referenced (but not
    itself defined) as ``RF_MT`` in the source MATLAB.
    """
    g_fn = _LINESHAPES[lineshape]
    delta_hz = delta_w_rad_s / (2 * np.pi)
    g = float(g_fn(np.array([delta_hz]), t2c_s)[0])
    return np.pi * w1_rad_s ** 2 * g


def _equilibrium_vector(pools: Sequence[Pool]) -> np.ndarray:
    n = len(pools)
    conc = np.array([p.concentration for p in pools], dtype=float)
    return np.concatenate([np.zeros(2 * n), conc]) / conc[0]


def _exchange_matrix(pools: Sequence[Pool]) -> np.ndarray:
    n = len(pools)
    water = pools[0]
    other2water = np.array([1.0 / p.lifetime for p in pools[1:]])
    water2other = np.array([p.concentration / p.lifetime for p in pools[1:]]) / water.concentration
    block = np.zeros((n, n))
    block[0, 1:] = other2water
    block[1:, 0] = water2other
    exch = np.zeros((3 * n, 3 * n))
    for k in range(3):
        exch[k * n:(k + 1) * n, k * n:(k + 1) * n] = block
    exch -= np.diag(exch.sum(axis=0))
    return exch


def _relaxation_matrix(pools: Sequence[Pool]) -> np.ndarray:
    r1 = np.array([1.0 / p.t1 for p in pools])
    r2 = np.array([1.0 / p.t2 for p in pools])
    return -np.diag(np.concatenate([r2, r2, r1]))


def _chemical_shift_matrix(
    pools: Sequence[Pool], offset_ppm: float, field_t: float, gyro: float, no_rf: bool
) -> tuple[np.ndarray, np.ndarray]:
    """Off-resonance precession block.

    ``no_rf`` reproduces the source MATLAB's zero-power ("delay") branch,
    which decouples the segment from the current saturation offset by
    adding a large fictitious 1e6 ppm shift instead of subtracting the
    carrier offset. That convention is preserved here for pulse-train
    spacer segments; it is intentionally NOT used by ``evolve_free`` below,
    which needs ordinary physical free-precession during a readout window.
    """
    n = len(pools)
    shifts = np.array([p.shift_ppm for p in pools])
    if no_rf:
        w = 2 * np.pi * field_t * GYRO_1H_MHZ_PER_T * gyro * (shifts + 1e6)
    else:
        w = 2 * np.pi * field_t * GYRO_1H_MHZ_PER_T * gyro * (shifts - offset_ppm)
    mat = np.zeros((3 * n, 3 * n))
    for i in range(n):
        mat[i, n + i] = -w[i]
        mat[n + i, i] = w[i]
    return mat, w


def _pulse_matrix(pools: Sequence[Pool], strength_hz: float, phase_rad: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(pools)
    exposed = np.array([1.0 if p.rf_exposed else 0.0 for p in pools])
    x_amp = strength_hz * np.cos(phase_rad) * 2 * np.pi * exposed
    y_amp = strength_hz * np.sin(phase_rad) * 2 * np.pi * exposed
    mat = np.zeros((3 * n, 3 * n))
    for i in range(n):
        mat[i, 2 * n + i] = y_amp[i]
        mat[n + i, 2 * n + i] = -x_amp[i]
        mat[2 * n + i, i] = -y_amp[i]
        mat[2 * n + i, n + i] = x_amp[i]
    return mat, x_amp, y_amp


def solve(
    pools: Sequence[Pool],
    offset_ppm: float,
    pulse_train: Sequence[PulseSegment],
    field_t: float = 3.0,
    gyro: float = 1.0,
    y_init: np.ndarray | None = None,
    n_repeats: int = 1,
) -> np.ndarray:
    """Propagate the N-pool magnetization through a pulse train.

    ``pulse_train`` is a flat sequence of segments (the two-level
    pulse-cell-of-pulses nesting in the MATLAB source is flattened here;
    the propagation order and physics are identical). Returns the final
    state vector ``[x0..xN, y0..yN, z0..zN]``.
    """
    n = len(pools)
    y0 = _equilibrium_vector(pools)
    y = np.array(y_init, dtype=float) if y_init is not None and len(y_init) == 3 * n else y0.copy()

    exch = _exchange_matrix(pools)
    rmat = _relaxation_matrix(pools)
    crusher = np.diag(np.concatenate([np.zeros(2 * n), np.ones(n)]))

    mt_pool_idx = 2 if (n > 2 and pools[2].lineshape) else None

    for _ in range(n_repeats):
        for seg in pulse_train:
            t = seg.duration_s
            if t == EQUILIBRIUM:
                y = y0.copy()
                continue
            if t == CRUSH:
                y = crusher @ y
                continue

            no_rf = seg.strength_hz == 0.0
            cs_mat, w = _chemical_shift_matrix(pools, offset_ppm, field_t, gyro, no_rf)
            pulse_mat, x_amp, y_amp = _pulse_matrix(pools, seg.strength_hz, seg.phase_rad)
            evol = exch + rmat + cs_mat + pulse_mat

            if mt_pool_idx is not None:
                i = mt_pool_idx
                w1 = float(np.hypot(x_amp[i], y_amp[i]))
                mtc_rf = mt_saturation_rate(pools[i].t2, w1, w[i], pools[i].lineshape)
                evol[2 * n + i, 2 * n + i] -= mtc_rf

            yp = np.linalg.solve(evol, -rmat @ y0)
            trans = expm(evol * t)
            y = trans @ (y + yp) - yp

    return y


def z_spectrum(
    offsets_ppm: np.ndarray,
    pools: Sequence[Pool],
    pulse_train: Sequence[PulseSegment],
    field_t: float = 3.0,
    gyro: float = 1.0,
    y_init: np.ndarray | None = None,
    n_repeats: int = 1,
) -> np.ndarray:
    """Water-pool Mz across a set of saturation offsets (ppm)."""
    n = len(pools)
    water_z_idx = 2 * n
    z = np.empty(len(offsets_ppm), dtype=float)
    for i, off in enumerate(offsets_ppm):
        y = solve(pools, float(off), pulse_train, field_t, gyro, y_init, n_repeats)
        z[i] = y[water_z_idx]
    return z


def evolve_free(
    pools: Sequence[Pool],
    offset_ppm: float,
    y_state: np.ndarray,
    field_t: float,
    gyro: float,
    dt: float,
    n_steps: int,
) -> np.ndarray:
    """Continue propagating a magnetization state through RF-off intervals.

    Models the physics of a readout window: relaxation, exchange (including
    with the blood pool, i.e. flow), and off-resonance precession continue
    to act while k-space is being sampled. Unlike a zero-power pulse-train
    spacer, this uses ordinary chemical-shift precession at the true
    saturation offset (see ``_chemical_shift_matrix`` docstring), since it
    represents real time passing during acquisition rather than an
    idealized inter-pulse gap.

    Returns the water-pool Mz after each of ``n_steps`` sub-intervals of
    duration ``dt``, shape ``(n_steps,)``.
    """
    n = len(pools)
    y0 = _equilibrium_vector(pools)
    exch = _exchange_matrix(pools)
    rmat = _relaxation_matrix(pools)
    cs_mat, _ = _chemical_shift_matrix(pools, offset_ppm, field_t, gyro, no_rf=False)
    evol = exch + rmat + cs_mat
    yp = np.linalg.solve(evol, -rmat @ y0)
    trans = expm(evol * dt)

    y = np.array(y_state, dtype=float)
    water_z_idx = 2 * n
    out = np.empty(n_steps, dtype=float)
    for k in range(n_steps):
        y = trans @ (y + yp) - yp
        out[k] = y[water_z_idx]
    return out
