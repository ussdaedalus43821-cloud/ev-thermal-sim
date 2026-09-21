"""Behavioral tests for `ev_thermal.driver`'s PI speed-tracking controller.

PI gains (`DEFAULT_KP_NM_PER_MPS`/`DEFAULT_KI_NM_PER_M`) are TODO(tuning)
placeholders, not derived from any real vehicle's control loop.
"""

from __future__ import annotations

from ev_thermal.driver import step_driver

_ACCEL_LIMIT_NM = 3000.0
_BRAKE_LIMIT_NM = 3000.0
_REGEN_LIMIT_NM = 1500.0
_DT_S = 0.1


def _step(
    target_speed_mps: float,
    current_speed_mps: float,
    aggression: float = 1.0,
    integral_error_state: float = 0.0,
):
    return step_driver(
        target_speed_mps,
        current_speed_mps,
        _DT_S,
        integral_error_state,
        aggression,
        _ACCEL_LIMIT_NM,
        _BRAKE_LIMIT_NM,
        _REGEN_LIMIT_NM,
    )


def test_zero_speed_error_gives_near_zero_torque_request() -> None:
    result = _step(target_speed_mps=15.0, current_speed_mps=15.0)
    assert result.torque_request_nm == 0.0


def test_large_positive_error_clamps_to_accel_limit() -> None:
    result = _step(target_speed_mps=20.0, current_speed_mps=0.0)
    assert result.torque_request_nm == _ACCEL_LIMIT_NM


def test_higher_aggression_gives_higher_clamped_request_for_same_error() -> None:
    low = _step(target_speed_mps=50.0, current_speed_mps=0.0, aggression=0.7)
    high = _step(target_speed_mps=50.0, current_speed_mps=0.0, aggression=1.3)

    assert high.torque_request_nm > low.torque_request_nm
    assert low.torque_request_nm == _ACCEL_LIMIT_NM * 0.7
    assert high.torque_request_nm == _ACCEL_LIMIT_NM * 1.3


def test_deceleration_within_regen_capacity_uses_regen_only() -> None:
    result = _step(target_speed_mps=0.0, current_speed_mps=1.25)

    assert result.torque_request_nm < 0.0
    assert result.regen_torque_nm == result.torque_request_nm
    assert result.mechanical_brake_torque_nm == 0.0
    assert result.mechanical_brake_active is False


def test_deceleration_beyond_regen_capacity_flags_mechanical_brake() -> None:
    result = _step(target_speed_mps=0.0, current_speed_mps=3.0)

    assert result.regen_torque_nm == -_REGEN_LIMIT_NM
    assert result.mechanical_brake_torque_nm > 0.0
    assert result.mechanical_brake_active is True
    assert result.regen_torque_nm + (-result.mechanical_brake_torque_nm) == result.torque_request_nm
