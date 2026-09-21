"""Behavioral tests for `ev_thermal.motor`'s analytical electro-thermal loss model.

Fixture coefficients (winding resistance, Kt, iron/mechanical loss coefficients)
are physically-plausible placeholders, not sourced spec data — see the
TODO(spec-sourcing) markers on the corresponding `MotorSpec` fields in schema.py.
"""

from __future__ import annotations

from ev_thermal.motor import step_motor
from ev_thermal.schema import Confidence, MotorSpec, Sourced, ThermalNode

_COOLANT_TEMP_K = 298.15
_AMBIENT_TEMP_K = 298.15


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="placeholder")


def _make_motor_spec() -> MotorSpec:
    node = ThermalNode(
        name="motor",
        capacitance_j_per_k=_sourced(30_000.0),
        resistance_to_coolant_k_per_w=_sourced(0.03),
        initial_temp_k=298.15,
        max_temp_k=_sourced(453.15),
        derate_temp_k=_sourced(423.15),
    )
    return MotorSpec(
        motor_type="PMSM",
        peak_power_kw=_sourced(150.0),
        continuous_power_kw=_sourced(75.0),
        peak_torque_nm=_sourced(310.0),
        efficiency_peak=_sourced(0.96),
        mass_kg=_sourced(70.0),
        max_winding_temp_k=_sourced(453.15),
        thermal_node=node,
        winding_resistance_ref_ohm=_sourced(0.05),
        winding_resistance_ref_temp_k=_sourced(298.15),
        torque_constant_nm_per_a=_sourced(1.0),
        iron_loss_coefficient_w_per_rad_per_s=_sourced(2.0),
        mechanical_loss_coefficient_w_per_rad3_per_s3=_sourced(0.0002),
        regen_extra_loss_fraction=_sourced(0.15),
    )


def _step(
    spec: MotorSpec,
    torque_request_nm: float,
    speed_rad_per_s: float,
    winding_temp_k: float,
):
    return step_motor(
        spec,
        torque_request_nm,
        speed_rad_per_s,
        winding_temp_k,
        coolant_temp_k=_COOLANT_TEMP_K,
        coolant_flow_l_per_min=10.0,
        ambient_temp_k=_AMBIENT_TEMP_K,
        dt_s=1.0,
        sim_time_s=0.0,
    )


def test_zero_torque_has_zero_copper_loss_but_iron_and_mechanical_present() -> None:
    spec = _make_motor_spec()
    result = _step(spec, torque_request_nm=0.0, speed_rad_per_s=200.0, winding_temp_k=298.15)

    assert result.copper_loss_w == 0.0
    assert result.iron_loss_w > 0.0
    assert result.mechanical_loss_w > 0.0


def test_copper_loss_higher_at_100c_than_25c_for_identical_current() -> None:
    spec = _make_motor_spec()

    cool_result = _step(spec, torque_request_nm=150.0, speed_rad_per_s=200.0, winding_temp_k=298.15)
    hot_result = _step(spec, torque_request_nm=150.0, speed_rad_per_s=200.0, winding_temp_k=373.15)

    assert hot_result.copper_loss_w > cool_result.copper_loss_w


def test_efficiency_at_mid_load_mid_speed_is_physically_plausible() -> None:
    spec = _make_motor_spec()
    result = _step(spec, torque_request_nm=150.0, speed_rad_per_s=200.0, winding_temp_k=333.15)

    efficiency = result.mechanical_power_w / result.electrical_power_w
    assert 0.85 <= efficiency <= 0.96


def test_regen_produces_more_heat_than_motoring_at_same_magnitude() -> None:
    spec = _make_motor_spec()

    motoring = _step(spec, torque_request_nm=150.0, speed_rad_per_s=200.0, winding_temp_k=333.15)
    regen = _step(spec, torque_request_nm=-150.0, speed_rad_per_s=200.0, winding_temp_k=333.15)

    motoring_total_loss_w = motoring.copper_loss_w + motoring.iron_loss_w + motoring.mechanical_loss_w
    regen_total_loss_w = regen.copper_loss_w + regen.iron_loss_w + regen.mechanical_loss_w
    assert regen_total_loss_w > motoring_total_loss_w


def test_derate_reduces_available_torque_monotonically() -> None:
    spec = _make_motor_spec()
    derate_temp_k = spec.thermal_node.derate_temp_k.value
    max_temp_k = spec.thermal_node.max_temp_k.value

    below = _step(spec, torque_request_nm=200.0, speed_rad_per_s=200.0, winding_temp_k=derate_temp_k - 5.0)
    mid = _step(spec, torque_request_nm=200.0, speed_rad_per_s=200.0, winding_temp_k=derate_temp_k + 15.0)
    high = _step(spec, torque_request_nm=200.0, speed_rad_per_s=200.0, winding_temp_k=derate_temp_k + 25.0)
    at_max = _step(spec, torque_request_nm=200.0, speed_rad_per_s=200.0, winding_temp_k=max_temp_k)

    assert below.applied_torque_nm == 200.0
    assert below.applied_torque_nm > mid.applied_torque_nm > high.applied_torque_nm > at_max.applied_torque_nm
    assert at_max.applied_torque_nm == 0.0
    assert len(mid.causal_events) == 1
    assert mid.causal_events[0].event_type == "DERATE_TRIGGERED"
    assert mid.causal_events[0].affected_component == "motor"
    assert len(below.causal_events) == 0
