"""Hand-calc validation for `ev_thermal.vehicle`'s longitudinal dynamics functions."""

from __future__ import annotations

import math

import pytest

from ev_thermal.vehicle import (
    STANDARD_GRAVITY_M_PER_S2,
    motor_angular_velocity_rad_per_s,
    motor_power_w,
    motor_torque_request_nm,
    road_load_force_n,
    wheel_force_from_torque_n,
    wheel_torque_from_motor_torque_nm,
    wheel_torque_nm,
)

_MASS_KG = 1800.0
_AIR_DENSITY_KG_PER_M3 = 1.225
_DRAG_COEFFICIENT = 0.28
_FRONTAL_AREA_M2 = 2.3
_CRR = 0.01
_WHEEL_RADIUS_M = 0.33
_GEAR_RATIO = 9.0
_DRIVELINE_EFFICIENCY = 0.95


def test_constant_speed_cruise_matches_hand_calc() -> None:
    speed_mps = 27.0  # ~97 km/h

    actual = road_load_force_n(
        mass_kg=_MASS_KG,
        speed_mps=speed_mps,
        acceleration_mps2=0.0,
        air_density_kg_per_m3=_AIR_DENSITY_KG_PER_M3,
        drag_coefficient=_DRAG_COEFFICIENT,
        frontal_area_m2=_FRONTAL_AREA_M2,
        rolling_resistance_coefficient=_CRR,
        grade_rad=0.0,
    )

    drag_n = 0.5 * _AIR_DENSITY_KG_PER_M3 * _DRAG_COEFFICIENT * _FRONTAL_AREA_M2 * speed_mps**2
    rolling_n = _CRR * _MASS_KG * STANDARD_GRAVITY_M_PER_S2
    expected = drag_n + rolling_n

    assert actual == expected
    assert abs(actual - expected) / expected < 0.001


def test_grade_adds_gravity_component() -> None:
    grade_rad = math.radians(5.0)

    flat = road_load_force_n(
        mass_kg=_MASS_KG,
        speed_mps=20.0,
        acceleration_mps2=0.0,
        air_density_kg_per_m3=_AIR_DENSITY_KG_PER_M3,
        drag_coefficient=_DRAG_COEFFICIENT,
        frontal_area_m2=_FRONTAL_AREA_M2,
        rolling_resistance_coefficient=_CRR,
        grade_rad=0.0,
    )
    graded = road_load_force_n(
        mass_kg=_MASS_KG,
        speed_mps=20.0,
        acceleration_mps2=0.0,
        air_density_kg_per_m3=_AIR_DENSITY_KG_PER_M3,
        drag_coefficient=_DRAG_COEFFICIENT,
        frontal_area_m2=_FRONTAL_AREA_M2,
        rolling_resistance_coefficient=_CRR,
        grade_rad=grade_rad,
    )

    expected_delta = _MASS_KG * STANDARD_GRAVITY_M_PER_S2 * math.sin(grade_rad) + _MASS_KG * (
        STANDARD_GRAVITY_M_PER_S2 * math.cos(grade_rad) - STANDARD_GRAVITY_M_PER_S2
    ) * _CRR
    assert abs((graded - flat) - expected_delta) < 1e-9


def test_acceleration_raises_road_load_by_exactly_ma() -> None:
    kwargs = dict(
        speed_mps=20.0,
        air_density_kg_per_m3=_AIR_DENSITY_KG_PER_M3,
        drag_coefficient=_DRAG_COEFFICIENT,
        frontal_area_m2=_FRONTAL_AREA_M2,
        rolling_resistance_coefficient=_CRR,
        grade_rad=0.0,
    )

    for mass_kg in (1200.0, 1800.0, 2400.0):
        cruising = road_load_force_n(mass_kg=mass_kg, acceleration_mps2=0.0, **kwargs)
        accelerating = road_load_force_n(mass_kg=mass_kg, acceleration_mps2=1.0, **kwargs)
        assert accelerating - cruising == pytest.approx(mass_kg * 1.0)


def test_motor_torque_request_divides_by_efficiency_when_motoring() -> None:
    force_n = road_load_force_n(
        mass_kg=_MASS_KG,
        speed_mps=20.0,
        acceleration_mps2=0.5,
        air_density_kg_per_m3=_AIR_DENSITY_KG_PER_M3,
        drag_coefficient=_DRAG_COEFFICIENT,
        frontal_area_m2=_FRONTAL_AREA_M2,
        rolling_resistance_coefficient=_CRR,
        grade_rad=0.0,
    )
    assert force_n > 0.0

    wheel_torque = wheel_torque_nm(force_n, _WHEEL_RADIUS_M)
    motor_torque = motor_torque_request_nm(wheel_torque, _GEAR_RATIO, _DRIVELINE_EFFICIENCY)

    expected = wheel_torque / (_GEAR_RATIO * _DRIVELINE_EFFICIENCY)
    assert motor_torque == expected
    assert abs(motor_torque) > abs(wheel_torque / _GEAR_RATIO)


def test_motor_torque_request_multiplies_by_efficiency_when_regen_braking() -> None:
    wheel_torque = -400.0
    motor_torque = motor_torque_request_nm(wheel_torque, _GEAR_RATIO, _DRIVELINE_EFFICIENCY)

    expected = wheel_torque * _DRIVELINE_EFFICIENCY / _GEAR_RATIO
    assert motor_torque == expected
    assert abs(motor_torque) < abs(wheel_torque / _GEAR_RATIO)


def test_motor_power_matches_torque_times_angular_velocity() -> None:
    speed_mps = 25.0
    omega = motor_angular_velocity_rad_per_s(speed_mps, _WHEEL_RADIUS_M, _GEAR_RATIO)
    torque_nm = 50.0

    assert motor_power_w(torque_nm, omega) == torque_nm * omega
    assert omega == (speed_mps / _WHEEL_RADIUS_M) * _GEAR_RATIO


def test_wheel_torque_from_motor_torque_round_trips_when_motoring() -> None:
    original_wheel_torque_nm = 400.0
    motor_torque_nm = motor_torque_request_nm(original_wheel_torque_nm, _GEAR_RATIO, _DRIVELINE_EFFICIENCY)
    recovered_wheel_torque_nm = wheel_torque_from_motor_torque_nm(motor_torque_nm, _GEAR_RATIO, _DRIVELINE_EFFICIENCY)

    assert recovered_wheel_torque_nm == pytest.approx(original_wheel_torque_nm)


def test_wheel_torque_from_motor_torque_round_trips_when_regen_braking() -> None:
    original_wheel_torque_nm = -300.0
    motor_torque_nm = motor_torque_request_nm(original_wheel_torque_nm, _GEAR_RATIO, _DRIVELINE_EFFICIENCY)
    recovered_wheel_torque_nm = wheel_torque_from_motor_torque_nm(motor_torque_nm, _GEAR_RATIO, _DRIVELINE_EFFICIENCY)

    assert recovered_wheel_torque_nm == pytest.approx(original_wheel_torque_nm)


def test_wheel_force_from_torque_is_inverse_of_wheel_torque() -> None:
    force_n = 500.0
    torque_nm = wheel_torque_nm(force_n, _WHEEL_RADIUS_M)

    assert wheel_force_from_torque_n(torque_nm, _WHEEL_RADIUS_M) == pytest.approx(force_n)
