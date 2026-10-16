import math

from cest_scout.config import GYRO_1H_MHZ_PER_T
from cest_scout.skills.frequency_calc import ppm_to_frequency_table


def test_field_strengths_and_monotonic_larmor():
    rows = ppm_to_frequency_table(3.5)
    fields = [r["field_strength_T"] for r in rows]
    assert fields == [0.5, 1.5, 3.0, 5.0, 7.0]
    larmors = [r["larmor_frequency_MHz"] for r in rows]
    assert larmors == sorted(larmors)


def test_offset_hz_matches_manual_formula():
    rows = ppm_to_frequency_table(2.0)
    row_3t = next(r for r in rows if r["field_strength_T"] == 3.0)
    expected_hz = 2.0 * 3.0 * GYRO_1H_MHZ_PER_T
    assert math.isclose(row_3t["offset_from_water_Hz"], expected_hz, abs_tol=0.01)


def test_zero_ppm_gives_zero_offset():
    rows = ppm_to_frequency_table(0.0)
    assert all(r["offset_from_water_Hz"] == 0.0 for r in rows)
