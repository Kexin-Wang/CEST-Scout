"""Saturation-scheme (RF pulse train) diagrams for the sequences the agents recommend.

Draws the actual timing an agent proposed -- B1 amplitude, per-pulse duration, interpulse delay,
total Tsat, TR -- as a labeled time-domain schematic, so a claimed "optimized sequence" can be
read as a sequence diagram rather than only as prose. These are schematics of the saturation
module's envelope (pulse shape is annotated, not waveform-accurate) and deliberately show only
what the agent actually specified.
"""
from __future__ import annotations

import base64
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from cest_scout.skills.bloch_bridge import sweep_key


def _pulse_timing(b1_uT: float, saturation_time_ms: float, n_pulses: int,
                   pulse_duration_ms: float, interpulse_delay_ms: float) -> tuple[int, float, float]:
    """Fall back to a single continuous pulse spanning the whole saturation time when an agent
    left the pulse-train fields unset (older reports, or a continuous-wave recommendation), so a
    diagram is still drawn instead of a zero-width rectangle.
    """
    if n_pulses and n_pulses > 1 and pulse_duration_ms > 0:
        return int(n_pulses), float(pulse_duration_ms), float(max(interpulse_delay_ms, 0.0))
    duration = pulse_duration_ms if pulse_duration_ms > 0 else saturation_time_ms
    return 1, float(duration), 0.0


def _span(ax, x0: float, x1: float, y: float, tick: float, color: str, label: str) -> None:
    """A |----| span with a label above it, readable even when x1-x0 is a small fraction of the axis."""
    ax.plot([x0, x1], [y, y], color=color, lw=1)
    ax.plot([x0, x0], [y - tick, y + tick], color=color, lw=1)
    ax.plot([x1, x1], [y - tick, y + tick], color=color, lw=1)
    ax.text((x0 + x1) / 2, y + tick * 1.6, label, ha="center", fontsize=7.5, color=color)


def render_saturation_scheme_png_base64(
    title: str,
    b1_uT: float,
    saturation_time_ms: float,
    n_pulses: int = 1,
    pulse_duration_ms: float = 0.0,
    interpulse_delay_ms: float = 0.0,
    tr_ms: float | None = None,
    pulse_shape: str = "",
) -> str | None:
    """The timing as one row of drawn pulses; every number lives in the subtitle and a compact
    legend to the right of the axes, so no two labels can collide on the drawing."""
    if b1_uT <= 0 or saturation_time_ms <= 0:
        return None

    n, dur, gap = _pulse_timing(b1_uT, saturation_time_ms, n_pulses, pulse_duration_ms, interpulse_delay_ms)
    fig, ax = plt.subplots(figsize=(9.2, 2.7))

    t = 0.0
    for i in range(n):
        ax.add_patch(Rectangle((t, 0), dur, b1_uT, facecolor="#1baf7a", edgecolor="#0e7a53", lw=0.6))
        t += dur
        if i < n - 1:
            t += gap
    train_end = t

    # Readout block after the saturation module, so the diagram reads as a sequence, not just pulses.
    readout_width = max(train_end * 0.06, dur * 0.5)
    ax.add_patch(Rectangle((train_end, 0), readout_width, b1_uT * 0.45, facecolor="#b0b0b0", edgecolor="#555"))

    # Tsat span, measured from the pulse timing actually drawn. If that disagrees with the Tsat
    # the agent stated, say so on the figure rather than quietly showing one of the two numbers --
    # an internally inconsistent sequence is itself a finding.
    y_span = -b1_uT * 0.16
    _span(ax, 0, train_end, y_span, b1_uT * 0.03, "#333", f"Tsat {train_end:.0f} ms")
    warning = ""
    if saturation_time_ms > 0 and abs(train_end - saturation_time_ms) > 0.05 * saturation_time_ms:
        warning = f"stated Tsat {saturation_time_ms:g} ms does not match the pulse timing ({train_end:.0f} ms)"

    ax.axhline(b1_uT, color="#1baf7a", ls=":", lw=1, alpha=0.7)

    x_max = train_end + readout_width
    tr_line = None
    if tr_ms and tr_ms > 0:
        realizable = tr_ms > train_end
        tr_line = ax.axvline(tr_ms, color="#777" if realizable else "#c00", ls="--", lw=1.1)
        if not realizable:
            # A TR shorter than the saturation module itself cannot be played out on a scanner;
            # show it rather than silently drawing a plausible-looking sequence.
            warning = (warning + "; " if warning else "") + f"TR {tr_ms:g} ms < Tsat: not realizable"
        x_max = max(x_max, tr_ms)

    duty = 100.0 * (n * dur) / train_end if train_end > 0 else 100.0
    bits = [f"B$_1$ {b1_uT:g} µT"]
    bits.append(f"{n} × {dur:g} ms" + (f", {gap:g} ms gaps" if n > 1 and gap > 0 else "") + (f" ({duty:.0f}% duty)" if n > 1 else " CW"))
    bits.append(f"Tsat {train_end:.0f} ms")
    if tr_ms and tr_ms > 0:
        bits.append(f"TR {tr_ms:g} ms")
    shape_bit = f"{pulse_shape}: " if pulse_shape else ""
    ax.set_title(f"{title}\n{shape_bit}{' · '.join(bits)}", fontsize=9.5)

    handles = [Rectangle((0, 0), 1, 1, facecolor="#1baf7a", edgecolor="#0e7a53"),
               Rectangle((0, 0), 1, 1, facecolor="#b0b0b0", edgecolor="#555")]
    labels = ["saturation pulse", "readout"]
    if tr_line is not None:
        handles.append(tr_line)
        labels.append("TR")
    ax.legend(handles, labels, loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=False, fontsize=8)
    if warning:
        fig.text(0.01, 0.01, warning, fontsize=7.5, color="#c00", ha="left", va="bottom")

    ax.set_xlabel("Time (ms)")
    ax.set_ylabel("B$_1$ (µT)")
    ax.set_xlim(-x_max * 0.02, x_max * 1.04)
    ax.set_ylim(-b1_uT * 0.34, b1_uT * 1.2)
    ax.set_yticks([0, b1_uT])
    ax.grid(axis="x", alpha=0.2)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout(rect=(0, 0.04 if warning else 0, 1, 1))

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# Categorical slots of the validated default palette, in its fixed order.
_SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")


def render_roi_contrast_png_base64(organ_predictions: list, threshold_percent: float | None = None) -> str | None:
    """Predicted ROI-differential contrast of the verification scan: one bar per (ROI, metabolite)
    pair, grouped by ROI, error bars spanning the B1 -/+ inhomogeneity re-simulations. Pairs that
    were not simulated get no bar and an 'n/s' mark. Series legend in one row below the axes."""
    preds = [p for p in organ_predictions]
    if not preds:
        return None
    rois: list[str] = []
    mets: list[str] = []
    for p in preds:
        if p.organ not in rois:
            rois.append(p.organ)
        if p.metabolite_name not in mets:
            mets.append(p.metabolite_name)
    mets = mets[:8]
    by = {(p.organ, p.metabolite_name): p for p in preds}

    fig, ax = plt.subplots(figsize=(max(6.5, 1.6 * len(rois) + 2.5), 3.6))
    width = 0.8 / max(len(mets), 1)
    for j, met in enumerate(mets):
        xs, ys, lo, hi = [], [], [], []
        for i, roi in enumerate(rois):
            p = by.get((roi, met))
            x = i - 0.4 + width * (j + 0.5)
            if p is None or p.simulation_note.startswith("not simulated"):
                ax.text(x, 0.01, "n/s", ha="center", va="bottom", fontsize=6.5, color="#888",
                        transform=ax.get_xaxis_transform(), rotation=90)
                continue
            xs.append(x)
            ys.append(p.predicted_contrast_percent)
            rng = p.predicted_contrast_range_percent if len(p.predicted_contrast_range_percent) == 2 else [p.predicted_contrast_percent] * 2
            lo.append(max(p.predicted_contrast_percent - rng[0], 0.0))
            hi.append(max(rng[1] - p.predicted_contrast_percent, 0.0))
        if xs:
            ax.bar(xs, ys, width=width * 0.92, color=_SERIES[j % len(_SERIES)], label=_short(met), linewidth=0)
            ax.errorbar(xs, ys, yerr=[lo, hi], fmt="none", ecolor="#333", elinewidth=1, capsize=2)
    if threshold_percent:
        ax.axhline(threshold_percent, color="#888", ls=":", lw=1)
        ax.text(len(rois) - 0.5, threshold_percent, f" {threshold_percent:g}%", fontsize=7, color="#555", va="bottom", ha="right")
    ax.set_xticks(range(len(rois)))
    ax.set_xticklabels([_short(r, 22) for r in rois], fontsize=8.5)
    ax.set_ylabel("predicted contrast (%)")
    ax.set_title("Verification scan: predicted ROI-differential contrast (bars) and B$_1$ ±30% range (whiskers)", fontsize=9.5)
    ax.grid(axis="y", alpha=0.25)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=min(len(mets), 4), frameon=False, fontsize=8)
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _short(name: str, n: int = 30) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def render_optimization_landscape_png_base64(
    title: str,
    b1_grid_uT: list,
    tsat_grid_ms: list,
    contrast: dict,
    chosen_b1_uT: float,
    chosen_tsat_ms: float,
    sar_ceiling_uT: float = 0.0,          # legacy argument, ignored: no SAR ceiling is applied
    unconstrained_b1_uT: float | None = None,
) -> str | None:
    """Show the B1 x Tsat surface the recommended saturation settings were picked from.

    A bare "B1 = 0.75 uT, Tsat = 3000 ms" is unfalsifiable on its face. Plotting the sweep makes
    the choice checkable: whether contrast peaks at a real interior maximum or merely runs to the
    edge of the grid, how sharp that peak is, and how much contrast the SAR ceiling costs. The
    left panel is contrast vs B1 (one curve per Tsat), the right is contrast vs Tsat at the chosen
    B1; the chosen Tsat is the one with the highest contrast.
    """
    if not b1_grid_uT or not tsat_grid_ms or not contrast:
        return None

    def at(b1, tsat):
        return contrast.get(sweep_key(b1, tsat))

    fig, (ax_b1, ax_ts) = plt.subplots(1, 2, figsize=(11.5, 4.1))

    cmap = plt.get_cmap("viridis")
    for i, tsat in enumerate(tsat_grid_ms):
        ys = [at(b1, tsat) for b1 in b1_grid_uT]
        if any(y is None for y in ys):
            continue
        ax_b1.plot(b1_grid_uT, ys, marker="o", ms=3, lw=1.4,
                    color=cmap(i / max(len(tsat_grid_ms) - 1, 1)), label=f"{tsat:g} ms")

    # No SAR ceiling is drawn: none is applied (bloch_bridge.NO_SAR_MODEL_NOTE).
    chosen = at(chosen_b1_uT, chosen_tsat_ms)
    if chosen is not None:
        ax_b1.plot([chosen_b1_uT], [chosen], marker="*", ms=15, color="#c00", zorder=5,
                    label=f"chosen {chosen_b1_uT:g} µT")
    ax_b1.set_xlabel("B$_1$ (µT)")
    ax_b1.set_ylabel("with-vs-without contrast (%)")
    ax_b1.set_title("Contrast vs B$_1$ (one curve per saturation time)", fontsize=9)
    ax_b1.legend(fontsize=6.5, ncol=1, title="Tsat", title_fontsize=7, loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False)
    ax_b1.grid(alpha=0.25)

    ys = [at(chosen_b1_uT, t) for t in tsat_grid_ms]
    if not any(y is None for y in ys):
        ax_ts.plot(tsat_grid_ms, ys, marker="o", ms=4, lw=1.6, color="#2a6")
        if chosen is not None:
            ax_ts.plot([chosen_tsat_ms], [chosen], marker="*", ms=15, color="#c00", zorder=5)
    ax_ts.set_xlabel("Saturation time (ms)")
    ax_ts.set_ylabel("contrast (%)")
    ax_ts.set_title(f"Contrast vs T$_{{sat}}$ at the chosen B$_1$ = {chosen_b1_uT:g} µT", fontsize=9)
    ax_ts.grid(alpha=0.25)

    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=125)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")
