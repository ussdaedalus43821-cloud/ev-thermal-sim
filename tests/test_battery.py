"""Behavioral tests for `ev_thermal.battery`'s single-RC-pair equivalent-circuit model.

Fixture curves/coefficients (OCV shape, R0 shape, Arrhenius activation temps,
RC pair) are physically-plausible placeholders, not sourced chemistry data —
see the TODO(spec-sourcing) markers on the corresponding `BatterySpec` fields
in schema.py.
"""

from __future__ import annotations

import pytest

from ev_thermal.battery import BatteryStabilityError, available_charge_current_a, step_battery
from ev_thermal.schema import BatterySpec, Confidence, Sourced, ThermalNode

_COOLANT_TEMP_K = 298.15
_AMBIENT_TEMP_K = 298.15
_REFERENCE_TEMP_K = 298.15
_CAPACITY_AH = 180.0
_TAU_RC_S = 0.015 * 3000.0  # r1_ohm * c1_farad


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="placeholder")


def _curve(pairs: list[tuple[float, float]]) -> list[tuple[Sourced[float], Sourced[float]]]:
    return [(_sourced(x), _sourced(y)) for x, y in pairs]


def _make_battery_spec() -> BatterySpec:
    node = ThermalNode(
        name="battery_pack",
        capacitance_j_per_k=_sourced(400_000.0),
        resistance_to_coolant_k_per_w=_sourced(0.02),
        initial_temp_k=298.15,
        max_temp_k=_sourced(333.15),
        derate_temp_k=_sourced(318.15),
    )
    return BatterySpec(
        chemistry="NMC",
        capacity_kwh_nominal=_sourced(62.0),
        capacity_kwh_usable=_sourced(58.0),
        nominal_voltage_v=_sourced(350.0),
        internal_resistance_ohm=_sourced(0.05),
        mass_kg=_sourced(450.0),
        specific_heat_j_per_kg_k=_sourced(900.0),
        max_charge_c_rate=_sourced(1.5),
        max_discharge_c_rate=_sourced(3.0),
        thermal_node=node,
        capacity_nominal_ah=_sourced(_CAPACITY_AH),
        reference_temp_k=_sourced(_REFERENCE_TEMP_K),
        ocv_curve_v=_curve(
            [
                (0.0, 300.0),
                (0.05, 320.0),
                (0.1, 330.0),
                (0.2, 338.0),
                (0.3, 342.0),
                (0.5, 348.0),
                (0.7, 354.0),
                (0.8, 358.0),
                (0.9, 365.0),
                (0.95, 375.0),
                (1.0, 395.0),
            ]
        ),
        docv_dt_v_per_k=_sourced(0.0),
        r0_curve_ohm=_curve(
            [
                (0.0, 0.09),
                (0.1, 0.07),
                (0.3, 0.055),
                (0.5, 0.05),
                (0.7, 0.055),
                (0.9, 0.07),
                (1.0, 0.10),
            ]
        ),
        r0_arrhenius_activation_temp_k=_sourced(3000.0),
        r1_ohm=_sourced(0.015),
        c1_farad=_sourced(3000.0),
        min_terminal_voltage_v=_sourced(290.0),
        max_terminal_voltage_v=_sourced(410.0),
        min_operating_temp_k=_sourced(253.15),
        cold_derate_threshold_temp_k=_sourced(283.15),
        cold_derate_activation_temp_k=_sourced(1000.0),
        soc_floor=_sourced(0.05),
        soc_floor_full_available_soc=_sourced(0.1),
        calendar_aging_rate_per_s=_sourced(4.4135e-10),
        calendar_aging_activation_temp_k=_sourced(7457.4),
        calendar_aging_soc_sensitivity=_sourced(1.0),
        cycle_aging_rate_per_efc=_sourced(1.350e-4),
        cycle_aging_activation_temp_k=_sourced(3728.7),
        cycle_aging_c_rate_sensitivity=_sourced(1.0),
        resistance_growth_per_soh_loss=_sourced(1.0),
        cold_soak_offset_k=_sourced(3.0),
        hot_soak_offset_k=_sourced(8.0),
        charge_current_limit_curve=_curve([(0.0, 1.0), (0.6, 1.0), (0.95, 0.2), (1.0, 0.2)]),
    )


def _step(
    spec: BatterySpec,
    current_request_a: float,
    soc: float,
    cell_temp_k: float,
    v_rc_v: float = 0.0,
    dt_s: float = 1.0,
):
    return step_battery(
        spec,
        current_request_a,
        soc,
        cell_temp_k,
        v_rc_v,
        coolant_temp_k=_COOLANT_TEMP_K,
        coolant_flow_l_per_min=10.0,
        ambient_temp_k=_AMBIENT_TEMP_K,
        dt_s=dt_s,
        sim_time_s=0.0,
    )


def test_ocv_lookup_is_monotonic_in_soc_and_deterministic() -> None:
    spec = _make_battery_spec()
    soc_points = [0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0]

    ocv_values = [_step(spec, 0.0, soc, _REFERENCE_TEMP_K).ocv_v for soc in soc_points]
    assert all(a < b for a, b in zip(ocv_values, ocv_values[1:], strict=False))

    repeat_a = _step(spec, 0.0, 0.5, _REFERENCE_TEMP_K)
    repeat_b = _step(spec, 0.0, 0.5, _REFERENCE_TEMP_K)
    assert repeat_a.ocv_v == repeat_b.ocv_v


def test_discharge_at_constant_current_soc_decreases_linearly() -> None:
    spec = _make_battery_spec()
    current_a = 50.0
    dt_s = 10.0
    steps = 100
    soc = 0.8
    v_rc_v = 0.0

    for _ in range(steps):
        result = _step(spec, current_a, soc, _REFERENCE_TEMP_K, v_rc_v, dt_s=dt_s)
        soc, v_rc_v = result.soc, result.v_rc_v

    expected_delta = -current_a * (steps * dt_s) / (3600.0 * _CAPACITY_AH)
    actual_delta = soc - 0.8
    assert abs(actual_delta - expected_delta) / abs(expected_delta) < 0.001


def test_charge_increases_soc_and_terminal_voltage_exceeds_ocv() -> None:
    spec = _make_battery_spec()
    result = _step(spec, -50.0, 0.5, _REFERENCE_TEMP_K)

    assert result.soc > 0.5
    assert result.terminal_voltage_v > result.ocv_v


def test_higher_current_produces_more_heat_with_i_squared_scaling() -> None:
    spec = _make_battery_spec()
    low = _step(spec, 50.0, 0.5, _REFERENCE_TEMP_K)
    high = _step(spec, 150.0, 0.5, _REFERENCE_TEMP_K)

    expected = low.irreversible_heat_w * (150.0 / 50.0) ** 2
    assert abs(high.irreversible_heat_w - expected) / expected < 0.01


def test_cold_cell_has_measurably_higher_r0_than_warm_cell() -> None:
    spec = _make_battery_spec()
    current_a = 50.0
    soc = 0.5

    cold = _step(spec, current_a, soc, 273.15)
    warm = _step(spec, current_a, soc, _REFERENCE_TEMP_K)

    cold_sag_v = cold.ocv_v - cold.terminal_voltage_v
    warm_sag_v = warm.ocv_v - warm.terminal_voltage_v
    assert cold_sag_v > warm_sag_v


def test_charge_at_0c_is_limited_more_than_charge_at_25c() -> None:
    spec = _make_battery_spec()
    requested_a = -1000.0
    soc = 0.5

    cold = _step(spec, requested_a, soc, 273.15)
    warm = _step(spec, requested_a, soc, _REFERENCE_TEMP_K)

    assert abs(cold.applied_current_a) < abs(warm.applied_current_a)


def test_available_charge_current_a_unrestricted_below_soc_curve_knee() -> None:
    spec = _make_battery_spec()
    below_knee = available_charge_current_a(spec, soc=0.5, cell_temp_k=_REFERENCE_TEMP_K, v_rc_v=0.0)
    at_zero = available_charge_current_a(spec, soc=0.0, cell_temp_k=_REFERENCE_TEMP_K, v_rc_v=0.0)

    assert below_knee == pytest.approx(at_zero)


def test_available_charge_current_a_throttles_above_soc_curve_knee() -> None:
    spec = _make_battery_spec()
    below_knee = available_charge_current_a(spec, soc=0.5, cell_temp_k=_REFERENCE_TEMP_K, v_rc_v=0.0)
    near_full = available_charge_current_a(spec, soc=0.97, cell_temp_k=_REFERENCE_TEMP_K, v_rc_v=0.0)

    assert near_full < below_knee
    # placeholder curve: scale=0.2 at soc=0.97 vs scale=1.0 below the knee
    assert near_full / below_knee == pytest.approx(0.2, abs=0.01)


def test_charge_current_throttled_at_high_soc_end_to_end() -> None:
    spec = _make_battery_spec()
    requested_a = -1000.0

    below_knee = _step(spec, requested_a, 0.5, _REFERENCE_TEMP_K)
    near_full = _step(spec, requested_a, 0.97, _REFERENCE_TEMP_K)

    assert abs(near_full.applied_current_a) < abs(below_knee.applied_current_a)


def test_regen_current_also_throttled_at_high_soc() -> None:
    # Milestone E2e: charge_current_limit_curve is a general BMS ceiling, not
    # fast-charge-specific -- it must throttle regen (a much smaller charge
    # current than fast-charge, but the same underlying cell-protection
    # physics) at high SOC too, same as real EVs' reduced-regen behavior.
    spec = _make_battery_spec()
    # Below both SOC points' ceilings pre-E2e (270A/54A here), so this current
    # is unclamped at soc=0.5 but clamped at soc=0.97 -- isolates the new
    # curve's effect rather than saturating both points identically.
    modest_regen_a = -100.0

    below_knee = _step(spec, modest_regen_a, 0.5, _REFERENCE_TEMP_K)
    near_full = _step(spec, modest_regen_a, 0.97, _REFERENCE_TEMP_K)

    assert below_knee.applied_current_a == pytest.approx(modest_regen_a)
    assert abs(near_full.applied_current_a) < abs(below_knee.applied_current_a)


def test_soc_floor_limits_discharge_and_logs_causal_event() -> None:
    spec = _make_battery_spec()
    soc_floor = spec.soc_floor.value
    soc_full_available = spec.soc_floor_full_available_soc.value
    mid_soc = (soc_floor + soc_full_available) / 2.0

    near_floor = _step(spec, 300.0, mid_soc, _REFERENCE_TEMP_K)
    above_floor = _step(spec, 300.0, 0.5, _REFERENCE_TEMP_K)

    assert near_floor.applied_current_a < 300.0
    assert near_floor.applied_current_a < above_floor.applied_current_a
    assert any(e.event_type == "SOC_FLOOR_LIMIT_TRIGGERED" for e in near_floor.causal_events)


def test_undervoltage_clamp_triggers_causal_event() -> None:
    spec = _make_battery_spec()
    result = _step(spec, 200.0, 0.0, _REFERENCE_TEMP_K)

    assert result.applied_current_a < 200.0
    assert any(e.event_type == "VOLTAGE_LIMIT_TRIGGERED" for e in result.causal_events)


def test_overvoltage_clamp_triggers_causal_event() -> None:
    spec = _make_battery_spec()
    result = _step(spec, -250.0, 0.99, _REFERENCE_TEMP_K)

    assert abs(result.applied_current_a) < 250.0
    assert any(e.event_type == "VOLTAGE_LIMIT_TRIGGERED" for e in result.causal_events)


def test_over_temperature_clamp_triggers_causal_event() -> None:
    spec = _make_battery_spec()
    derate_temp_k = spec.thermal_node.derate_temp_k.value
    # 400A comfortably exceeds the C-rate/voltage ceilings' derated value at
    # this temp (but not their un-derated value), isolating the thermal derate.
    result = _step(spec, 400.0, 0.5, derate_temp_k + 5.0)

    assert result.applied_current_a < 400.0
    assert any(e.event_type == "DERATE_TRIGGERED" for e in result.causal_events)


def test_energy_balance_holds_after_rc_settling() -> None:
    spec = _make_battery_spec()
    dt_s = 0.5
    current_a = 50.0
    soc = 0.5
    v_rc_v = 0.0
    cell_temp_k = _REFERENCE_TEMP_K

    settling_steps = round(20 * _TAU_RC_S / dt_s)
    for _ in range(settling_steps):
        result = _step(spec, current_a, soc, cell_temp_k, v_rc_v, dt_s=dt_s)
        soc, v_rc_v = result.soc, result.v_rc_v

    accumulation_steps = round(10 * _TAU_RC_S / dt_s)
    electrical_energy_j = 0.0
    chemical_energy_j = 0.0
    loss_energy_j = 0.0
    for _ in range(accumulation_steps):
        result = _step(spec, current_a, soc, cell_temp_k, v_rc_v, dt_s=dt_s)
        electrical_energy_j += result.terminal_voltage_v * current_a * dt_s
        chemical_energy_j += result.ocv_v * current_a * dt_s
        loss_energy_j += result.irreversible_heat_w * dt_s
        soc, v_rc_v = result.soc, result.v_rc_v

    predicted_electrical_energy_j = chemical_energy_j - loss_energy_j
    relative_error = abs(electrical_energy_j - predicted_electrical_energy_j) / abs(electrical_energy_j)
    assert relative_error < 1e-4


def test_stability_check_raises_when_dt_exceeds_rc_time_constant() -> None:
    spec = _make_battery_spec()

    with pytest.raises(BatteryStabilityError):
        _step(spec, 50.0, 0.5, _REFERENCE_TEMP_K, dt_s=_TAU_RC_S * 2)
