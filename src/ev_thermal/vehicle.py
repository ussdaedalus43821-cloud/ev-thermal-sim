"""Longitudinal vehicle dynamics: pure functions converting driving state (speed,
acceleration, road grade) and vehicle-spec scalars into road-load force, wheel
torque, motor torque request, and motor power.

No state, no schema I/O: every function takes plain float arguments (SI units)
and returns a plain float. Callers (`sim.py`, eventually) pull the underlying
values out of `VehicleDynamics`' `Sourced[float]` fields before calling in here.
Grade is passed as an angle in radians, not a percent-grade fraction.
"""

from __future__ import annotations

import math

STANDARD_GRAVITY_M_PER_S2 = 9.80665


def inertial_force_n(mass_kg: float, acceleration_mps2: float) -> float:
    """F = ma: force needed to accelerate the vehicle's mass."""
    return mass_kg * acceleration_mps2


def aerodynamic_drag_force_n(
    speed_mps: float,
    air_density_kg_per_m3: float,
    drag_coefficient: float,
    frontal_area_m2: float,
) -> float:
    """F = 1/2 * rho * Cd * A * v^2."""
    return 0.5 * air_density_kg_per_m3 * drag_coefficient * frontal_area_m2 * speed_mps**2


def rolling_resistance_force_n(
    mass_kg: float,
    rolling_resistance_coefficient: float,
    grade_rad: float,
    g_m_per_s2: float = STANDARD_GRAVITY_M_PER_S2,
) -> float:
    """F = Crr * m * g * cos(theta)."""
    return rolling_resistance_coefficient * mass_kg * g_m_per_s2 * math.cos(grade_rad)


def grade_force_n(
    mass_kg: float,
    grade_rad: float,
    g_m_per_s2: float = STANDARD_GRAVITY_M_PER_S2,
) -> float:
    """F = m * g * sin(theta): component of gravity along the road surface."""
    return mass_kg * g_m_per_s2 * math.sin(grade_rad)


def road_load_force_n(
    mass_kg: float,
    speed_mps: float,
    acceleration_mps2: float,
    air_density_kg_per_m3: float,
    drag_coefficient: float,
    frontal_area_m2: float,
    rolling_resistance_coefficient: float,
    grade_rad: float,
    g_m_per_s2: float = STANDARD_GRAVITY_M_PER_S2,
) -> float:
    """Total tractive force at the wheels: F = ma + 1/2*rho*Cd*A*v^2 + Crr*m*g*cos(theta) + m*g*sin(theta)."""
    return (
        inertial_force_n(mass_kg, acceleration_mps2)
        + aerodynamic_drag_force_n(speed_mps, air_density_kg_per_m3, drag_coefficient, frontal_area_m2)
        + rolling_resistance_force_n(mass_kg, rolling_resistance_coefficient, grade_rad, g_m_per_s2)
        + grade_force_n(mass_kg, grade_rad, g_m_per_s2)
    )


def wheel_torque_nm(road_load_force_n: float, wheel_radius_m: float) -> float:
    """Torque at the wheel needed to produce the given road-load force."""
    return road_load_force_n * wheel_radius_m


def wheel_angular_velocity_rad_per_s(speed_mps: float, wheel_radius_m: float) -> float:
    return speed_mps / wheel_radius_m


def motor_angular_velocity_rad_per_s(speed_mps: float, wheel_radius_m: float, gear_ratio: float) -> float:
    return wheel_angular_velocity_rad_per_s(speed_mps, wheel_radius_m) * gear_ratio


def motor_torque_request_nm(wheel_torque_nm: float, gear_ratio: float, driveline_efficiency: float) -> float:
    """Motor-shaft torque needed to produce `wheel_torque_nm` at the wheels.

    Gear ratio sets the torque/speed multiplication kinematically regardless
    of direction; efficiency only discounts whichever side is the power
    *output*. Motoring (positive wheel torque, motor is the input, wheels are
    the output) divides by efficiency, so the motor must push harder than the
    ideal case to cover losses. Regen braking (negative wheel torque, wheels
    are the input, motor/generator is the output) multiplies by efficiency,
    so less motor braking torque is needed since driveline friction is
    already removing some of the wheels' kinetic energy for free.
    """
    if wheel_torque_nm >= 0.0:
        return wheel_torque_nm / (gear_ratio * driveline_efficiency)
    return wheel_torque_nm * driveline_efficiency / gear_ratio


def motor_power_w(motor_torque_nm: float, motor_angular_velocity_rad_per_s: float) -> float:
    return motor_torque_nm * motor_angular_velocity_rad_per_s


def wheel_torque_from_motor_torque_nm(motor_torque_nm: float, gear_ratio: float, driveline_efficiency: float) -> float:
    """Wheel torque resulting from an actual motor-shaft torque — the exact inverse of
    `motor_torque_request_nm`. Used to find out what a motor *actually* delivered
    (e.g. after thermal derating reduced it below what was requested), as opposed to
    `motor_torque_request_nm`'s use of finding out what a *desired* wheel torque requires.

    `motor_torque_nm` and the resulting wheel torque always share the same sign
    (motoring: both >=0; regen: both <0), so branching on `motor_torque_nm`'s
    sign here is equivalent to branching on the wheel torque's sign there.
    """
    if motor_torque_nm >= 0.0:
        return motor_torque_nm * gear_ratio * driveline_efficiency
    return motor_torque_nm * gear_ratio / driveline_efficiency


def wheel_force_from_torque_n(wheel_torque_nm: float, wheel_radius_m: float) -> float:
    """Inverse of `wheel_torque_nm`: force at the wheel implied by a wheel torque."""
    return wheel_torque_nm / wheel_radius_m
