"""Deterministic ppm -> Hz frequency-offset calculator.

The chemist agent should call this instead of computing Larmor offsets by
hand, so the numbers are exact rather than LLM arithmetic. Uses the same
proton gyromagnetic ratio as CEST_simulator/bloch_mcconnell.py so figures
agree with the physicist agent's simulation.
"""
from __future__ import annotations

from cest_scout.config import FIELD_STRENGTHS_T, GYRO_1H_MHZ_PER_T

TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "ppm_to_frequency_table",
        "description": (
            "Given a chemical-shift offset from water in ppm, compute the exact proton "
            "Larmor frequency and the saturation-offset frequency (Hz) at standard clinical/"
            "preclinical field strengths (0.5, 1.5, 3, 5, 7 T). Always use this instead of "
            "computing the Hz values yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ppm_shift": {"type": "number", "description": "Chemical shift relative to bulk water, in ppm."},
            },
            "required": ["ppm_shift"],
        },
    },
}


def ppm_to_frequency_table(ppm_shift: float) -> list[dict]:
    rows = []
    for field_t in FIELD_STRENGTHS_T:
        larmor_mhz = field_t * GYRO_1H_MHZ_PER_T
        offset_hz = ppm_shift * larmor_mhz  # ppm * MHz = Hz
        rows.append({
            "field_strength_T": field_t,
            "larmor_frequency_MHz": round(larmor_mhz, 4),
            "offset_from_water_Hz": round(offset_hz, 2),
        })
    return rows


def call(args: dict) -> dict:
    ppm_shift = float(args["ppm_shift"])
    return {"ppm_shift": ppm_shift, "table": ppm_to_frequency_table(ppm_shift)}
