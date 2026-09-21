"""Analytical validation for `ev_thermal.thermal`'s explicit-Euler network solver."""

from __future__ import annotations

import pytest

from ev_thermal.schema import Confidence, Sourced, ThermalNetwork, ThermalNode
from ev_thermal.thermal import ThermalStabilityError, node_time_constant_s, step_thermal_network


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.MEDIUM, source="test-fixture")


def _single_node_network(
    *,
    capacitance_j_per_k: float,
    resistance_to_coolant_k_per_w: float,
    ambient_conductance_w_per_k: float | None = None,
) -> ThermalNetwork:
    node = ThermalNode(
        name="node",
        capacitance_j_per_k=_sourced(capacitance_j_per_k),
        resistance_to_coolant_k_per_w=_sourced(resistance_to_coolant_k_per_w),
        initial_temp_k=298.15,
        max_temp_k=_sourced(400.0),
        derate_temp_k=_sourced(350.0),
    )
    ambient_coupling = {}
    if ambient_conductance_w_per_k is not None:
        ambient_coupling["node"] = _sourced(ambient_conductance_w_per_k)
    return ThermalNetwork(nodes={"node": node}, edges=[], ambient_coupling=ambient_coupling)


def test_constant_heat_input_reaches_analytical_steady_state() -> None:
    capacitance_j_per_k = 1000.0
    resistance_k_per_w = 0.05
    heat_input_w = 100.0
    coolant_temp_k = 300.0

    network = _single_node_network(
        capacitance_j_per_k=capacitance_j_per_k,
        resistance_to_coolant_k_per_w=resistance_k_per_w,
    )
    tau_s = node_time_constant_s(network, "node", coolant_flow_l_per_min=10.0)
    assert tau_s is not None

    dt_s = tau_s / 2000.0
    temps_k = {"node": coolant_temp_k}
    steps = round(5 * tau_s / dt_s)
    for _ in range(steps):
        temps_k = step_thermal_network(
            network,
            temps_k,
            heat_inputs_w={"node": heat_input_w},
            ambient_temp_k=coolant_temp_k,
            coolant_temp_k=coolant_temp_k,
            coolant_flow_l_per_min=10.0,
            dt_s=dt_s,
        )

    expected_delta_k = heat_input_w * resistance_k_per_w
    actual_delta_k = temps_k["node"] - coolant_temp_k
    assert abs(actual_delta_k - expected_delta_k) / expected_delta_k < 0.01


def test_no_heat_input_warms_toward_ambient_not_coolant() -> None:
    capacitance_j_per_k = 500.0
    ambient_conductance_w_per_k = 10.0
    ambient_temp_k = 298.0
    initial_temp_k = 280.0
    decoy_coolant_temp_k = 200.0  # far from ambient; flow=0 must exclude this entirely

    network = _single_node_network(
        capacitance_j_per_k=capacitance_j_per_k,
        resistance_to_coolant_k_per_w=0.02,
        ambient_conductance_w_per_k=ambient_conductance_w_per_k,
    )
    tau_s = node_time_constant_s(network, "node", coolant_flow_l_per_min=0.0)
    assert tau_s is not None

    dt_s = tau_s / 2000.0
    temps_k = {"node": initial_temp_k}
    steps = round(5 * tau_s / dt_s)
    previous_temp_k = initial_temp_k
    for _ in range(steps):
        temps_k = step_thermal_network(
            network,
            temps_k,
            heat_inputs_w={},
            ambient_temp_k=ambient_temp_k,
            coolant_temp_k=decoy_coolant_temp_k,
            coolant_flow_l_per_min=0.0,
            dt_s=dt_s,
        )
        assert temps_k["node"] >= previous_temp_k  # monotonic warming, no overshoot
        previous_temp_k = temps_k["node"]

    assert abs(temps_k["node"] - ambient_temp_k) / (ambient_temp_k - initial_temp_k) < 0.01


def test_stability_check_raises_when_dt_exceeds_time_constant() -> None:
    network = _single_node_network(
        capacitance_j_per_k=100.0,
        resistance_to_coolant_k_per_w=0.01,
    )
    tau_s = node_time_constant_s(network, "node", coolant_flow_l_per_min=10.0)
    assert tau_s is not None

    with pytest.raises(ThermalStabilityError):
        step_thermal_network(
            network,
            {"node": 300.0},
            heat_inputs_w={},
            ambient_temp_k=300.0,
            coolant_temp_k=300.0,
            coolant_flow_l_per_min=10.0,
            dt_s=tau_s * 2,
        )


def test_zero_flow_disables_coolant_and_isolated_node_has_no_time_constant() -> None:
    network = _single_node_network(
        capacitance_j_per_k=100.0,
        resistance_to_coolant_k_per_w=0.01,
    )
    assert node_time_constant_s(network, "node", coolant_flow_l_per_min=0.0) is None

    # No sink active (flow=0, no ambient coupling): temperature should be static
    # aside from direct heat input, and the (very large) dt must not raise.
    temps_k = step_thermal_network(
        network,
        {"node": 300.0},
        heat_inputs_w={},
        ambient_temp_k=250.0,
        coolant_temp_k=250.0,
        coolant_flow_l_per_min=0.0,
        dt_s=3600.0,
    )
    assert temps_k["node"] == 300.0
