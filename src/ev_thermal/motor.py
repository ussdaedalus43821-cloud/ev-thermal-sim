"""Motor electro-thermal model: converts requested torque plus a `MotorSpec` into
electrical draw, losses, and heat generation at the motor's `ThermalNode`.

Loss model is a simplified analytical placeholder, not a measured efficiency
map: copper loss from current-squared through a temperature-dependent winding
resistance, iron loss proportional to speed, and windage/friction loss
proportional to speed cubed. All coefficients on `MotorSpec` marked
TODO(spec-sourcing) are physically-plausible placeholders pending real
datasheet/teardown data, per CLAUDE.md's "don't invent parameters" rule
waived for this milestone.
"""

from __future__ import annotations

from ev_thermal.causal import make_causal_event
from ev_thermal.schema import CausalEvent, MotorHeatResult, MotorSpec, MotorStepResult, ThermalNetwork
from ev_thermal.thermal import step_thermal_network

COPPER_TEMP_COEFFICIENT_PER_K = 0.00393
"""Temperature coefficient of resistance for copper windings; a material
physical constant, not an uncertain spec value, so it isn't `Sourced`."""


def _derate_scale(temp_k: float, derate_temp_k: float, max_temp_k: float) -> float:
    """1.0 at/below derate_temp_k, ramping linearly to 0.0 at max_temp_k."""
    if temp_k <= derate_temp_k:
        return 1.0
    if temp_k >= max_temp_k:
        return 0.0
    return 1.0 - (temp_k - derate_temp_k) / (max_temp_k - derate_temp_k)


def available_torque_nm(spec: MotorSpec, winding_temp_k: float) -> float:
    """The motor's thermally-derated torque ceiling (magnitude) at `winding_temp_k`.

    This is the single source of truth for the motor's torque limit: both
    `step_motor` (clamping a request to it) and `controller.py` (comparing it
    against other components' limits) call this rather than each computing
    their own copy of the derate curve.
    """
    scale = _derate_scale(winding_temp_k, spec.thermal_node.derate_temp_k.value, spec.thermal_node.max_temp_k.value)
    return spec.peak_torque_nm.value * scale


def compute_motor_heat(
    spec: MotorSpec,
    torque_request_nm: float,
    speed_rad_per_s: float,
    winding_temp_k: float,
    sim_time_s: float,
) -> MotorHeatResult:
    """Everything about one motor step except advancing the winding's own temperature.

    Split out from `step_motor` so `sim.py` can combine this heat output with
    inverter/battery heat and the coolant node into one shared
    `thermal.step_thermal_network` call spanning all of them, rather than each
    component integrating its own node in isolation. `step_motor` still calls
    this internally and does its own isolated-node thermal step afterward, so
    it's unchanged for standalone/direct use.

    Positive `torque_request_nm` with positive `speed_rad_per_s` is motoring;
    negative torque at positive speed is regen braking.
    """
    causal_events: list[CausalEvent] = []

    derate_temp_k = spec.thermal_node.derate_temp_k.value
    # available_torque_nm stays the single source of truth for the ceiling
    # itself; thermal_scale is recomputed here only to tell apart WHY the
    # ceiling bound (see the branch below), not to recompute the ceiling.
    thermal_scale = _derate_scale(winding_temp_k, derate_temp_k, spec.thermal_node.max_temp_k.value)
    torque_ceiling_nm = available_torque_nm(spec, winding_temp_k)
    applied_torque_nm = max(-torque_ceiling_nm, min(torque_request_nm, torque_ceiling_nm))
    if abs(applied_torque_nm) < abs(torque_request_nm):
        # Two genuinely different causes produce the same clamp, and must be
        # told apart rather than both blamed on temperature: thermal_scale<1.0
        # means the derate curve is actually binding; thermal_scale==1.0 means
        # the request simply exceeds peak_torque_nm regardless of temperature
        # (winding_temp_k can be far below derate_temp_k in this case).
        if thermal_scale < 1.0:
            causal_events.append(
                make_causal_event(
                    sim_time_s=sim_time_s,
                    event_type="DERATE_TRIGGERED",
                    description=(
                        f"Motor winding temp {winding_temp_k:.1f}K exceeded derate threshold "
                        f"{derate_temp_k:.1f}K; torque request {torque_request_nm:.1f}Nm limited "
                        f"to {applied_torque_nm:.1f}Nm."
                    ),
                    affected_component="motor",
                    severity="warning",
                )
            )
        else:
            causal_events.append(
                make_causal_event(
                    sim_time_s=sim_time_s,
                    event_type="TORQUE_LIMIT_TRIGGERED",
                    description=(
                        f"Torque request {torque_request_nm:.1f}Nm exceeded motor peak rating "
                        f"peak_torque_nm={spec.peak_torque_nm.value:.1f}Nm (winding temp {winding_temp_k:.1f}K "
                        f"is below the {derate_temp_k:.1f}K derate threshold — not a thermal cause); "
                        f"limited to {applied_torque_nm:.1f}Nm."
                    ),
                    affected_component="motor",
                    severity="warning",
                )
            )

    current_a = abs(applied_torque_nm) / spec.torque_constant_nm_per_a.value
    winding_resistance_ohm = spec.winding_resistance_ref_ohm.value * (
        1.0 + COPPER_TEMP_COEFFICIENT_PER_K * (winding_temp_k - spec.winding_resistance_ref_temp_k.value)
    )
    copper_loss_w = current_a**2 * winding_resistance_ohm
    if applied_torque_nm < 0.0:
        copper_loss_w *= 1.0 + spec.regen_extra_loss_fraction.value

    iron_loss_w = spec.iron_loss_coefficient_w_per_rad_per_s.value * abs(speed_rad_per_s)
    mechanical_loss_w = spec.mechanical_loss_coefficient_w_per_rad3_per_s3.value * abs(speed_rad_per_s) ** 3
    total_heat_w = copper_loss_w + iron_loss_w + mechanical_loss_w

    mechanical_power_w = applied_torque_nm * speed_rad_per_s
    electrical_power_w = mechanical_power_w + total_heat_w

    return MotorHeatResult(
        electrical_power_w=electrical_power_w,
        mechanical_power_w=mechanical_power_w,
        current_a=current_a,
        copper_loss_w=copper_loss_w,
        iron_loss_w=iron_loss_w,
        mechanical_loss_w=mechanical_loss_w,
        total_heat_w=total_heat_w,
        applied_torque_nm=applied_torque_nm,
        causal_events=causal_events,
    )


def step_motor(
    spec: MotorSpec,
    torque_request_nm: float,
    speed_rad_per_s: float,
    winding_temp_k: float,
    coolant_temp_k: float,
    coolant_flow_l_per_min: float,
    ambient_temp_k: float,
    dt_s: float,
    sim_time_s: float,
) -> MotorStepResult:
    """Advance the motor by one step of size `dt_s`, standalone (isolated-node thermal
    step — see `compute_motor_heat`'s docstring for why `sim.py` doesn't use this).

    `ambient_temp_k` is accepted for interface consistency with
    `thermal.step_thermal_network` but currently has no effect, since
    `MotorSpec` doesn't yet carry an ambient conductance for its node
    (coolant is the only modeled sink).
    """
    heat_result = compute_motor_heat(spec, torque_request_nm, speed_rad_per_s, winding_temp_k, sim_time_s)

    node_name = spec.thermal_node.name
    network = ThermalNetwork(nodes={node_name: spec.thermal_node}, edges=[], ambient_coupling={})
    new_temps_k = step_thermal_network(
        network,
        {node_name: winding_temp_k},
        heat_inputs_w={node_name: heat_result.total_heat_w},
        ambient_temp_k=ambient_temp_k,
        coolant_temp_k=coolant_temp_k,
        coolant_flow_l_per_min=coolant_flow_l_per_min,
        dt_s=dt_s,
    )

    return MotorStepResult(
        electrical_power_w=heat_result.electrical_power_w,
        mechanical_power_w=heat_result.mechanical_power_w,
        current_a=heat_result.current_a,
        copper_loss_w=heat_result.copper_loss_w,
        iron_loss_w=heat_result.iron_loss_w,
        mechanical_loss_w=heat_result.mechanical_loss_w,
        winding_temp_k=new_temps_k[node_name],
        applied_torque_nm=heat_result.applied_torque_nm,
        causal_events=heat_result.causal_events,
    )
