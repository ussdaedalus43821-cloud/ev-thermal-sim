"""Behavioral tests for `ev_thermal.inverter`'s analytical electro-thermal loss model.

Fixture coefficients (R_ds_on, switching energy) are physically-plausible
placeholders, not sourced spec data — see the TODO(spec-sourcing) markers on
the corresponding `InverterSpec` fields in schema.py.
"""

from __future__ import annotations

from ev_thermal.inverter import step_inverter
from ev_thermal.schema import Confidence, InverterSpec, Sourced, ThermalNode

_COOLANT_TEMP_K = 298.15
_AMBIENT_TEMP_K = 298.15
_DC_BUS_VOLTAGE_V = 400.0


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="placeholder")


def _make_inverter_spec(switching_frequency_hz: float = 10_000.0) -> InverterSpec:
    node = ThermalNode(
        name="inverter",
        capacitance_j_per_k=_sourced(3_000.0),
        resistance_to_coolant_k_per_w=_sourced(0.05),
        initial_temp_k=298.15,
        max_temp_k=_sourced(423.15),
        derate_temp_k=_sourced(398.15),
    )
    return InverterSpec(
        topology="SiC three-phase",
        peak_power_kw=_sourced(150.0),
        efficiency_peak=_sourced(0.98),
        switching_frequency_hz=_sourced(switching_frequency_hz),
        max_junction_temp_k=_sourced(423.15),
        mass_kg=_sourced(15.0),
        thermal_node=node,
        r_ds_on_ref_ohm=_sourced(0.002),
        r_ds_on_ref_temp_k=_sourced(298.15),
        r_ds_on_temp_coefficient_per_k=_sourced(0.005),
        switching_energy_coefficient_j_per_v=_sourced(0.00003),
    )


def _step(spec: InverterSpec, current_a: float, junction_temp_k: float = 298.15):
    return step_inverter(
        spec,
        dc_bus_voltage_v=_DC_BUS_VOLTAGE_V,
        ac_current_request_a=current_a,
        junction_temp_k=junction_temp_k,
        coolant_temp_k=_COOLANT_TEMP_K,
        coolant_flow_l_per_min=10.0,
        ambient_temp_k=_AMBIENT_TEMP_K,
        dt_s=1.0,
        sim_time_s=0.0,
    )


def test_conduction_loss_scales_with_current_squared() -> None:
    spec = _make_inverter_spec()
    base_current_a = 50.0
    base = _step(spec, base_current_a)

    for multiplier in (2.0, 3.0, 5.0):
        scaled = _step(spec, base_current_a * multiplier)
        expected = base.conduction_loss_w * multiplier**2
        assert abs(scaled.conduction_loss_w - expected) / expected < 0.01


def test_switching_loss_scales_linearly_with_switching_frequency() -> None:
    base_spec = _make_inverter_spec(switching_frequency_hz=5_000.0)
    base = _step(base_spec, current_a=50.0)

    for multiplier in (2.0, 3.0, 4.0):
        scaled_spec = _make_inverter_spec(switching_frequency_hz=5_000.0 * multiplier)
        scaled = _step(scaled_spec, current_a=50.0)
        expected = base.switching_loss_w * multiplier
        assert abs(scaled.switching_loss_w - expected) / expected < 0.01


def test_derate_reduces_available_current_monotonically() -> None:
    spec = _make_inverter_spec()
    derate_temp_k = spec.thermal_node.derate_temp_k.value
    max_temp_k = spec.thermal_node.max_temp_k.value

    below = _step(spec, current_a=100.0, junction_temp_k=derate_temp_k - 5.0)
    mid = _step(spec, current_a=100.0, junction_temp_k=derate_temp_k + 10.0)
    high = _step(spec, current_a=100.0, junction_temp_k=derate_temp_k + 20.0)
    at_max = _step(spec, current_a=100.0, junction_temp_k=max_temp_k)

    assert below.applied_current_a == 100.0
    assert below.applied_current_a > mid.applied_current_a > high.applied_current_a > at_max.applied_current_a
    assert at_max.applied_current_a == 0.0
    assert len(mid.causal_events) == 1
    assert mid.causal_events[0].event_type == "DERATE_TRIGGERED"
    assert mid.causal_events[0].affected_component == "inverter"
    assert len(below.causal_events) == 0
