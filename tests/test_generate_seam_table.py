"""Tests for examples/generate_seam_table.py's own new logic -- the daily-
to-monthly SOH-loss aggregation, and an end-to-end smoke test that a real
row comes out well-formed. Does not re-test characterize_all_trips/
simulate_long_horizon themselves (already covered by tests/test_long_horizon.py).
"""

from __future__ import annotations

import pytest
from examples.generate_seam_table import _monthly_soh_deltas_pct, generate_blue_bird_columbus_row

from ev_thermal.schema import LongHorizonCheckpoint


def _make_checkpoint(day_index: int, battery_soh: float) -> LongHorizonCheckpoint:
    return LongHorizonCheckpoint(
        day_index=day_index,
        battery_soh=battery_soh,
        calendar_soh_loss_cumulative=1.0 - battery_soh,
        cycle_soh_loss_cumulative=0.0,
        battery_soc=0.8,
        pack_cell_temp_k=298.15,
        odometer_km=0.0,
    )


def test_monthly_soh_deltas_pct_matches_hand_computed_two_month_case() -> None:
    # 90 days (enough for month 2's own lookup, round(2*365/12)-1=60, to stay
    # well within range without hitting the end-of-data clamp) -- 1.0 -> 0.99 linearly.
    checkpoints = [_make_checkpoint(d, 1.0 - d * 0.0001) for d in range(90)]

    deltas = _monthly_soh_deltas_pct(checkpoints, duration_years=1)

    days_per_month = 365.0 / 12.0
    month1_day_index = round(1 * days_per_month) - 1
    month2_day_index = round(2 * days_per_month) - 1
    expected_month1_cum_pct = (1.0 - checkpoints[month1_day_index].battery_soh) * 100.0
    expected_month2_cum_pct = (1.0 - checkpoints[month2_day_index].battery_soh) * 100.0

    assert deltas[0] == pytest.approx(expected_month1_cum_pct)
    assert deltas[1] == pytest.approx(expected_month2_cum_pct - expected_month1_cum_pct)


def test_monthly_soh_deltas_pct_sums_to_total_loss_at_final_month() -> None:
    checkpoints = [_make_checkpoint(d, 1.0 - d * 0.0001) for d in range(400)]
    deltas = _monthly_soh_deltas_pct(checkpoints, duration_years=1)

    final_day_index = min(round(12 * (365.0 / 12.0)) - 1, len(checkpoints) - 1)
    expected_total_pct = (1.0 - checkpoints[final_day_index].battery_soh) * 100.0

    assert sum(deltas) == pytest.approx(expected_total_pct)
    assert len(deltas) == 12


def test_monthly_soh_deltas_pct_clamps_to_last_checkpoint_when_run_is_shorter() -> None:
    # Only 10 days of checkpoints available, but 1 year (12 months) requested --
    # every month past day 10 should clamp to the last checkpoint (no IndexError).
    checkpoints = [_make_checkpoint(d, 1.0 - d * 0.001) for d in range(10)]
    deltas = _monthly_soh_deltas_pct(checkpoints, duration_years=1)

    assert len(deltas) == 12
    assert sum(deltas) == pytest.approx((1.0 - checkpoints[-1].battery_soh) * 100.0)
    # Every month after the data runs out contributes exactly zero further loss.
    assert deltas[-1] == pytest.approx(0.0)


@pytest.mark.slow
def test_generate_blue_bird_columbus_row_is_well_formed() -> None:
    row = generate_blue_bird_columbus_row(duration_years=1)

    assert row.vehicle_class == "blue_bird_vision_electric"
    assert row.climate_zone == "columbus_oh"
    assert row.methodology == "point_estimate"
    assert len(row.monthly_soh_delta_pct) == 12
    assert all(v >= 0.0 for v in row.monthly_soh_delta_pct)  # SOH is monotonically non-increasing
    assert len(row.caveats) > 0
