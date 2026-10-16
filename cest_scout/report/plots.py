"""Z-spectrum plots for the deterministic multi-pool simulation.

Rendered directly from the ZSpectrumCurve data the pipeline recorded from its own Bloch-McConnell
calls -- never from any model's prose transcription of those numbers. One panel per field
strength, one shared legend for the whole figure (with / without the metabolite), and the pool's
offset marked on each panel: downfield (positive ppm) for chemical exchange, upfield (negative
ppm) for an rNOE pool.
"""
from __future__ import annotations

import base64
from io import BytesIO

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from cest_scout.schemas import ZSpectrumCurve

# Okabe-Ito blue and vermillion (colour-blind safe), with solid vs dashed line style as well.
WITH_COLOR = "#0072B2"
WITHOUT_COLOR = "#D55E00"
MARK_COLOR = "#52514e"


def _short(name: str, n: int = 28) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def render_zspectra_png_base64(metabolite_name: str, curves: list[ZSpectrumCurve]) -> str | None:
    if not curves:
        return None
    n = len(curves)
    ncols = min(n, 3)
    nrows = -(-n // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.3 * ncols, 3.3 * nrows + 0.6), squeeze=False)

    handles = None
    for i, curve in enumerate(curves):
        ax = axes[i // ncols][i % ncols]
        h1, = ax.plot(curve.offsets_ppm, curve.z_with_metabolite, color=WITH_COLOR, linewidth=2.0)
        h2, = ax.plot(curve.offsets_ppm, curve.z_without_metabolite, color=WITHOUT_COLOR, linewidth=2.0, linestyle="--")
        handles = (h1, h2)
        if curve.shift_ppm:
            ax.axvline(curve.shift_ppm, color=MARK_COLOR, lw=1, ls=":")
            side = "rNOE" if curve.shift_ppm < 0 else "pool"
            ax.text(curve.shift_ppm, 0.02, f" {side} {curve.shift_ppm:+.1f} ppm", fontsize=7, color=MARK_COLOR,
                    transform=ax.get_xaxis_transform(), ha="left" if curve.shift_ppm < 0 else "right", va="bottom")
        ax.invert_xaxis()
        ax.set_xlabel("offset (ppm)")
        if i % ncols == 0:
            ax.set_ylabel("Z = Mz/M0")
        letter = chr(ord("A") + i)
        ax.set_title(f"{letter}   {curve.field_strength_T:g} T · Δ = {curve.contrast_diff_percent:.2f}%", fontsize=10, loc="left")
        ax.grid(alpha=0.25)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)

    for j in range(n, nrows * ncols):
        axes[j // ncols][j % ncols].axis("off")

    fig.suptitle(f"Z-spectra with / without {_short(metabolite_name, 50)}", fontsize=11)
    if handles:
        fig.legend(handles, ["with", "without"], loc="lower center", ncol=2, frameon=False, fontsize=8.5)
    # Room for the shared legend below and the title above, so panel rows never overlap.
    fig.tight_layout(rect=(0, 0.07, 1, 0.95))

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=140)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")
