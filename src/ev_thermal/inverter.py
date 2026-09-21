"""Inverter electro-thermal model: converts motor electrical draw plus an
`InverterSpec` into switching/conduction losses and heat generation at the
inverter's `ThermalNode`.

Loss model is a simplified analytical placeholder: conduction loss from
current-squared through a temperature-dependent on-resistance, and switching
loss proportional to switching frequency and DC bus voltage. All
coefficients on `InverterSpec` marked TODO(spec-sourcing) are
physically-plausible placeholders pending real datasheet data, per
CLAUDE.md's "don't invent parameters" rule waived for this milestone.
"""

from __future__ import annotations

from ev_thermal.causal import make_causal_event
from ev_thermal.schema import CausalEvent, InverterSpec, InverterStepResult, ThermalNetwork
from ev_thermal.thermal import step_thermal_network


def _derate_scale(temp_k: float, derate_temp_k: float, max_temp_k: float) -> float:
    """1.0 at/below derate_temp_k, ramping linearly to 0.0 at max_temp_k."""
    if temp_k <= derate_temp_k:
        return 1.0
    if temp_k >= max_temp_k:
        return 0.0
    return 1.0 - (temp_k - derate_temp_k) / (max_temp_k - derate_temp_k)


def step_inverter(
    spec: InverterSpec,
    dc_bus_voltage_v: float,
    ac_current_request_a: float,
    junction_temp_k: float,
    coolant_temp_k: float,
    coolant_flow_l_per_min: float,
    ambient_temp_k: float,
    dt_s: float,
    sim_time_s: float,
) -> InverterStepResult:
    """Advance the inverter by one step of size `dt_s`.

    `ambient_temp_k` is accepted for interface consistency with
    `thermal.step_thermal_network` but currently has no effect, since
    `InverterSpec` doesn't yet carry an ambient conductance for its node
    (coolant is the only modeled sink).
    """
    causal_events: list[CausalEvent] = []

    derate_temp_k = spec.thermal_node.derate_temp_k.value
    max_temp_k = spec.thermal_node.max_temp_k.value
    scale = _derate_scale(junction_temp_k, derate_temp_k, max_temp_k)
    applied_current_a = ac_current_request_a * scale
    if scale < 1.0:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="DERATE_TRIGGERED",
                description=(
                    f"Inverter junction temp {junction_temp_k:.1f}K exceeded derate threshold "
                    f"{derate_temp_k:.1f}K; current scaled to {scale:.1%} of request."
                ),
                affected_component="inverter",
                severity="warning",
            )
        )

    r_ds_on_ohm = spec.r_ds_on_ref_ohm.value * (
        1.0 + spec.r_ds_on_temp_coefficient_per_k.value * (junction_temp_k - spec.r_ds_on_ref_temp_k.value)
    )
    conduction_loss_w = applied_current_a**2 * r_ds_on_ohm
    switching_loss_w = (
        spec.switching_frequency_hz.value * spec.switching_energy_coefficient_j_per_v.value * dc_bus_voltage_v
    )
    total_loss_w = conduction_loss_w + switching_loss_w

    node_name = spec.thermal_node.name
    network = ThermalNetwork(nodes={node_name: spec.thermal_node}, edges=[], ambient_coupling={})
    new_temps_k = step_thermal_network(
        network,
        {node_name: junction_temp_k},
        heat_inputs_w={node_name: total_loss_w},
        ambient_temp_k=ambient_temp_k,
        coolant_temp_k=coolant_temp_k,
        coolant_flow_l_per_min=coolant_flow_l_per_min,
        dt_s=dt_s,
    )

    return InverterStepResult(
        conduction_loss_w=conduction_loss_w,
        switching_loss_w=switching_loss_w,
        total_loss_w=total_loss_w,
        junction_temp_k=new_temps_k[node_name],
        applied_current_a=applied_current_a,
        causal_events=causal_events,
    )
