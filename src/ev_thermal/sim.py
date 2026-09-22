"""Main simulation loop: drives a `Scenario` forward in time using an adaptive
dt policy, composing driver/controller/motor/inverter/battery/cooling/thermal
steps into a sequence of `SimState`s, and collecting `CausalEvent`s as it goes.

Scope for this milestone: short-duration runs (minutes to hours) via
`run_scenario`. Long-horizon mode (weeks to years, adaptive soak steps,
checkpointing), Monte Carlo, fork-branching UI, and file I/O are all
out of scope here — see CLAUDE.md's deferred-work notes.

Step order within one dt, and why:

1. `driver.step_driver` — torque request from target speed vs. current speed
   (state from the END of the previous step; there's no "current" speed yet
   this step until step 7 computes it).
2. `controller.step_controller` — commanded torque, pump/fan duty, active
   limits. Uses component states from the end of the previous step (motor/
   inverter/battery temps, SOC, `battery_voltage_v`) — see `ControllerStepResult`'s
   docstring; this is not a simplification, it's what the design already calls for.
3. `motor.compute_motor_heat` and `inverter.compute_inverter_heat` — mechanical
   power, electrical power, and heat, WITHOUT advancing either component's own
   winding/junction temperature.
4. `battery.compute_battery_electrical_step` — current, terminal voltage, SOC/
   V_RC update, and heat, WITHOUT advancing the pack's own temperature.
5. `cooling.step_cooling` — heat rejected by the radiator, using the coolant
   node's current (start-of-step) temperature as its `coolant_inlet_temp_k`.
6. `thermal.step_thermal_network` — ONE combined solve across motor+inverter+
   battery+coolant nodes together, with each component's heat as a source,
   radiator rejection as a sink on the coolant node, and edges (not the
   ad-hoc per-node "coolant boundary" mechanism) connecting each component to
   the coolant node, using `1/resistance_to_coolant_k_per_w` as edge
   conductance. This is why steps 3-4 don't advance temperature themselves:
   a real coolant node needs all four nodes integrated together, not four
   separate isolated-node solves. `coolant_flow_l_per_min=0.0` is passed to
   disable the legacy per-node boundary mechanism for this call, so heat only
   flows through the explicit edges (no double-counting).
7. Vehicle speed advance — motor's actually-applied torque converted back to
   wheel force via the driveline (the exact inverse of how
   `vehicle.motor_torque_request_nm` derived the request), net against road
   load at the CURRENT (pre-step) speed, integrated to a new speed. `grade_rad`
   (default 0.0, flat) is a single scalar applied for the whole run via
   `vehicle.rolling_resistance_force_n`/`vehicle.grade_force_n` — still not a
   route/elevation profile (no per-position grade model exists), just a
   constant road slope for the dashboard's "road grade" control.
8. Collect every component's `CausalEvent`s (controller's are authoritative;
   motor/inverter/battery still emit their own too, per CLAUDE.md's note on
   the intentional redundancy — all are recorded here regardless).
9. `sim_time_s += dt_s`.

Adaptive dt (`_choose_target_dt_s`) is a simple three-tier placeholder,
TODO(tuning): driving uses `scenario.dt_policy.min_dt_s`; a fully quiescent,
near-ambient-temperature parked vehicle uses `scenario.dt_policy.max_dt_s`;
a parked-but-still-warm vehicle uses an intermediate `PARKED_WARM_DT_S`.
KNOWN GAP: the "or charging" clause from the original spec isn't wired up —
there's no external-charger model yet, so "charging" can't be distinguished
from "parked" this milestone; only vehicle speed and coolant temperature
drive the tier choice. `dt_policy.max_temp_change_per_step_k` is unused.

Stability is handled PROACTIVELY, not by catching `ThermalStabilityError`
reactively: `_stabilize_dt_s` calls `thermal.node_time_constant_s` on the
combined network for the chosen dt and halves it until every node is stable,
before running the rest of the step. Halving only the thermal sub-step and
leaving steps 1-5's calculations at the original (unstable-for-thermal) dt
would leave the step's state advances mutually inconsistent (e.g. SOC
advanced by one dt, temps by a smaller one); checking upfront avoids that
without needing a partial-step redo. `_stabilize_dt_s` also respects the
battery's own RC-pair time constant (R1*C1) — a completely separate Euler
stability requirement from the thermal network's, enforced by
`battery.compute_battery_electrical_step` itself, but one the adaptive dt
choice needs to know about too, or that function raises `BatteryStabilityError`.

`SimState.state_id` is `f"{scenario_id}-step-{step_count:06d}"`, not a random
uuid: determinism (same seed + same inputs -> byte-identical output,
including this field) requires it. `causal.make_causal_event`'s `event_id`
is similarly a deterministic hash of the event's own content, not a uuid4 —
see that function's docstring.

Single-cooling-loop, single-motor simplification: `profile.thermal_architecture
.cooling_loops[0]` is treated as THE loop serving all three components, and
`profile.motors[0]` as THE motor, regardless of `CoolingLoop.serves` or
dual-motor support in the schema. Multi-loop/dual-motor orchestration is
deferred.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from ev_thermal.battery import compute_battery_electrical_step
from ev_thermal.controller import step_controller
from ev_thermal.cooling import AIR_DENSITY_KG_PER_M3, step_cooling
from ev_thermal.driver import step_driver
from ev_thermal.inverter import compute_inverter_heat
from ev_thermal.motor import compute_motor_heat
from ev_thermal.schema import (
    CausalEvent,
    Confidence,
    CoolingLoop,
    DTPolicy,
    EnergyTotals,
    ManufacturerProfile,
    RunResult,
    Scenario,
    SimState,
    SimStepRecord,
    SimTimeSeries,
    Sourced,
    ThermalNetwork,
    ThermalNode,
)
from ev_thermal.thermal import node_time_constant_s, step_thermal_network
from ev_thermal.vehicle import (
    aerodynamic_drag_force_n,
    grade_force_n,
    motor_angular_velocity_rad_per_s,
    rolling_resistance_force_n,
    wheel_force_from_torque_n,
    wheel_torque_from_motor_torque_nm,
)

COOLANT_NODE_NAME = "coolant"

DRIVING_SPEED_THRESHOLD_MPS = 0.1
COOLANT_NEAR_AMBIENT_THRESHOLD_K = 2.0
PARKED_WARM_DT_S = 5.0
"""TODO(tuning): placeholder middle tier of the adaptive dt policy."""

MAX_DT_HALVINGS = 20

DEFAULT_DRIVER_AGGRESSION = 1.0
DEFAULT_GRADE_RAD = 0.0
DEFAULT_PUMP_AGGRESSIVENESS = 1.0
REGEN_TORQUE_LIMIT_FRACTION_OF_PEAK = 0.5
"""TODO(tuning): placeholder driver-behavior limits, derived from the motor's
own peak_torque_nm rather than an unrelated arbitrary constant, so the
driver's own clamp is never the binding one ahead of the real component
limits at a comparable scale."""

SIM_DRIVER_KP_NM_PER_MPS = 60.0
SIM_DRIVER_KI_NM_PER_M = 5.0
"""TODO(tuning): driver.py's own DEFAULT_KP/DEFAULT_KI (800/40) were tuned
against that module's own tests, which use dt=0.1s. At sim.py's dt=1.0s
driving step, those gains are unstable: a linearized loop-gain check near
the setpoint (K_loop = Kp*gear_ratio*driveline_efficiency*dt /
(wheel_radius*mass)) comes out to roughly 11.5 with Kp=800 for a typical
fixture vehicle — over 5x the |1-K_loop|<1 stability boundary — and this was
confirmed empirically: the vehicle oscillated between roughly -170Nm and the
+310Nm ceiling every step near the setpoint instead of converging. These
values keep K_loop comfortably inside the stable range at dt=1.0s. Passed
explicitly to `step_driver` rather than changing driver.py's own defaults,
since those defaults are validated against a different dt regime."""


def _sourced(value: float, source: str) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source=source)


def _conductance_from_resistance(resistance: Sourced[float]) -> Sourced[float]:
    return Sourced[float](
        value=1.0 / resistance.value,
        confidence=resistance.confidence,
        source=f"1/resistance_to_coolant_k_per_w ({resistance.source})",
    )


def _coolant_thermal_node(cooling_spec: CoolingLoop, temp_k: float) -> ThermalNode:
    """Builds the coolant's own lumped `ThermalNode` for the combined network.

    Capacitance is derived from the loop's own volume/density/specific heat.
    `max_temp_k`/`derate_temp_k`/`resistance_to_coolant_k_per_w` are required
    by `ThermalNode` but genuinely unused here: nothing reads the coolant
    node's own derate/max-temp limits or its own coolant-boundary resistance
    (only the explicit edges to it matter in the combined network), so
    they're set to obviously-non-binding placeholders, not spec values.
    """
    capacitance_j_per_k = (
        cooling_spec.loop_volume_l.value
        / 1000.0
        * cooling_spec.coolant_density_kg_per_m3.value
        * cooling_spec.coolant_specific_heat_j_per_kg_k.value
    )
    return ThermalNode(
        name=COOLANT_NODE_NAME,
        capacitance_j_per_k=_sourced(capacitance_j_per_k, "derived from loop_volume_l/density/specific_heat"),
        resistance_to_coolant_k_per_w=_sourced(1.0, "unused for the coolant node itself"),
        initial_temp_k=temp_k,
        max_temp_k=_sourced(1000.0, "unused: no derate curve applies to the coolant node itself"),
        derate_temp_k=_sourced(1000.0, "unused: no derate curve applies to the coolant node itself"),
    )


def _combined_thermal_network(profile: ManufacturerProfile, coolant_node: ThermalNode) -> ThermalNetwork:
    motor_node = profile.motors[0].thermal_node
    inverter_node = profile.inverter.thermal_node
    battery_node = profile.battery.thermal_node
    coolant_name = coolant_node.name

    edges = [
        (motor_node.name, coolant_name, _conductance_from_resistance(motor_node.resistance_to_coolant_k_per_w)),
        (inverter_node.name, coolant_name, _conductance_from_resistance(inverter_node.resistance_to_coolant_k_per_w)),
        (battery_node.name, coolant_name, _conductance_from_resistance(battery_node.resistance_to_coolant_k_per_w)),
    ]
    nodes = {
        motor_node.name: motor_node,
        inverter_node.name: inverter_node,
        battery_node.name: battery_node,
        coolant_name: coolant_node,
    }
    return ThermalNetwork(nodes=nodes, edges=edges, ambient_coupling={})


def _choose_target_dt_s(
    speed_mps: float, target_speed_mps: float, coolant_temp_k: float, ambient_temp_k: float, dt_policy: DTPolicy
) -> float:
    """`target_speed_mps` is considered alongside `speed_mps` (not just current
    speed) so that pulling away from a stop — current speed 0, target speed
    well above threshold — is classified as driving from the first step, not
    "parked." Using current speed alone picked a coarse (up to max_dt_s)
    step for that very first step in testing, coarse enough that vehicle
    speed integration diverged wildly before the loop ever got a chance to
    react (a vehicle-dynamics accuracy problem, not a thermal-stability one —
    `_stabilize_dt_s` wouldn't have caught it either).
    """
    if max(abs(speed_mps), abs(target_speed_mps)) > DRIVING_SPEED_THRESHOLD_MPS:
        return dt_policy.min_dt_s
    if abs(coolant_temp_k - ambient_temp_k) <= COOLANT_NEAR_AMBIENT_THRESHOLD_K:
        return dt_policy.max_dt_s
    return PARKED_WARM_DT_S


def _stabilize_dt_s(network: ThermalNetwork, battery_rc_tau_s: float, target_dt_s: float) -> float:
    """Halve `target_dt_s` until it's stable for both the combined thermal
    network AND the battery's separate RC-pair time constant (R1*C1) —
    `battery.compute_battery_electrical_step` has its own, unrelated Euler
    stability requirement that isn't part of `network` at all.
    """
    dt_s = target_dt_s
    for _ in range(MAX_DT_HALVINGS):
        time_constants_s = [
            tau
            for name in network.nodes
            if (tau := node_time_constant_s(network, name, coolant_flow_l_per_min=0.0)) is not None
        ]
        time_constants_s.append(battery_rc_tau_s)
        if dt_s < min(time_constants_s):
            return dt_s
        dt_s /= 2.0
    raise RuntimeError(f"Could not find a stable dt after {MAX_DT_HALVINGS} halvings from {target_dt_s:g}s.")


def initialize_sim_state(profile: ManufacturerProfile, scenario: Scenario, seed: int) -> SimState:
    """Builds a fresh t=0 `SimState` for `profile` under `scenario`'s climate.

    Initial temps come from each component's own `ThermalNode.initial_temp_k`;
    the coolant starts at ambient. Initial SOC comes from
    `scenario.charging_profile.typical_start_soc` as a reasonable default,
    not a scenario-specified "starting SOC" concept (none exists in the schema).
    """
    motor_node = profile.motors[0].thermal_node
    inverter_node = profile.inverter.thermal_node
    battery_node = profile.battery.thermal_node
    ambient_temp_k = scenario.climate.mean_annual_temp_k.value
    rng = np.random.default_rng(seed)

    return SimState(
        state_id=f"{scenario.name}-step-000000",
        scenario_id=scenario.name,
        sim_time_s=0.0,
        odometer_km=0.0,
        battery_soc=scenario.charging_profile.typical_start_soc.value,
        battery_soh=1.0,
        node_temps_k={
            motor_node.name: motor_node.initial_temp_k,
            inverter_node.name: inverter_node.initial_temp_k,
            battery_node.name: battery_node.initial_temp_k,
            COOLANT_NODE_NAME: ambient_temp_k,
        },
        rng_state=rng.bit_generator.state,
        fork_lineage=[],
        step_count=0,
        dt_current_s=scenario.dt_policy.min_dt_s,
        speed_mps=0.0,
        battery_v_rc_v=0.0,
        driver_integral_error_state=0.0,
        battery_throughput_ah=0.0,
        event_ids=[],
        battery_voltage_v=profile.battery.nominal_voltage_v.value,
    )


def advance_one_step(
    profile: ManufacturerProfile,
    scenario: Scenario,
    state: SimState,
    target_speed_mps: float,
    driver_aggression: float = DEFAULT_DRIVER_AGGRESSION,
    max_dt_s: float | None = None,
    grade_rad: float = DEFAULT_GRADE_RAD,
    pump_aggressiveness: float = DEFAULT_PUMP_AGGRESSIVENESS,
) -> SimStepRecord:
    """Advance `state` by one adaptively-chosen step (see module docstring for
    the 9-step sequence and why it's ordered this way). `max_dt_s`, if given,
    caps the chosen dt (used by `run_scenario` to avoid overshooting
    `duration_s` on the last step). `grade_rad` is a constant road grade for
    the whole run (default 0.0, flat); `pump_aggressiveness` scales the
    cooling duty-ramp response (default 1.0, matching the original
    unparameterized behavior) — see controller.step_controller's docstring.
    """
    motor_spec = profile.motors[0]
    inverter_spec = profile.inverter
    battery_spec = profile.battery
    vehicle_dynamics = profile.vehicle_dynamics
    cooling_spec = profile.thermal_architecture.cooling_loops[0]

    motor_name = motor_spec.thermal_node.name
    inverter_name = inverter_spec.thermal_node.name
    battery_name = battery_spec.thermal_node.name

    ambient_temp_k = scenario.climate.mean_annual_temp_k.value

    accel_limit_nm = motor_spec.peak_torque_nm.value
    brake_limit_nm = motor_spec.peak_torque_nm.value
    regen_limit_nm = REGEN_TORQUE_LIMIT_FRACTION_OF_PEAK * motor_spec.peak_torque_nm.value

    coolant_node = _coolant_thermal_node(cooling_spec, state.node_temps_k[COOLANT_NODE_NAME])
    network = _combined_thermal_network(profile, coolant_node)

    target_dt_s = _choose_target_dt_s(
        state.speed_mps, target_speed_mps, state.node_temps_k[COOLANT_NODE_NAME], ambient_temp_k, scenario.dt_policy
    )
    battery_rc_tau_s = battery_spec.r1_ohm.value * battery_spec.c1_farad.value
    dt_s = _stabilize_dt_s(network, battery_rc_tau_s, target_dt_s)
    if max_dt_s is not None:
        dt_s = min(dt_s, max_dt_s)

    causal_events: list[CausalEvent] = []

    # 1. driver
    driver_result = step_driver(
        target_speed_mps,
        state.speed_mps,
        dt_s,
        state.driver_integral_error_state,
        driver_aggression,
        accel_limit_nm,
        brake_limit_nm,
        regen_limit_nm,
        kp_nm_per_mps=SIM_DRIVER_KP_NM_PER_MPS,
        ki_nm_per_m=SIM_DRIVER_KI_NM_PER_M,
    )

    # 2. controller (all component state read from end of previous step)
    motor_speed_rad_per_s = motor_angular_velocity_rad_per_s(
        state.speed_mps, vehicle_dynamics.wheel_radius_m.value, vehicle_dynamics.gear_ratio.value
    )
    controller_result = step_controller(
        driver_result.torque_request_nm,
        motor_spec,
        inverter_spec,
        battery_spec,
        vehicle_dynamics,
        cooling_spec,
        state.node_temps_k[motor_name],
        state.node_temps_k[inverter_name],
        state.battery_soc,
        state.node_temps_k[battery_name],
        state.battery_v_rc_v,
        state.battery_voltage_v,
        motor_speed_rad_per_s,
        state.node_temps_k[COOLANT_NODE_NAME],
        state.sim_time_s,
        pump_aggressiveness=pump_aggressiveness,
    )
    causal_events.extend(controller_result.causal_events)

    # 3. motor + inverter heat (no thermal integration yet — see module docstring)
    motor_heat = compute_motor_heat(
        motor_spec,
        controller_result.commanded_torque_nm,
        motor_speed_rad_per_s,
        state.node_temps_k[motor_name],
        state.sim_time_s,
    )
    causal_events.extend(motor_heat.causal_events)
    inverter_heat = compute_inverter_heat(
        inverter_spec,
        state.battery_voltage_v,
        motor_heat.current_a,
        state.node_temps_k[inverter_name],
        state.sim_time_s,
    )
    causal_events.extend(inverter_heat.causal_events)

    aux_power_w = (
        profile.aux_loads.hvac_power_w.value
        + profile.aux_loads.accessory_load_w.value / profile.aux_loads.dc_dc_converter_efficiency.value
    )
    aux_current_a = aux_power_w / state.battery_voltage_v

    # 4. battery (no thermal integration yet)
    # Deliberately NOT controller_result.battery_current_demand_a: that's a
    # mechanical-power-only estimate (see ControllerStepResult's docstring),
    # good enough for arbitration but energy-inconsistent for the actual
    # battery draw — it ignores motor/inverter losses entirely, which would
    # make those losses appear from nowhere in the energy balance. DC bus
    # power = motor's electrical_power_w (already includes motor's own
    # losses) + inverter's own total_loss_w (its additional DC-side
    # dissipation), which is exactly what's available now that steps 3
    # have actually run.
    battery_current_request_a = (motor_heat.electrical_power_w + inverter_heat.total_loss_w) / (
        state.battery_voltage_v
    ) + aux_current_a
    battery_electrical = compute_battery_electrical_step(
        battery_spec,
        battery_current_request_a,
        state.battery_soc,
        state.node_temps_k[battery_name],
        state.battery_v_rc_v,
        dt_s,
        state.sim_time_s,
    )
    causal_events.extend(battery_electrical.causal_events)

    # 5. cooling
    cooling_result = step_cooling(
        cooling_spec,
        state.node_temps_k[COOLANT_NODE_NAME],
        ambient_temp_k,
        state.speed_mps,
        controller_result.commanded_pump_duty,
        controller_result.commanded_fan_duty,
    )

    # 6. one combined thermal solve
    heat_inputs_w = {
        motor_name: motor_heat.total_heat_w,
        inverter_name: inverter_heat.total_loss_w,
        battery_name: battery_electrical.irreversible_heat_w + battery_electrical.entropic_heat_w,
        COOLANT_NODE_NAME: -cooling_result.heat_rejected_w,
    }
    new_temps_k = step_thermal_network(
        network,
        state.node_temps_k,
        heat_inputs_w,
        ambient_temp_k=ambient_temp_k,
        coolant_temp_k=0.0,
        coolant_flow_l_per_min=0.0,
        dt_s=dt_s,
    )

    # 7. vehicle speed advance (grade_rad: constant whole-run slope, no route/elevation model yet)
    wheel_torque_nm = wheel_torque_from_motor_torque_nm(
        motor_heat.applied_torque_nm, vehicle_dynamics.gear_ratio.value, vehicle_dynamics.driveline_efficiency.value
    )
    wheel_force_n = wheel_force_from_torque_n(wheel_torque_nm, vehicle_dynamics.wheel_radius_m.value)
    drag_n = aerodynamic_drag_force_n(
        state.speed_mps,
        AIR_DENSITY_KG_PER_M3,
        vehicle_dynamics.drag_coefficient.value,
        vehicle_dynamics.frontal_area_m2.value,
    )
    rolling_n = rolling_resistance_force_n(
        vehicle_dynamics.curb_mass_kg.value, vehicle_dynamics.rolling_resistance_coefficient.value, grade_rad=grade_rad
    )
    grade_n = grade_force_n(vehicle_dynamics.curb_mass_kg.value, grade_rad)
    acceleration_mps2 = (wheel_force_n - drag_n - rolling_n - grade_n) / vehicle_dynamics.curb_mass_kg.value
    new_speed_mps = max(0.0, state.speed_mps + acceleration_mps2 * dt_s)

    # 8-9. assemble new state
    new_odometer_km = state.odometer_km + (state.speed_mps + new_speed_mps) / 2.0 * dt_s / 1000.0
    new_event_ids = [event.event_id for event in causal_events]

    new_state = SimState(
        state_id=f"{state.scenario_id}-step-{state.step_count + 1:06d}",
        scenario_id=state.scenario_id,
        sim_time_s=state.sim_time_s + dt_s,
        odometer_km=new_odometer_km,
        battery_soc=battery_electrical.soc,
        battery_soh=state.battery_soh,
        node_temps_k=new_temps_k,
        rng_state=state.rng_state,
        fork_lineage=state.fork_lineage,
        step_count=state.step_count + 1,
        dt_current_s=dt_s,
        speed_mps=new_speed_mps,
        battery_v_rc_v=battery_electrical.v_rc_v,
        driver_integral_error_state=driver_result.integral_error_state,
        battery_throughput_ah=state.battery_throughput_ah + abs(battery_electrical.applied_current_a) * dt_s / 3600.0,
        event_ids=[*state.event_ids, *new_event_ids],
        battery_voltage_v=battery_electrical.terminal_voltage_v,
    )

    return SimStepRecord(
        new_state=new_state,
        causal_events=causal_events,
        requested_torque_nm=driver_result.torque_request_nm,
        commanded_torque_nm=controller_result.commanded_torque_nm,
        motor_mechanical_power_w=motor_heat.mechanical_power_w,
        motor_electrical_power_w=motor_heat.electrical_power_w,
        battery_electrical_power_w=battery_electrical.terminal_voltage_v * battery_electrical.applied_current_a,
        battery_ocv_power_w=battery_electrical.ocv_v * battery_electrical.applied_current_a,
        pump_duty=controller_result.commanded_pump_duty,
        fan_duty=controller_result.commanded_fan_duty,
        heat_rejected_w=cooling_result.heat_rejected_w,
        motor_loss_w=motor_heat.total_heat_w,
        inverter_loss_w=inverter_heat.total_loss_w,
        battery_loss_w=battery_electrical.irreversible_heat_w + battery_electrical.entropic_heat_w,
        aux_power_w=aux_power_w,
        active_limit_event_types=[event.event_type for event in controller_result.causal_events],
    )


def run_scenario(
    profile: ManufacturerProfile,
    scenario: Scenario,
    duration_s: float,
    target_speed_mps: Callable[[float], float] = lambda _t: 0.0,
    initial_state: SimState | None = None,
    driver_aggression: float = DEFAULT_DRIVER_AGGRESSION,
    grade_rad: float = DEFAULT_GRADE_RAD,
    pump_aggressiveness: float = DEFAULT_PUMP_AGGRESSIVENESS,
) -> RunResult:
    """Run `profile` under `scenario` for `duration_s` seconds.

    `target_speed_mps` is a function of elapsed sim time (seconds) to target
    speed (m/s) — a temporary interface until drive-cycle generation exists
    (see CLAUDE.md); pass e.g. `lambda t: 20.0` for a constant-speed cruise.

    `initial_state`, if given, resumes from that state instead of building a
    fresh t=0 one (`initialize_sim_state`) — this is how fork replay works:
    call `run_scenario` again with a previous run's `final_state`.

    `grade_rad` and `pump_aggressiveness` are passed straight through to
    `advance_one_step` every step (see its docstring); both default to the
    original unparameterized behavior (flat road, unscaled duty ramp).
    """
    if initial_state is not None:
        state = initial_state
    else:
        state = initialize_sim_state(profile, scenario, scenario.random_seed)

    time_series = SimTimeSeries(
        time_s=[state.sim_time_s],
        speed_mps=[state.speed_mps],
        requested_torque_nm=[0.0],
        commanded_torque_nm=[0.0],
        motor_electrical_power_w=[0.0],
        soc=[state.battery_soc],
        node_temps_k={name: [temp] for name, temp in state.node_temps_k.items()},
        pump_duty=[0.0],
        fan_duty=[0.0],
        heat_rejected_w=[0.0],
        active_limit_event_types=[[]],
    )
    all_causal_events: list[CausalEvent] = []
    consumed_j = 0.0
    regen_recovered_j = 0.0
    motor_loss_j = 0.0
    inverter_loss_j = 0.0
    battery_loss_j = 0.0
    radiator_rejected_j = 0.0
    aux_j = 0.0

    end_time_s = state.sim_time_s + duration_s
    while state.sim_time_s < end_time_s:
        remaining_s = end_time_s - state.sim_time_s
        record = advance_one_step(
            profile,
            scenario,
            state,
            target_speed_mps(state.sim_time_s),
            driver_aggression,
            max_dt_s=remaining_s,
            grade_rad=grade_rad,
            pump_aggressiveness=pump_aggressiveness,
        )
        dt_s = record.new_state.sim_time_s - state.sim_time_s
        state = record.new_state
        all_causal_events.extend(record.causal_events)

        if record.battery_electrical_power_w >= 0.0:
            consumed_j += record.battery_electrical_power_w * dt_s
        else:
            regen_recovered_j += -record.battery_electrical_power_w * dt_s

        time_series.time_s.append(state.sim_time_s)
        time_series.speed_mps.append(state.speed_mps)
        time_series.requested_torque_nm.append(record.requested_torque_nm)
        time_series.commanded_torque_nm.append(record.commanded_torque_nm)
        time_series.motor_electrical_power_w.append(record.motor_electrical_power_w)
        time_series.soc.append(state.battery_soc)
        for name, temp in state.node_temps_k.items():
            time_series.node_temps_k.setdefault(name, []).append(temp)
        time_series.pump_duty.append(record.pump_duty)
        time_series.fan_duty.append(record.fan_duty)
        time_series.heat_rejected_w.append(record.heat_rejected_w)
        time_series.active_limit_event_types.append(record.active_limit_event_types)

        motor_loss_j += record.motor_loss_w * dt_s
        inverter_loss_j += record.inverter_loss_w * dt_s
        battery_loss_j += record.battery_loss_w * dt_s
        radiator_rejected_j += record.heat_rejected_w * dt_s
        aux_j += record.aux_power_w * dt_s

    energy_totals = EnergyTotals(
        consumed_j=consumed_j,
        regen_recovered_j=regen_recovered_j,
        motor_loss_j=motor_loss_j,
        inverter_loss_j=inverter_loss_j,
        battery_loss_j=battery_loss_j,
        radiator_rejected_j=radiator_rejected_j,
        aux_j=aux_j,
    )

    return RunResult(
        final_state=state,
        time_series=time_series,
        causal_events=all_causal_events,
        energy_totals=energy_totals,
    )
