"""Behavioral tests for `ev_thermal.controller`'s arbitration layer.

Reuses the motor/inverter/battery/cooling fixtures from their own test
modules so the limit magnitudes here stay consistent with what those
modules' own tests already exercise.
"""

from __future__ import annotations

from ev_thermal.battery import available_discharge_current_a
from ev_thermal.controller import step_controller
from ev_thermal.motor import available_torque_nm
from ev_thermal.schema import Confidence, Sourced, VehicleDynamics
from tests.test_battery import _make_battery_spec
from tests.test_cooling import _make_cooling_loop
from tests.test_inverter import _make_inverter_spec
from tests.test_motor import _make_motor_spec

_NOMINAL_TEMP_K = 298.15
_BATTERY_VOLTAGE_V = 350.0


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="placeholder")


def _make_vehicle_dynamics() -> VehicleDynamics:
    return VehicleDynamics(
        curb_mass_kg=_sourced(1800.0),
        drag_coefficient=_sourced(0.28),
        frontal_area_m2=_sourced(2.3),
        rolling_resistance_coefficient=_sourced(0.01),
        wheel_radius_m=_sourced(0.33),
        gear_ratio=_sourced(9.0),
        driveline_efficiency=_sourced(0.95),
        tire_road_friction_coefficient=_sourced(0.9),
    )


def _step(
    driver_torque_request_nm: float = 100.0,
    motor_winding_temp_k: float = _NOMINAL_TEMP_K,
    inverter_junction_temp_k: float = _NOMINAL_TEMP_K,
    battery_soc: float = 0.5,
    battery_cell_temp_k: float = _NOMINAL_TEMP_K,
    battery_v_rc_v: float = 0.0,
    battery_voltage_v: float = _BATTERY_VOLTAGE_V,
    speed_rad_per_s: float = 100.0,
    coolant_temp_k: float = _NOMINAL_TEMP_K,
):
    return step_controller(
        driver_torque_request_nm,
        _make_motor_spec(),
        _make_inverter_spec(),
        _make_battery_spec(),
        _make_vehicle_dynamics(),
        _make_cooling_loop(),
        motor_winding_temp_k,
        inverter_junction_temp_k,
        battery_soc,
        battery_cell_temp_k,
        battery_v_rc_v,
        battery_voltage_v,
        speed_rad_per_s,
        coolant_temp_k,
        sim_time_s=0.0,
    )


def test_hot_battery_and_cold_motor_battery_limit_dominates() -> None:
    result = _step(
        driver_torque_request_nm=280.0,
        motor_winding_temp_k=_NOMINAL_TEMP_K,
        battery_cell_temp_k=330.0,
        speed_rad_per_s=150.0,
    )

    motor_ceiling_nm = available_torque_nm(_make_motor_spec(), _NOMINAL_TEMP_K)
    assert result.commanded_torque_nm < motor_ceiling_nm
    assert result.commanded_torque_nm < 280.0
    assert result.causal_events
    assert all(e.event_type == "BATTERY_LIMIT_TRIGGERED" for e in result.causal_events)


def test_nominal_conditions_command_equals_request() -> None:
    result = _step(driver_torque_request_nm=100.0)

    assert result.commanded_torque_nm == 100.0
    assert result.causal_events == []


def test_soc_floor_hit_tapers_commanded_torque_to_zero_with_event() -> None:
    battery_spec = _make_battery_spec()
    result = _step(driver_torque_request_nm=200.0, battery_soc=battery_spec.soc_floor.value)

    assert result.commanded_torque_nm == 0.0
    assert any(e.event_type == "BATTERY_LIMIT_TRIGGERED" for e in result.causal_events)
    assert any("SOC floor" in e.description for e in result.causal_events)


def test_each_active_limit_produces_a_distinct_causal_event() -> None:
    motor_spec = _make_motor_spec()
    inverter_spec = _make_inverter_spec()
    motor_derate_temp_k = motor_spec.thermal_node.derate_temp_k.value
    inverter_derate_temp_k = inverter_spec.thermal_node.derate_temp_k.value

    result = _step(
        driver_torque_request_nm=300.0,
        motor_winding_temp_k=motor_derate_temp_k + 10.0,
        inverter_junction_temp_k=inverter_derate_temp_k + 10.0,
        speed_rad_per_s=400.0,
    )

    event_types = {e.event_type for e in result.causal_events}
    assert "MOTOR_LIMIT_TRIGGERED" in event_types
    assert "INVERTER_LIMIT_TRIGGERED" in event_types
    assert len(result.causal_events) == len(event_types)  # each type appears once


def test_pump_and_fan_duty_rise_with_component_and_coolant_temp() -> None:
    baseline = _step(battery_cell_temp_k=_NOMINAL_TEMP_K, coolant_temp_k=_NOMINAL_TEMP_K)
    hot_battery = _step(battery_cell_temp_k=312.0, coolant_temp_k=_NOMINAL_TEMP_K)
    hot_coolant = _step(battery_cell_temp_k=_NOMINAL_TEMP_K, coolant_temp_k=312.0)

    assert hot_battery.commanded_pump_duty > baseline.commanded_pump_duty
    assert hot_coolant.commanded_fan_duty > baseline.commanded_fan_duty


def test_no_limits_and_no_heat_gives_minimum_nonzero_duty() -> None:
    result = _step(
        battery_cell_temp_k=_NOMINAL_TEMP_K,
        motor_winding_temp_k=_NOMINAL_TEMP_K,
        inverter_junction_temp_k=_NOMINAL_TEMP_K,
        coolant_temp_k=_NOMINAL_TEMP_K,
    )

    assert result.commanded_pump_duty > 0.0
    assert result.commanded_fan_duty > 0.0


def test_battery_current_demand_matches_commanded_torque_direction() -> None:
    motoring = _step(driver_torque_request_nm=100.0, speed_rad_per_s=100.0)
    regen = _step(driver_torque_request_nm=-100.0, speed_rad_per_s=100.0)

    assert motoring.battery_current_demand_a > 0.0
    assert regen.battery_current_demand_a < 0.0


def test_battery_ceiling_matches_available_discharge_current_a_via_power_conversion() -> None:
    battery_spec = _make_battery_spec()
    speed_rad_per_s = 150.0
    result = _step(driver_torque_request_nm=1000.0, battery_cell_temp_k=330.0, speed_rad_per_s=speed_rad_per_s)

    expected_current_ceiling_a = available_discharge_current_a(battery_spec, 0.5, 330.0, 0.0)
    expected_torque_ceiling_nm = (expected_current_ceiling_a * _BATTERY_VOLTAGE_V) / speed_rad_per_s
    assert abs(result.commanded_torque_nm - expected_torque_ceiling_nm) < 1e-6
