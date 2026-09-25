"""Tests for `ev_thermal.aging`'s calendar/cycle SOH model and SOH-feedback helper.

The anchor-reproduction tests here are what actually locks in the calibrated
coefficients (see `aging.py`'s module docstring, "Coefficient provenance",
and the calibration reasoning behind each number) — if these coefficients
are ever changed without re-solving against the same anchors, these tests
are what catch it.
"""

from __future__ import annotations

from ev_thermal.aging import apply_soh_to_battery_spec, compute_aging_step
from tests.test_battery import _make_battery_spec

YEAR_S = 365.25 * 86400.0


def test_calendar_aging_matches_leaf_guam_anchor_within_one_percent() -> None:
    spec = _make_battery_spec()
    result = compute_aging_step(
        spec,
        soh=1.0,
        cell_temp_k=273.15 + 40.0,
        soc=0.80,
        applied_current_a=0.0,
        dt_s=5.0 * YEAR_S,
    )
    assert abs(result.new_soh - 0.70) < 0.01
    assert result.cycle_delta == 0.0


def test_calendar_aging_matches_model3_seattle_anchor_within_one_percent() -> None:
    spec = _make_battery_spec()
    result = compute_aging_step(
        spec,
        soh=1.0,
        cell_temp_k=273.15 + 20.0,
        soc=0.60,
        applied_current_a=0.0,
        dt_s=10.0 * YEAR_S,
    )
    assert abs(result.new_soh - 0.90) < 0.01
    assert result.cycle_delta == 0.0


def test_cycle_aging_matches_throughput_anchor_within_one_percent() -> None:
    """EFC-normalized version of the throughput anchor (see `docs/DESIGN_QUESTIONS.md`
    Q-0006/Q-0007): 100,000 kWh / 75 kWh assumed reference capacity = 1,333.33 EFC.
    Deliberately independent of the test fixture's own `capacity_nominal_ah`
    (180 Ah, unrelated to the 75 kWh reference) -- holding C-rate at 1.0 makes
    EFC accrual `dt_s / 3600` regardless of the spec's actual capacity, since
    `throughput_efc = (applied_current_a / capacity_ah) * dt_s / 3600 == C_rate
    * dt_s / 3600`.
    """
    spec = _make_battery_spec()
    capacity_ah = spec.capacity_nominal_ah.value
    target_efc = 100_000.0 / 75.0  # anchor's 100,000 kWh / assumed 75 kWh reference capacity
    applied_current_a = capacity_ah  # C-rate = 1.0, matching the calibration's reference C-rate
    dt_s = target_efc * 3600.0

    result = compute_aging_step(
        spec,
        soh=1.0,
        cell_temp_k=298.15,  # exactly reference_temp_k, matching the anchor's 25C condition
        soc=0.5,
        applied_current_a=applied_current_a,
        dt_s=dt_s,
    )
    assert abs(result.new_soh - 0.82) < 0.01
    assert result.calendar_delta > 0.0  # calendar aging still accrues over ~55.6 days of elapsed time


def test_combined_aging_delta_equals_calendar_plus_cycle() -> None:
    spec = _make_battery_spec()
    calendar_only = compute_aging_step(spec, soh=1.0, cell_temp_k=310.0, soc=0.7, applied_current_a=0.0, dt_s=3600.0)
    combined = compute_aging_step(
        spec, soh=1.0, cell_temp_k=310.0, soc=0.7, applied_current_a=spec.capacity_nominal_ah.value, dt_s=3600.0
    )

    assert combined.calendar_delta == calendar_only.calendar_delta
    assert combined.cycle_delta > 0.0
    assert combined.new_soh == 1.0 - combined.calendar_delta - combined.cycle_delta
    assert combined.new_soh < calendar_only.new_soh


def test_extrapolation_beyond_anchor_envelope_rate_is_bounded() -> None:
    """The calibration anchors span roughly 20-40C; 50C is genuine
    extrapolation (see aging.py's docstring). This doesn't validate the
    magnitude at 50C -- nothing anchors that -- it just guards against
    someone later "fixing" calendar_aging_activation_temp_k to a literature
    value and silently blowing up the extrapolated behavior far beyond what
    the calibrated anchors would suggest is plausible.
    """
    spec = _make_battery_spec()
    dt_s = 3600.0
    soc = 0.80

    at_40c = compute_aging_step(spec, soh=1.0, cell_temp_k=273.15 + 40.0, soc=soc, applied_current_a=0.0, dt_s=dt_s)
    at_50c = compute_aging_step(spec, soh=1.0, cell_temp_k=273.15 + 50.0, soc=soc, applied_current_a=0.0, dt_s=dt_s)

    assert at_50c.calendar_delta > at_40c.calendar_delta
    assert at_50c.calendar_delta < 3.0 * at_40c.calendar_delta


def test_apply_soh_to_battery_spec_scales_capacity_and_resistance() -> None:
    spec = _make_battery_spec()
    aged = apply_soh_to_battery_spec(spec, soh=0.8)

    assert aged.capacity_nominal_ah.value == spec.capacity_nominal_ah.value * 0.8
    expected_resistance_scale = 1.0 + spec.resistance_growth_per_soh_loss.value * 0.2
    for (aged_soc, aged_r0), (orig_soc, orig_r0) in zip(aged.r0_curve_ohm, spec.r0_curve_ohm, strict=True):
        assert aged_soc.value == orig_soc.value
        assert abs(aged_r0.value - orig_r0.value * expected_resistance_scale) < 1e-12

    # every other field is untouched
    assert aged.chemistry == spec.chemistry
    assert aged.r1_ohm.value == spec.r1_ohm.value
    assert aged.calendar_aging_rate_per_s.value == spec.calendar_aging_rate_per_s.value


def test_apply_soh_to_battery_spec_at_full_soh_is_identity_for_scaled_fields() -> None:
    spec = _make_battery_spec()
    aged = apply_soh_to_battery_spec(spec, soh=1.0)

    assert aged.capacity_nominal_ah.value == spec.capacity_nominal_ah.value
    for (_aged_soc, aged_r0), (_orig_soc, orig_r0) in zip(aged.r0_curve_ohm, spec.r0_curve_ohm, strict=True):
        assert aged_r0.value == orig_r0.value
