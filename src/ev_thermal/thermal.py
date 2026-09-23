"""Lumped-node thermal network solver: advances a `ThermalNetwork`'s node
temperatures by one explicit-Euler step given per-node heat generation and a
single ambient/coolant boundary condition.

No RK integrator yet — `step_thermal_network` is explicit Euler only, and
enforces that `dt_s` stays below every node's RC time constant, since a
larger step would make Euler diverge rather than merely lose accuracy.
Callers (`sim.py`, eventually) supply `dt_s` from `DTPolicy` and thread the
returned temperature dict forward as `SimState.node_temps_k`.

Coolant coupling uses each `ThermalNode`'s `resistance_to_coolant_k_per_w` as
a fixed conductance to a single scalar `coolant_temp_k`, gated on/off by
`coolant_flow_l_per_min`: zero (or negative) flow disables the coolant path
entirely (pump off), any positive flow fully enables it. There is no
flow-rate-dependent heat-exchanger effectiveness yet.

Ambient coupling uses each `ThermalNode`'s own `resistance_to_ambient_k_per_w`
(added in the ambient-coupling milestone) as a fixed conductance to the same
`ambient_temp_k` every caller already passes in — a node with `None` there
has no ambient path at all, which is correct only for a genuinely adiabatic
node. This is a node-level property (the node's own physical environment),
unlike the deprecated `ThermalNetwork.ambient_coupling` dict it supersedes,
which was network-level and never actually populated by any caller.
"""

from __future__ import annotations

from ev_thermal.schema import ThermalNetwork


class ThermalStabilityError(Exception):
    """Raised when `dt_s` meets or exceeds a node's RC time constant.

    Explicit Euler integration of dT/dt = -(T - T_sink) / tau diverges (grows
    per step in magnitude) once dt >= tau, so this is caught before stepping
    rather than silently producing nonsense temperatures.
    """


def _node_total_conductance_w_per_k(
    network: ThermalNetwork,
    node_name: str,
    coolant_flow_l_per_min: float,
) -> float:
    total_w_per_k = 0.0
    node = network.nodes[node_name]
    if coolant_flow_l_per_min > 0.0:
        total_w_per_k += 1.0 / node.resistance_to_coolant_k_per_w.value
    if node.resistance_to_ambient_k_per_w is not None:
        total_w_per_k += 1.0 / node.resistance_to_ambient_k_per_w.value
    ambient_conductance = network.ambient_coupling.get(node_name)
    if ambient_conductance is not None:
        total_w_per_k += ambient_conductance.value
    for node_a, node_b, conductance in network.edges:
        if node_name in (node_a, node_b):
            total_w_per_k += conductance.value
    return total_w_per_k


def node_time_constant_s(
    network: ThermalNetwork,
    node_name: str,
    coolant_flow_l_per_min: float,
) -> float | None:
    """RC time constant for one node, or `None` if it has no sink (no decay)."""
    total_conductance_w_per_k = _node_total_conductance_w_per_k(network, node_name, coolant_flow_l_per_min)
    if total_conductance_w_per_k <= 0.0:
        return None
    capacitance_j_per_k = network.nodes[node_name].capacitance_j_per_k.value
    return capacitance_j_per_k / total_conductance_w_per_k


def _check_stability(network: ThermalNetwork, coolant_flow_l_per_min: float, dt_s: float) -> None:
    for node_name in network.nodes:
        tau_s = node_time_constant_s(network, node_name, coolant_flow_l_per_min)
        if tau_s is not None and dt_s >= tau_s:
            raise ThermalStabilityError(
                f"dt_s={dt_s:g} exceeds node {node_name!r}'s RC time constant tau_s={tau_s:g}; "
                "explicit Euler would diverge for this node. Reduce dt_s, increase its thermal "
                "mass, or reduce its conductance to coolant/ambient/neighboring nodes."
            )


def step_thermal_network(
    network: ThermalNetwork,
    temps_k: dict[str, float],
    heat_inputs_w: dict[str, float],
    ambient_temp_k: float,
    coolant_temp_k: float,
    coolant_flow_l_per_min: float,
    dt_s: float,
) -> dict[str, float]:
    """Advance every node in `network` by one explicit-Euler step of size `dt_s`.

    `heat_inputs_w` gives each node's internally-generated heat (missing
    nodes default to 0 W). Returns a new temperature dict; does not mutate
    `temps_k`.
    """
    _check_stability(network, coolant_flow_l_per_min, dt_s)

    net_flux_w = {node_name: heat_inputs_w.get(node_name, 0.0) for node_name in network.nodes}

    for node_name, node in network.nodes.items():
        if coolant_flow_l_per_min > 0.0:
            coolant_conductance_w_per_k = 1.0 / node.resistance_to_coolant_k_per_w.value
            net_flux_w[node_name] += coolant_conductance_w_per_k * (coolant_temp_k - temps_k[node_name])
        if node.resistance_to_ambient_k_per_w is not None:
            ambient_node_conductance_w_per_k = 1.0 / node.resistance_to_ambient_k_per_w.value
            net_flux_w[node_name] += ambient_node_conductance_w_per_k * (ambient_temp_k - temps_k[node_name])
        ambient_conductance = network.ambient_coupling.get(node_name)
        if ambient_conductance is not None:
            net_flux_w[node_name] += ambient_conductance.value * (ambient_temp_k - temps_k[node_name])

    for node_a, node_b, conductance in network.edges:
        flow_w = conductance.value * (temps_k[node_b] - temps_k[node_a])
        net_flux_w[node_a] += flow_w
        net_flux_w[node_b] -= flow_w

    new_temps_k = dict(temps_k)
    for node_name, node in network.nodes.items():
        capacitance_j_per_k = node.capacitance_j_per_k.value
        new_temps_k[node_name] = temps_k[node_name] + net_flux_w[node_name] / capacitance_j_per_k * dt_s
    return new_temps_k
