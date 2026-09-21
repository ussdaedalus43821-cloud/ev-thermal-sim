"""Driver (human or ADAS) speed-tracking control: a PI controller on speed error
that produces a torque request, split into regen (preferred) and mechanical
brake demand when decelerating.

This is a different concern from drive-cycle/trip *generation* (producing the
target-speed sequence itself from a `UsageProfile`'s distributions), which
this file does NOT do despite its original scaffold docstring suggesting
otherwise. That's settled: trip generation belongs in a future `cycle.py` or
a function inside `sim.py`, not here — this file is staying the reactive
per-step PI controller.

No state serialization exists yet (matching `motor.py`/`battery.py`'s
threaded-state pattern), but the PI integral term IS explicit state, passed
in and returned via `DriverStepResult.integral_error_state`, so a fork
branching mid-run replays with the correct integral history rather than
restarting it from zero.

Mechanical brakes are a pure torque sink here — TODO(spec-sourcing): no
thermal model for them exists; they don't generate heat anywhere yet.
"""

from __future__ import annotations

from ev_thermal.schema import DriverStepResult

DEFAULT_KP_NM_PER_MPS = 800.0
DEFAULT_KI_NM_PER_M = 40.0
"""TODO(tuning): placeholder PI gains, not derived from any real vehicle's
control loop. Kp in N*m per m/s of speed error; Ki in N*m per m of
accumulated (integral) speed error."""


def step_driver(
    target_speed_mps: float,
    current_speed_mps: float,
    dt_s: float,
    integral_error_state: float,
    aggression: float,
    accel_torque_limit_nm: float,
    brake_torque_limit_nm: float,
    regen_torque_limit_nm: float,
    kp_nm_per_mps: float = DEFAULT_KP_NM_PER_MPS,
    ki_nm_per_m: float = DEFAULT_KI_NM_PER_M,
) -> DriverStepResult:
    """Advance the driver's PI speed controller by one step of size `dt_s`.

    `aggression` (0.7-1.3) scales the accel/brake clamp limits, not the raw
    PI output directly — so it only visibly changes the result once the
    request is actually being clamped by one of those limits.
    """
    error_mps = target_speed_mps - current_speed_mps
    candidate_integral_state = integral_error_state + error_mps * dt_s
    raw_torque_request_nm = kp_nm_per_mps * error_mps + ki_nm_per_m * candidate_integral_state

    scaled_accel_limit_nm = accel_torque_limit_nm * aggression
    scaled_brake_limit_nm = brake_torque_limit_nm * aggression
    torque_request_nm = max(-scaled_brake_limit_nm, min(raw_torque_request_nm, scaled_accel_limit_nm))

    # Simple anti-windup: freeze the integral rather than let it keep growing
    # in the direction that's already saturating the output.
    new_integral_error_state = (
        integral_error_state if torque_request_nm != raw_torque_request_nm else candidate_integral_state
    )

    if torque_request_nm >= 0.0:
        regen_torque_nm = 0.0
        mechanical_brake_torque_nm = 0.0
    else:
        regen_torque_nm = -min(-torque_request_nm, regen_torque_limit_nm)
        mechanical_brake_torque_nm = -torque_request_nm - (-regen_torque_nm)

    return DriverStepResult(
        torque_request_nm=torque_request_nm,
        regen_torque_nm=regen_torque_nm,
        mechanical_brake_torque_nm=mechanical_brake_torque_nm,
        mechanical_brake_active=mechanical_brake_torque_nm > 0.0,
        integral_error_state=new_integral_error_state,
    )
