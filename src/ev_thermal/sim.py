"""Main simulation loop: drives a `Scenario` forward in time using an adaptive
dt policy, composing driver/controller/motor/inverter/battery/cooling/thermal
steps into a sequence of `SimState`s, and collecting `CausalEvent`s as it goes.

Scope for this milestone: short-duration runs (minutes to hours) via
`run_scenario`. Long-horizon mode (weeks to years, adaptive soak steps,
checkpointing), Monte Carlo, fork-branching UI, and file I/O are all
out of scope here — see docs/MILESTONES.md's deferred-work notes.

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
5. `cooling.step_cooling` — heat rejected by the radiator, called ONCE PER
   `CoolingLoop` (see the topology note below), each using that loop's own
   coolant node's current (start-of-step) temperature as its
   `coolant_inlet_temp_k`.
6. `thermal.step_thermal_network` — ONE combined solve across motor+inverter+
   battery+ALL coolant nodes together (one per loop), with each component's
   heat as a source, each loop's radiator rejection as a sink on ITS OWN
   coolant node, and edges (not the ad-hoc per-node "coolant boundary"
   mechanism) connecting each component to the coolant node(s) of whichever
   loop(s) actually serve it, per `CoolingLoop.serves`, using
   `1/resistance_to_coolant_k_per_w` as edge conductance. A component no
   loop serves gets no coolant edge at all — it falls back to whatever
   `ThermalNode.resistance_to_ambient_k_per_w` path it has (see the
   ambient-coupling milestone note in docs/MILESTONES.md), or is fully adiabatic if
   that's also unset. This is why steps 3-4 don't advance temperature
   themselves: a real coolant node needs all nodes integrated together, not
   isolated per-node solves. `coolant_flow_l_per_min=0.0` is passed to
   disable the legacy per-node boundary mechanism for this call, so heat
   only flows through the explicit edges (no double-counting).
7. Vehicle speed advance — motor's actually-applied torque converted back to
   wheel force via the driveline (the exact inverse of how
   `vehicle.motor_torque_request_nm` derived the request), net against road
   load at the CURRENT (pre-step) speed, integrated to a new speed. `grade_rad`
   (default 0.0, flat) is a single scalar applied for the whole run via
   `vehicle.rolling_resistance_force_n`/`vehicle.grade_force_n` — still not a
   route/elevation profile (no per-position grade model exists), just a
   constant road slope for the dashboard's "road grade" control.
8. Collect every component's `CausalEvent`s (controller's are authoritative;
   motor/inverter/battery still emit their own too, per docs/MILESTONES.md's note on
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

Single-motor simplification: `profile.motors[0]` is treated as THE motor,
regardless of dual-motor support in the schema. Dual-motor orchestration is
deferred.

Multi-loop topology (Milestones A-D documented in docs/MILESTONES.md — A added
`resistance_to_ambient_k_per_w`, B made `CoolingLoop.serves` authoritative,
this is C, D is the still-deferred refrigerant-loop abstraction):
`_combined_thermal_network` builds one coolant `ThermalNode` per entry in
`cooling_loops` (named via `coolant_node_name(loop.name)`, not one global
name), and edges each component to only the loop(s) whose `serves` list
names it. `controller.py`'s pump/fan duty is now genuinely per-loop —
`ControllerStepResult.commanded_pump_duty`/`commanded_fan_duty` are
`dict[str, float]` keyed by `CoolingLoop.name`, one entry per loop, each
computed from only the components *that* loop serves against *that* loop's
own thermostat. `step_cooling` is called once per loop with that loop's own
commanded duty (no more broadcasting one shared value, which was Milestone
B's explicit, temporary compromise).

Milestone E1 (long-horizon aging mode) additions:
- Step 4.5, right after the battery's electrical step: `aging.compute_aging_step`
  runs against the RAW (unaged) `profile.battery` spec and updates
  `SimState.battery_soh`. Every OTHER use of the battery spec in this
  function (controller arbitration, the actual electrical step) uses
  `aging.apply_soh_to_battery_spec(profile.battery, state.battery_soh)`
  instead — a capacity/resistance-scaled copy — computed once at the top of
  `advance_one_step` as `aged_battery_spec`, so aging feeds back into the
  pack's usable capacity and internal resistance every step, not just at
  the end of a run.
- `_choose_target_dt_s` gained a fourth tier, gated on `SimState.parked_duration_s`
  (time since the vehicle last drove or charged) rather than the pre-existing
  "coolant near ambient" temperature check alone: that check has a real
  hysteresis bug — any nonzero steady parasitic heat while parked can hold
  the coolant at an equilibrium offset from ambient that never closes back
  inside `COOLANT_NEAR_AMBIENT_THRESHOLD_K`, permanently pinning dt at
  `PARKED_WARM_DT_S` for the rest of a long run. Gating the new extended tier
  on elapsed parked TIME instead can't get stuck the same way — it only ever
  increases while parked. `_stabilize_dt_s` still has the final say: the
  extended tier's target is a request, clipped down to whatever's actually
  stable every step.
- `advance_one_step` accepts an optional pre-built `network`; `run_scenario`
  builds the combined `ThermalNetwork` once (its structure — capacitances,
  resistances, edges — never changes over a run; `ThermalNode.initial_temp_k`
  isn't read by `thermal.step_thermal_network`, which takes current temps as
  a separate argument) and passes it to every step, instead of rebuilding it
  from scratch every step. `network=None` (the default) preserves
  `advance_one_step`'s original build-it-fresh, standalone-callable behavior.
- `run_scenario` gained `record_interval_s` (default 0.0, unchanged
  every-step recording) to decimate `RunResult.time_series` for long-horizon
  runs; `RunResult.final_state` and the energy integrals are never decimated,
  only the time series arrays used for plotting.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from ev_thermal.aging import apply_soh_to_battery_spec, compute_aging_step
from ev_thermal.battery import compute_battery_electrical_step
from ev_thermal.controller import step_controller
from ev_thermal.cooling import AIR_DENSITY_KG_PER_M3, step_cooling
from ev_thermal.driver import step_driver
from ev_thermal.inverter import compute_inverter_heat
from ev_thermal.motor import compute_motor_heat
from ev_thermal.refrigerant import step_refrigerant_loop
from ev_thermal.schema import (
    CausalEvent,
    Confidence,
    CoolingLoop,
    DTPolicy,
    EnergyTotals,
    ManufacturerProfile,
    RefrigerantLoop,
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

DRIVING_SPEED_THRESHOLD_MPS = 0.1
COOLANT_NEAR_AMBIENT_THRESHOLD_K = 2.0
PARKED_WARM_DT_S = 5.0
"""TODO(tuning): placeholder middle tier of the adaptive dt policy."""

EXTENDED_PARKING_SETTLING_S = 3.0 * 3600.0
"""TODO(tuning): duration of continuous non-driving time after which the
extended-parking dt tier is requested regardless of the coolant-vs-ambient
temperature check. See the module docstring's Milestone E1 section for why
this exists: the temperature-only check can pin dt at PARKED_WARM_DT_S
forever if a steady parasitic heat load keeps coolant temperature at an
equilibrium offset from ambient. Gating on elapsed parked time instead
cannot get stuck the same way."""

EXTENDED_PARKING_TARGET_DT_S = 86400.0
"""TODO(tuning): requested (not guaranteed) dt once EXTENDED_PARKING_SETTLING_S
has elapsed. `_stabilize_dt_s` still halves this down to whatever's actually
stable for the current network/battery RC time constants every step — this
is a request, not a bypass of the stability check."""

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


def coolant_node_name(loop_name: str) -> str:
    """Public: the coolant ThermalNode name for a given CoolingLoop's own
    `.name` — one per loop, not one shared global name. Used by callers
    (dashboard, examples/first_run.py, tests) that need to enumerate a
    profile's coolant nodes without reaching into sim.py's private helpers.
    """
    return f"coolant_{loop_name}"


def coolant_node_names(profile: ManufacturerProfile) -> list[str]:
    """Public: every coolant node name this profile's loops produce, in
    `cooling_loops` order.
    """
    return [coolant_node_name(loop.name) for loop in profile.thermal_architecture.cooling_loops]


def _coolant_thermal_nodes(cooling_loops: list[CoolingLoop], temps_k: dict[str, float]) -> dict[str, ThermalNode]:
    """Builds one lumped coolant `ThermalNode` per `CoolingLoop`, keyed by
    `coolant_node_name`.

    Capacitance is derived from each loop's own volume/density/specific heat.
    `max_temp_k`/`derate_temp_k`/`resistance_to_coolant_k_per_w` are required
    by `ThermalNode` but genuinely unused here: nothing reads a coolant
    node's own derate/max-temp limits or its own coolant-boundary resistance
    (only the explicit edges to it matter in the combined network), so
    they're set to obviously-non-binding placeholders, not spec values.
    `temps_k` supplies each node's current (not "initial" in the run-lifetime
    sense) temp, falling back to ambient if a name isn't present yet.
    """
    nodes: dict[str, ThermalNode] = {}
    for loop in cooling_loops:
        name = coolant_node_name(loop.name)
        capacitance_j_per_k = (
            loop.loop_volume_l.value / 1000.0 * loop.coolant_density_kg_per_m3.value
            * loop.coolant_specific_heat_j_per_kg_k.value
        )
        nodes[name] = ThermalNode(
            name=name,
            capacitance_j_per_k=_sourced(capacitance_j_per_k, "derived from loop_volume_l/density/specific_heat"),
            resistance_to_coolant_k_per_w=_sourced(1.0, "unused for the coolant node itself"),
            initial_temp_k=temps_k[name],
            max_temp_k=_sourced(1000.0, "unused: no derate curve applies to the coolant node itself"),
            derate_temp_k=_sourced(1000.0, "unused: no derate curve applies to the coolant node itself"),
        )
    return nodes


def _combined_thermal_network(profile: ManufacturerProfile, coolant_nodes: dict[str, ThermalNode]) -> ThermalNetwork:
    """`CoolingLoop.serves` is authoritative here: a component gets a coolant
    edge only for the loop(s) whose `serves` names it. A component no loop
    serves gets no coolant edge at all — its only remaining path is whatever
    `resistance_to_ambient_k_per_w` it has (Milestone A), or none.
    """
    component_nodes = {
        profile.motors[0].thermal_node.name: profile.motors[0].thermal_node,
        profile.inverter.thermal_node.name: profile.inverter.thermal_node,
        profile.battery.thermal_node.name: profile.battery.thermal_node,
    }

    edges = []
    for loop in profile.thermal_architecture.cooling_loops:
        coolant_name = coolant_node_name(loop.name)
        for served_name in loop.serves:
            component_node = component_nodes.get(served_name)
            if component_node is not None:
                edges.append(
                    (
                        component_node.name,
                        coolant_name,
                        _conductance_from_resistance(component_node.resistance_to_coolant_k_per_w),
                    )
                )

    nodes = {**component_nodes, **coolant_nodes}
    return ThermalNetwork(nodes=nodes, edges=edges, ambient_coupling={})


def _step_refrigerant_loops(
    refrigerant_loops: list[RefrigerantLoop],
    commanded_duty: dict[str, float],
    component_temps_k: dict[str, float],
    component_resistances_to_coolant_k_per_w: dict[str, float],
) -> tuple[dict[str, float], float]:
    """Returns (component_name -> heat_removed_w, total compressor power_w)
    across every `RefrigerantLoop` (Milestone D). Computed from START-of-step
    temps, same convention every other quantity in `advance_one_step`/
    `advance_charging_step` uses -- this runs before the battery step (which
    needs total power for `battery_current_request_a`), well before
    `heat_inputs_w` even exists; the caller applies the returned dict to
    `heat_inputs_w` separately, once that's built (step 6). Split evenly if
    a loop's `serves` names more than one component -- untested case for now
    (every current profile's RefrigerantLoop serves exactly one component),
    not chased further. See `refrigerant.step_refrigerant_loop`'s own
    docstring for why heat_removed_w is resistance-capped, not just
    capacity*duty.
    """
    heat_removed_w_by_component: dict[str, float] = {}
    total_power_w = 0.0
    for loop in refrigerant_loops:
        duty = commanded_duty[loop.name]
        if not loop.serves:
            continue
        total_power_w += loop.power_w.value * duty
        capacity_share_w = loop.capacity_w.value / len(loop.serves)
        for name in loop.serves:
            component_temp_k = component_temps_k.get(name)
            resistance_k_per_w = component_resistances_to_coolant_k_per_w.get(name)
            if component_temp_k is None or resistance_k_per_w is None:
                continue
            heat_removed_w, _power_w = step_refrigerant_loop(
                loop, duty, component_temp_k, resistance_k_per_w, capacity_share_w
            )
            heat_removed_w_by_component[name] = heat_removed_w_by_component.get(name, 0.0) + heat_removed_w
    return heat_removed_w_by_component, total_power_w


def _is_driving(speed_mps: float, target_speed_mps: float) -> bool:
    """`target_speed_mps` is considered alongside `speed_mps` (not just current
    speed) so that pulling away from a stop — current speed 0, target speed
    well above threshold — is classified as driving from the first step, not
    "parked." Using current speed alone picked a coarse (up to max_dt_s) step
    for that very first step in testing, coarse enough that vehicle speed
    integration diverged wildly before the loop ever got a chance to react (a
    vehicle-dynamics accuracy problem, not a thermal-stability one —
    `_stabilize_dt_s` wouldn't have caught it either).
    """
    return max(abs(speed_mps), abs(target_speed_mps)) > DRIVING_SPEED_THRESHOLD_MPS


def _choose_target_dt_s(
    speed_mps: float,
    target_speed_mps: float,
    coolant_temps_k: list[float],
    ambient_temp_k: float,
    dt_policy: DTPolicy,
    parked_duration_s: float,
) -> float:
    """`coolant_temps_k` is one temperature per loop (multi-loop profiles have
    more than one) — "coolant near ambient" means ALL of them are, the
    conservative direction: staying in the fine-dt tier until every loop has
    settled, not just the first one.

    `parked_duration_s` (elapsed time since last driving/charging, tracked on
    `SimState`) drives a fourth, extended-parking tier once
    `EXTENDED_PARKING_SETTLING_S` has elapsed, checked BEFORE the
    coolant-near-ambient tier — see EXTENDED_PARKING_SETTLING_S's docstring
    for why a temperature-only check isn't enough on its own for long-horizon
    runs.
    """
    if _is_driving(speed_mps, target_speed_mps):
        return dt_policy.min_dt_s
    if parked_duration_s >= EXTENDED_PARKING_SETTLING_S:
        return EXTENDED_PARKING_TARGET_DT_S
    if max(abs(temp - ambient_temp_k) for temp in coolant_temps_k) <= COOLANT_NEAR_AMBIENT_THRESHOLD_K:
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
    every loop's coolant node starts at ambient. Initial SOC comes from
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
            **{name: ambient_temp_k for name in coolant_node_names(profile)},
        },
        rng_state=rng.bit_generator.state,
        fork_lineage=[],
        step_count=0,
        dt_current_s=scenario.dt_policy.min_dt_s,
        speed_mps=0.0,
        battery_v_rc_v=0.0,
        driver_integral_error_state=0.0,
        battery_throughput_ah=0.0,
        battery_voltage_v=profile.battery.nominal_voltage_v.value,
        parked_duration_s=0.0,
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
    network: ThermalNetwork | None = None,
) -> SimStepRecord:
    """Advance `state` by one adaptively-chosen step (see module docstring for
    the 9-step sequence and why it's ordered this way). `max_dt_s`, if given,
    caps the chosen dt (used by `run_scenario` to avoid overshooting
    `duration_s` on the last step). `grade_rad` is a constant road grade for
    the whole run (default 0.0, flat); `pump_aggressiveness` scales the
    cooling duty-ramp response (default 1.0, matching the original
    unparameterized behavior) — see controller.step_controller's docstring.
    `network`, if given, is a pre-built combined `ThermalNetwork` (its
    structure never changes over a run — see the module docstring's
    Milestone E1 section); `None` (the default) builds one fresh, preserving
    standalone-callable behavior for callers other than `run_scenario`.
    """
    motor_spec = profile.motors[0]
    inverter_spec = profile.inverter
    battery_spec = profile.battery
    aged_battery_spec = apply_soh_to_battery_spec(battery_spec, state.battery_soh)
    vehicle_dynamics = profile.vehicle_dynamics
    cooling_loops = profile.thermal_architecture.cooling_loops
    refrigerant_loops = profile.thermal_architecture.refrigerant_loops

    motor_name = motor_spec.thermal_node.name
    inverter_name = inverter_spec.thermal_node.name
    battery_name = battery_spec.thermal_node.name

    ambient_temp_k = scenario.climate.mean_annual_temp_k.value

    accel_limit_nm = motor_spec.peak_torque_nm.value
    brake_limit_nm = motor_spec.peak_torque_nm.value
    regen_limit_nm = REGEN_TORQUE_LIMIT_FRACTION_OF_PEAK * motor_spec.peak_torque_nm.value

    if network is None:
        coolant_nodes = _coolant_thermal_nodes(cooling_loops, state.node_temps_k)
        network = _combined_thermal_network(profile, coolant_nodes)
    coolant_names = coolant_node_names(profile)
    # keyed by CoolingLoop.name (controller.py's own vocabulary), not by sim.py's
    # internal coolant_node_name() -- see controller.py's module docstring
    coolant_temps_k = {loop.name: state.node_temps_k[coolant_node_name(loop.name)] for loop in cooling_loops}

    target_dt_s = _choose_target_dt_s(
        state.speed_mps,
        target_speed_mps,
        [state.node_temps_k[name] for name in coolant_names],
        ambient_temp_k,
        scenario.dt_policy,
        state.parked_duration_s,
    )
    battery_rc_tau_s = aged_battery_spec.r1_ohm.value * aged_battery_spec.c1_farad.value
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
        aged_battery_spec,
        vehicle_dynamics,
        cooling_loops,
        state.node_temps_k[motor_name],
        state.node_temps_k[inverter_name],
        state.battery_soc,
        state.node_temps_k[battery_name],
        state.battery_v_rc_v,
        state.battery_voltage_v,
        motor_speed_rad_per_s,
        coolant_temps_k,
        state.sim_time_s,
        refrigerant_loops=refrigerant_loops,
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

    # 3.5. cooling — once per loop, each driven by that loop's own commanded
    # duty. Moved ahead of the battery step (Q-0021): step_cooling depends
    # only on controller_result (step 2) and start-of-step state, never on
    # motor/inverter/battery's own step results, so this reordering changes
    # no physics — it only makes cooling_results available in time to charge
    # pump/fan/chiller draw to the battery's actual current request below,
    # closing a pre-existing gap where that draw was computed but never
    # charged to anything (see docs/DESIGN_QUESTIONS.md Q-0021).
    cooling_results = {
        loop.name: step_cooling(
            loop,
            state.node_temps_k[coolant_node_name(loop.name)],
            ambient_temp_k,
            state.speed_mps,
            controller_result.commanded_pump_duty[loop.name],
            controller_result.commanded_fan_duty[loop.name],
            controller_result.commanded_chiller_duty[loop.name],
        )
        for loop in cooling_loops
    }
    total_heat_rejected_w = sum(result.heat_rejected_w for result in cooling_results.values())
    total_chiller_heat_removed_w = sum(result.chiller_heat_removed_w for result in cooling_results.values())

    # 3.6. refrigerant loops (Milestone D) -- no coolant to gate on, so this
    # runs unconditionally at the commanded duty; see refrigerant.py's module
    # docstring for why heat_removed_w is resistance-capped, and this
    # function's own docstring for why it returns a dict to apply later
    # (step 6) instead of mutating heat_inputs_w directly the way cooling_
    # results's chiller path does.
    component_temps_k_for_refrigerant = {
        motor_name: state.node_temps_k[motor_name],
        inverter_name: state.node_temps_k[inverter_name],
        battery_name: state.node_temps_k[battery_name],
    }
    component_resistances_to_coolant_k_per_w = {
        motor_name: motor_spec.thermal_node.resistance_to_coolant_k_per_w.value,
        inverter_name: inverter_spec.thermal_node.resistance_to_coolant_k_per_w.value,
        battery_name: battery_spec.thermal_node.resistance_to_coolant_k_per_w.value,
    }
    refrigerant_heat_removed_w_by_component, refrigerant_parasitic_w = _step_refrigerant_loops(
        refrigerant_loops,
        controller_result.commanded_refrigerant_duty,
        component_temps_k_for_refrigerant,
        component_resistances_to_coolant_k_per_w,
    )
    total_refrigerant_heat_removed_w = sum(refrigerant_heat_removed_w_by_component.values())

    # cooling_parasitic_w folds in the refrigerant compressor's own draw
    # (Milestone D) -- same HV-bus parasitic-draw bucket pump/fan/chiller
    # already share, not a separate accounting category.
    cooling_parasitic_w = (
        sum(result.pump_power_w + result.fan_power_w + result.chiller_power_w for result in cooling_results.values())
        + refrigerant_parasitic_w
    )
    # Cooling-loop electrics (pump/chiller/refrigerant compressor/fan) are
    # HV-bus components in a real EV, not 12V accessories -- same treatment
    # as hvac_power_w above, no dc_dc_converter_efficiency correction.
    cooling_current_a = cooling_parasitic_w / state.battery_voltage_v

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
    battery_current_request_a = (
        (motor_heat.electrical_power_w + inverter_heat.total_loss_w) / state.battery_voltage_v
        + aux_current_a
        + cooling_current_a
    )
    battery_electrical = compute_battery_electrical_step(
        aged_battery_spec,
        battery_current_request_a,
        state.battery_soc,
        state.node_temps_k[battery_name],
        state.battery_v_rc_v,
        dt_s,
        state.sim_time_s,
    )
    causal_events.extend(battery_electrical.causal_events)

    # 4.5. battery aging -- against the RAW (unaged) spec: aging-rate
    # coefficients themselves don't change with SOH, only the pack's
    # resulting capacity/resistance do (that's aged_battery_spec, used
    # everywhere else above).
    aging_result = compute_aging_step(
        battery_spec,
        state.battery_soh,
        state.node_temps_k[battery_name],
        state.battery_soc,
        battery_electrical.applied_current_a,
        dt_s,
    )

    # 6. one combined thermal solve
    heat_inputs_w = {
        motor_name: motor_heat.total_heat_w,
        inverter_name: inverter_heat.total_loss_w,
        battery_name: battery_electrical.irreversible_heat_w + battery_electrical.entropic_heat_w,
        **{
            coolant_node_name(loop.name): -(
                cooling_results[loop.name].heat_rejected_w + cooling_results[loop.name].chiller_heat_removed_w
            )
            for loop in cooling_loops
        },
    }
    for name, refrigerant_heat_removed_w in refrigerant_heat_removed_w_by_component.items():
        heat_inputs_w[name] = heat_inputs_w.get(name, 0.0) - refrigerant_heat_removed_w
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
    new_parked_duration_s = (
        0.0 if _is_driving(state.speed_mps, target_speed_mps) else state.parked_duration_s + dt_s
    )

    new_state = SimState(
        state_id=f"{state.scenario_id}-step-{state.step_count + 1:06d}",
        scenario_id=state.scenario_id,
        sim_time_s=state.sim_time_s + dt_s,
        odometer_km=new_odometer_km,
        battery_soc=battery_electrical.soc,
        battery_soh=aging_result.new_soh,
        node_temps_k=new_temps_k,
        rng_state=state.rng_state,
        fork_lineage=state.fork_lineage,
        step_count=state.step_count + 1,
        dt_current_s=dt_s,
        speed_mps=new_speed_mps,
        battery_v_rc_v=battery_electrical.v_rc_v,
        driver_integral_error_state=driver_result.integral_error_state,
        battery_throughput_ah=state.battery_throughput_ah + abs(battery_electrical.applied_current_a) * dt_s / 3600.0,
        battery_voltage_v=battery_electrical.terminal_voltage_v,
        parked_duration_s=new_parked_duration_s,
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
        chiller_duty=controller_result.commanded_chiller_duty,
        refrigerant_duty=controller_result.commanded_refrigerant_duty,
        heat_rejected_w=total_heat_rejected_w,
        chiller_heat_removed_w=total_chiller_heat_removed_w,
        refrigerant_heat_removed_w=total_refrigerant_heat_removed_w,
        motor_loss_w=motor_heat.total_heat_w,
        inverter_loss_w=inverter_heat.total_loss_w,
        battery_loss_w=battery_electrical.irreversible_heat_w + battery_electrical.entropic_heat_w,
        aux_power_w=aux_power_w,
        cooling_parasitic_w=cooling_parasitic_w,
        active_limit_event_types=[event.event_type for event in controller_result.causal_events],
        applied_current_a=battery_electrical.applied_current_a,
        charging=False,
    )


def advance_charging_step(
    profile: ManufacturerProfile,
    scenario: Scenario,
    state: SimState,
    charge_power_request_w: float,
    max_dt_s: float | None = None,
    pump_aggressiveness: float = DEFAULT_PUMP_AGGRESSIVENESS,
    network: ThermalNetwork | None = None,
) -> SimStepRecord:
    """Milestone E2e: advance `state` by one step of an external DC charging
    session -- the charging-mode PEER to `advance_one_step`, not a variant of
    it (see the module's Milestone E2e section, and docs/MILESTONES.md's
    deferred work, for the full reasoning this design was ratified against).
    No driver, no motor/inverter mechanical output, vehicle stationary.

    Reuses the exact same underlying calls `advance_one_step` uses for
    everything that's shared, so charge and discharge heat generation can
    never diverge between the two step functions: `battery.
    compute_battery_electrical_step` (already charge-capable -- already
    exercised today via regen's negative current request -- and, as of this
    milestone, already routes through the extended `available_charge_
    current_a`'s BMS current-limit curve with no further code needed here),
    `controller.step_controller` (called with a zero torque request purely
    for its cooling-duty output, verified independent of the torque-
    arbitration path -- see docs/MILESTONES.md), and `thermal.step_thermal_network`
    over the same combined network, with motor/inverter heat inputs at zero.

    dt is NOT chosen via `_choose_target_dt_s`'s tier-selection logic --
    charging has only one regime (actively charging), not a driving-vs-
    parked choice to make -- it's `scenario.dt_policy.min_dt_s` directly,
    still stability-checked via the unchanged `_stabilize_dt_s`.

    Setting `SimState.parked_duration_s` to 0.0 here is not new behavior
    invented for this function -- it's what that field's own docstring
    ("elapsed time... since the vehicle was last driving OR CHARGING")
    already specified. This finally wires up the "or charging" half of the
    adaptive-dt policy's original spec, previously an explicit known gap
    (see the module docstring's Milestone E1 section) since no charging
    model existed to make "parked but charging" distinguishable at all.
    """
    battery_spec = profile.battery
    aged_battery_spec = apply_soh_to_battery_spec(battery_spec, state.battery_soh)
    motor_spec = profile.motors[0]
    inverter_spec = profile.inverter
    cooling_loops = profile.thermal_architecture.cooling_loops
    refrigerant_loops = profile.thermal_architecture.refrigerant_loops

    motor_name = motor_spec.thermal_node.name
    inverter_name = inverter_spec.thermal_node.name
    battery_name = battery_spec.thermal_node.name

    ambient_temp_k = scenario.climate.mean_annual_temp_k.value

    if network is None:
        coolant_nodes = _coolant_thermal_nodes(cooling_loops, state.node_temps_k)
        network = _combined_thermal_network(profile, coolant_nodes)
    coolant_temps_k = {loop.name: state.node_temps_k[coolant_node_name(loop.name)] for loop in cooling_loops}

    battery_rc_tau_s = aged_battery_spec.r1_ohm.value * aged_battery_spec.c1_farad.value
    dt_s = _stabilize_dt_s(network, battery_rc_tau_s, scenario.dt_policy.min_dt_s)
    if max_dt_s is not None:
        dt_s = min(dt_s, max_dt_s)

    causal_events: list[CausalEvent] = []

    # cooling duty only -- torque-arbitration output unused, its independence
    # from driver_torque_request_nm was verified by reading step_controller's
    # full body before this design was ratified (see docs/MILESTONES.md)
    controller_result = step_controller(
        0.0,
        motor_spec,
        inverter_spec,
        aged_battery_spec,
        profile.vehicle_dynamics,
        cooling_loops,
        state.node_temps_k[motor_name],
        state.node_temps_k[inverter_name],
        state.battery_soc,
        state.node_temps_k[battery_name],
        state.battery_v_rc_v,
        state.battery_voltage_v,
        0.0,
        coolant_temps_k,
        state.sim_time_s,
        refrigerant_loops=refrigerant_loops,
        pump_aggressiveness=pump_aggressiveness,
    )

    # cooling computed before the battery step (Q-0021, same reordering
    # reasoning as advance_one_step): step_cooling depends only on
    # controller_result and start-of-step state, so this changes no physics,
    # it just makes cooling_parasitic_w available to net against the charge
    # request below. Real fast-charging sessions actively run thermal
    # management (this is exactly when a chiller matters most), so charging
    # a real, if imperfect, share of that draw against the charger's power
    # budget is more honest than silently giving it away for free.
    # Deliberately NOT extended to aux_power_w/hvac -- advance_charging_step
    # has never modeled cabin HVAC/accessory draw during a charging session
    # at all, a separate, pre-existing simplification this fix does not
    # touch (see docs/ROADMAP.md's "Charging model gaps" section).
    cooling_results = {
        loop.name: step_cooling(
            loop,
            state.node_temps_k[coolant_node_name(loop.name)],
            ambient_temp_k,
            state.speed_mps,
            controller_result.commanded_pump_duty[loop.name],
            controller_result.commanded_fan_duty[loop.name],
            controller_result.commanded_chiller_duty[loop.name],
        )
        for loop in cooling_loops
    }
    total_heat_rejected_w = sum(result.heat_rejected_w for result in cooling_results.values())
    total_chiller_heat_removed_w = sum(result.chiller_heat_removed_w for result in cooling_results.values())

    # refrigerant loops (Milestone D) -- same treatment as advance_one_step;
    # see that function's own comments and _step_refrigerant_loops'/
    # refrigerant.py's module docstrings.
    component_temps_k_for_refrigerant = {
        motor_name: state.node_temps_k[motor_name],
        inverter_name: state.node_temps_k[inverter_name],
        battery_name: state.node_temps_k[battery_name],
    }
    component_resistances_to_coolant_k_per_w = {
        motor_name: motor_spec.thermal_node.resistance_to_coolant_k_per_w.value,
        inverter_name: inverter_spec.thermal_node.resistance_to_coolant_k_per_w.value,
        battery_name: battery_spec.thermal_node.resistance_to_coolant_k_per_w.value,
    }
    refrigerant_heat_removed_w_by_component, refrigerant_parasitic_w = _step_refrigerant_loops(
        refrigerant_loops,
        controller_result.commanded_refrigerant_duty,
        component_temps_k_for_refrigerant,
        component_resistances_to_coolant_k_per_w,
    )
    total_refrigerant_heat_removed_w = sum(refrigerant_heat_removed_w_by_component.values())

    # cooling_parasitic_w folds in the refrigerant compressor's own draw
    # (Milestone D), same bucket pump/fan/chiller already share.
    cooling_parasitic_w = (
        sum(result.pump_power_w + result.fan_power_w + result.chiller_power_w for result in cooling_results.values())
        + refrigerant_parasitic_w
    )

    charge_current_request_a = -(charge_power_request_w - cooling_parasitic_w) / state.battery_voltage_v
    battery_electrical = compute_battery_electrical_step(
        aged_battery_spec,
        charge_current_request_a,
        state.battery_soc,
        state.node_temps_k[battery_name],
        state.battery_v_rc_v,
        dt_s,
        state.sim_time_s,
    )
    causal_events.extend(battery_electrical.causal_events)

    aging_result = compute_aging_step(
        battery_spec,
        state.battery_soh,
        state.node_temps_k[battery_name],
        state.battery_soc,
        battery_electrical.applied_current_a,
        dt_s,
    )

    heat_inputs_w = {
        motor_name: 0.0,
        inverter_name: 0.0,
        battery_name: battery_electrical.irreversible_heat_w + battery_electrical.entropic_heat_w,
        **{
            coolant_node_name(loop.name): -(
                cooling_results[loop.name].heat_rejected_w + cooling_results[loop.name].chiller_heat_removed_w
            )
            for loop in cooling_loops
        },
    }
    for name, refrigerant_heat_removed_w in refrigerant_heat_removed_w_by_component.items():
        heat_inputs_w[name] = heat_inputs_w.get(name, 0.0) - refrigerant_heat_removed_w
    new_temps_k = step_thermal_network(
        network,
        state.node_temps_k,
        heat_inputs_w,
        ambient_temp_k=ambient_temp_k,
        coolant_temp_k=0.0,
        coolant_flow_l_per_min=0.0,
        dt_s=dt_s,
    )

    new_state = SimState(
        state_id=f"{state.scenario_id}-step-{state.step_count + 1:06d}",
        scenario_id=state.scenario_id,
        sim_time_s=state.sim_time_s + dt_s,
        odometer_km=state.odometer_km,
        battery_soc=battery_electrical.soc,
        battery_soh=aging_result.new_soh,
        node_temps_k=new_temps_k,
        rng_state=state.rng_state,
        fork_lineage=state.fork_lineage,
        step_count=state.step_count + 1,
        dt_current_s=dt_s,
        speed_mps=0.0,
        battery_v_rc_v=battery_electrical.v_rc_v,
        driver_integral_error_state=state.driver_integral_error_state,
        battery_throughput_ah=state.battery_throughput_ah + abs(battery_electrical.applied_current_a) * dt_s / 3600.0,
        battery_voltage_v=battery_electrical.terminal_voltage_v,
        parked_duration_s=0.0,
    )

    return SimStepRecord(
        new_state=new_state,
        causal_events=causal_events,
        requested_torque_nm=0.0,
        commanded_torque_nm=0.0,
        motor_mechanical_power_w=0.0,
        motor_electrical_power_w=0.0,
        battery_electrical_power_w=battery_electrical.terminal_voltage_v * battery_electrical.applied_current_a,
        battery_ocv_power_w=battery_electrical.ocv_v * battery_electrical.applied_current_a,
        pump_duty=controller_result.commanded_pump_duty,
        fan_duty=controller_result.commanded_fan_duty,
        chiller_duty=controller_result.commanded_chiller_duty,
        refrigerant_duty=controller_result.commanded_refrigerant_duty,
        heat_rejected_w=total_heat_rejected_w,
        chiller_heat_removed_w=total_chiller_heat_removed_w,
        refrigerant_heat_removed_w=total_refrigerant_heat_removed_w,
        motor_loss_w=0.0,
        inverter_loss_w=0.0,
        battery_loss_w=battery_electrical.irreversible_heat_w + battery_electrical.entropic_heat_w,
        aux_power_w=0.0,
        cooling_parasitic_w=cooling_parasitic_w,
        active_limit_event_types=[event.event_type for event in controller_result.causal_events],
        applied_current_a=battery_electrical.applied_current_a,
        charging=True,
    )


def advance_preconditioning_step(
    profile: ManufacturerProfile,
    scenario: Scenario,
    state: SimState,
    heater_power_w: float,
    max_dt_s: float | None = None,
    pump_aggressiveness: float = DEFAULT_PUMP_AGGRESSIVENESS,
    network: ThermalNetwork | None = None,
) -> SimStepRecord:
    """Real preconditioning (docs/DESIGN_QUESTIONS.md Q-0026): advance
    `state` by one step of the battery actively pre-heating itself before a
    planned fast charge. Peer to `advance_charging_step`, same "no driver,
    no motor/inverter mechanical output, vehicle stationary" shape and the
    same reason for it -- but the battery is DISCHARGING to power its own
    heater, not receiving current from an external charger.

    This is Q-0026's actual mechanism, not Q-0004's rejected temp-offset
    shortcut: `heater_power_w` is drawn as real battery current via the
    same `compute_battery_electrical_step` every other discharge path uses
    (so it costs real SOC, not an invented one), and is ALSO applied as a
    direct heat_inputs_w term on the battery's own node -- distinct from,
    and on top of, the battery's own incidental R0/entropic heat from
    carrying that current. A resistive preconditioning heater is treated
    as ~100% efficient (see `RefrigerantLoop`/`CoolingLoop.chiller_
    capacity_w` for the contrasting compressor-style case, which DOES need
    a separate capacity/COP figure) -- see `ThermalArchitecture.
    preconditioning_heater_power_w`'s own docstring.

    Reuses `step_controller`/`step_cooling`/the refrigerant-loop machinery
    unmodified: a cold pack being intentionally warmed naturally reads a
    near-zero/floor cooling duty from the existing thermostat-ramp logic,
    so no special-casing is needed to avoid the cooling and heating paths
    fighting each other.
    """
    battery_spec = profile.battery
    aged_battery_spec = apply_soh_to_battery_spec(battery_spec, state.battery_soh)
    motor_spec = profile.motors[0]
    inverter_spec = profile.inverter
    cooling_loops = profile.thermal_architecture.cooling_loops
    refrigerant_loops = profile.thermal_architecture.refrigerant_loops

    motor_name = motor_spec.thermal_node.name
    inverter_name = inverter_spec.thermal_node.name
    battery_name = battery_spec.thermal_node.name

    ambient_temp_k = scenario.climate.mean_annual_temp_k.value

    if network is None:
        coolant_nodes = _coolant_thermal_nodes(cooling_loops, state.node_temps_k)
        network = _combined_thermal_network(profile, coolant_nodes)
    coolant_temps_k = {loop.name: state.node_temps_k[coolant_node_name(loop.name)] for loop in cooling_loops}

    battery_rc_tau_s = aged_battery_spec.r1_ohm.value * aged_battery_spec.c1_farad.value
    dt_s = _stabilize_dt_s(network, battery_rc_tau_s, scenario.dt_policy.min_dt_s)
    if max_dt_s is not None:
        dt_s = min(dt_s, max_dt_s)

    causal_events: list[CausalEvent] = []

    # cooling duty only, same reasoning as advance_charging_step's own
    # identical call.
    controller_result = step_controller(
        0.0,
        motor_spec,
        inverter_spec,
        aged_battery_spec,
        profile.vehicle_dynamics,
        cooling_loops,
        state.node_temps_k[motor_name],
        state.node_temps_k[inverter_name],
        state.battery_soc,
        state.node_temps_k[battery_name],
        state.battery_v_rc_v,
        state.battery_voltage_v,
        0.0,
        coolant_temps_k,
        state.sim_time_s,
        refrigerant_loops=refrigerant_loops,
        pump_aggressiveness=pump_aggressiveness,
    )

    cooling_results = {
        loop.name: step_cooling(
            loop,
            state.node_temps_k[coolant_node_name(loop.name)],
            ambient_temp_k,
            state.speed_mps,
            controller_result.commanded_pump_duty[loop.name],
            controller_result.commanded_fan_duty[loop.name],
            controller_result.commanded_chiller_duty[loop.name],
        )
        for loop in cooling_loops
    }
    total_heat_rejected_w = sum(result.heat_rejected_w for result in cooling_results.values())
    total_chiller_heat_removed_w = sum(result.chiller_heat_removed_w for result in cooling_results.values())

    component_temps_k_for_refrigerant = {
        motor_name: state.node_temps_k[motor_name],
        inverter_name: state.node_temps_k[inverter_name],
        battery_name: state.node_temps_k[battery_name],
    }
    component_resistances_to_coolant_k_per_w = {
        motor_name: motor_spec.thermal_node.resistance_to_coolant_k_per_w.value,
        inverter_name: inverter_spec.thermal_node.resistance_to_coolant_k_per_w.value,
        battery_name: battery_spec.thermal_node.resistance_to_coolant_k_per_w.value,
    }
    refrigerant_heat_removed_w_by_component, refrigerant_parasitic_w = _step_refrigerant_loops(
        refrigerant_loops,
        controller_result.commanded_refrigerant_duty,
        component_temps_k_for_refrigerant,
        component_resistances_to_coolant_k_per_w,
    )
    total_refrigerant_heat_removed_w = sum(refrigerant_heat_removed_w_by_component.values())

    cooling_parasitic_w = (
        sum(result.pump_power_w + result.fan_power_w + result.chiller_power_w for result in cooling_results.values())
        + refrigerant_parasitic_w
    )

    # The heater draws FROM the battery (discharge, positive current) --
    # the opposite sign from advance_charging_step's external-charger
    # current, which flows INTO the battery. Cooling/refrigerant parasitic
    # draw is charged the same way every other discharge path charges it.
    heater_current_request_a = (heater_power_w + cooling_parasitic_w) / state.battery_voltage_v
    battery_electrical = compute_battery_electrical_step(
        aged_battery_spec,
        heater_current_request_a,
        state.battery_soc,
        state.node_temps_k[battery_name],
        state.battery_v_rc_v,
        dt_s,
        state.sim_time_s,
    )
    causal_events.extend(battery_electrical.causal_events)

    aging_result = compute_aging_step(
        battery_spec,
        state.battery_soh,
        state.node_temps_k[battery_name],
        state.battery_soc,
        battery_electrical.applied_current_a,
        dt_s,
    )

    heat_inputs_w = {
        motor_name: 0.0,
        inverter_name: 0.0,
        # heater_power_w is a DIRECT heat term, distinct from (and on top
        # of) the battery's own incidental irreversible/entropic heat from
        # carrying the discharge current -- see this function's own
        # docstring for why these aren't double-counting the same effect.
        battery_name: battery_electrical.irreversible_heat_w + battery_electrical.entropic_heat_w + heater_power_w,
        **{
            coolant_node_name(loop.name): -(
                cooling_results[loop.name].heat_rejected_w + cooling_results[loop.name].chiller_heat_removed_w
            )
            for loop in cooling_loops
        },
    }
    for name, refrigerant_heat_removed_w in refrigerant_heat_removed_w_by_component.items():
        heat_inputs_w[name] = heat_inputs_w.get(name, 0.0) - refrigerant_heat_removed_w
    new_temps_k = step_thermal_network(
        network,
        state.node_temps_k,
        heat_inputs_w,
        ambient_temp_k=ambient_temp_k,
        coolant_temp_k=0.0,
        coolant_flow_l_per_min=0.0,
        dt_s=dt_s,
    )

    new_state = SimState(
        state_id=f"{state.scenario_id}-step-{state.step_count + 1:06d}",
        scenario_id=state.scenario_id,
        sim_time_s=state.sim_time_s + dt_s,
        odometer_km=state.odometer_km,
        battery_soc=battery_electrical.soc,
        battery_soh=aging_result.new_soh,
        node_temps_k=new_temps_k,
        rng_state=state.rng_state,
        fork_lineage=state.fork_lineage,
        step_count=state.step_count + 1,
        dt_current_s=dt_s,
        speed_mps=0.0,
        battery_v_rc_v=battery_electrical.v_rc_v,
        driver_integral_error_state=state.driver_integral_error_state,
        battery_throughput_ah=state.battery_throughput_ah + abs(battery_electrical.applied_current_a) * dt_s / 3600.0,
        battery_voltage_v=battery_electrical.terminal_voltage_v,
        parked_duration_s=0.0,
    )

    return SimStepRecord(
        new_state=new_state,
        causal_events=causal_events,
        requested_torque_nm=0.0,
        commanded_torque_nm=0.0,
        motor_mechanical_power_w=0.0,
        motor_electrical_power_w=0.0,
        battery_electrical_power_w=battery_electrical.terminal_voltage_v * battery_electrical.applied_current_a,
        battery_ocv_power_w=battery_electrical.ocv_v * battery_electrical.applied_current_a,
        pump_duty=controller_result.commanded_pump_duty,
        fan_duty=controller_result.commanded_fan_duty,
        chiller_duty=controller_result.commanded_chiller_duty,
        refrigerant_duty=controller_result.commanded_refrigerant_duty,
        heat_rejected_w=total_heat_rejected_w,
        chiller_heat_removed_w=total_chiller_heat_removed_w,
        refrigerant_heat_removed_w=total_refrigerant_heat_removed_w,
        motor_loss_w=0.0,
        inverter_loss_w=0.0,
        battery_loss_w=battery_electrical.irreversible_heat_w + battery_electrical.entropic_heat_w,
        aux_power_w=0.0,
        cooling_parasitic_w=cooling_parasitic_w,
        active_limit_event_types=[event.event_type for event in controller_result.causal_events],
        applied_current_a=battery_electrical.applied_current_a,
        charging=False,
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
    record_interval_s: float = 0.0,
) -> RunResult:
    """Run `profile` under `scenario` for `duration_s` seconds.

    `target_speed_mps` is a function of elapsed sim time (seconds) to target
    speed (m/s) — a temporary interface until drive-cycle generation exists
    (see docs/MILESTONES.md); pass e.g. `lambda t: 20.0` for a constant-speed cruise.

    `initial_state`, if given, resumes from that state instead of building a
    fresh t=0 one (`initialize_sim_state`) — this is how fork replay works:
    call `run_scenario` again with a previous run's `final_state`.

    `grade_rad` and `pump_aggressiveness` are passed straight through to
    `advance_one_step` every step (see its docstring); both default to the
    original unparameterized behavior (flat road, unscaled duty ramp).

    `record_interval_s`, if greater than 0, decimates `RunResult.time_series`
    to roughly one sample per that many seconds of simulated time (always
    including the first and last sample), instead of recording every step —
    for long-horizon runs where per-step time-series retention would be
    prohibitively large. 0.0 (default) records every step, matching the
    original behavior. `RunResult.final_state` and the energy integrals are
    unaffected either way — only the time-series arrays are decimated.
    """
    if initial_state is not None:
        state = initial_state
    else:
        state = initialize_sim_state(profile, scenario, scenario.random_seed)

    coolant_nodes = _coolant_thermal_nodes(profile.thermal_architecture.cooling_loops, state.node_temps_k)
    network = _combined_thermal_network(profile, coolant_nodes)

    zero_duty_by_loop = {loop.name: 0.0 for loop in profile.thermal_architecture.cooling_loops}
    zero_duty_by_refrigerant_loop = {loop.name: 0.0 for loop in profile.thermal_architecture.refrigerant_loops}
    time_series = SimTimeSeries(
        time_s=[state.sim_time_s],
        speed_mps=[state.speed_mps],
        requested_torque_nm=[0.0],
        commanded_torque_nm=[0.0],
        motor_electrical_power_w=[0.0],
        soc=[state.battery_soc],
        battery_current_a=[0.0],
        charging=[False],
        node_temps_k={name: [temp] for name, temp in state.node_temps_k.items()},
        pump_duty=[dict(zero_duty_by_loop)],
        fan_duty=[dict(zero_duty_by_loop)],
        chiller_duty=[dict(zero_duty_by_loop)],
        refrigerant_duty=[dict(zero_duty_by_refrigerant_loop)],
        heat_rejected_w=[0.0],
        chiller_heat_removed_w=[0.0],
        refrigerant_heat_removed_w=[0.0],
        active_limit_event_types=[[]],
    )
    all_causal_events: list[CausalEvent] = []
    consumed_j = 0.0
    regen_recovered_j = 0.0
    motor_loss_j = 0.0
    inverter_loss_j = 0.0
    battery_loss_j = 0.0
    radiator_rejected_j = 0.0
    chiller_rejected_j = 0.0
    refrigerant_rejected_j = 0.0
    aux_j = 0.0
    cooling_parasitic_j = 0.0

    last_recorded_time_s = state.sim_time_s

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
            network=network,
        )
        dt_s = record.new_state.sim_time_s - state.sim_time_s
        state = record.new_state
        all_causal_events.extend(record.causal_events)

        if record.battery_electrical_power_w >= 0.0:
            consumed_j += record.battery_electrical_power_w * dt_s
        else:
            regen_recovered_j += -record.battery_electrical_power_w * dt_s

        should_record = (
            record_interval_s <= 0.0
            or (state.sim_time_s - last_recorded_time_s) >= record_interval_s
            or state.sim_time_s >= end_time_s
        )
        if should_record:
            last_recorded_time_s = state.sim_time_s
            time_series.time_s.append(state.sim_time_s)
            time_series.speed_mps.append(state.speed_mps)
            time_series.requested_torque_nm.append(record.requested_torque_nm)
            time_series.commanded_torque_nm.append(record.commanded_torque_nm)
            time_series.motor_electrical_power_w.append(record.motor_electrical_power_w)
            time_series.soc.append(state.battery_soc)
            time_series.battery_current_a.append(record.applied_current_a)
            time_series.charging.append(record.charging)
            for name, temp in state.node_temps_k.items():
                time_series.node_temps_k.setdefault(name, []).append(temp)
            time_series.pump_duty.append(record.pump_duty)
            time_series.fan_duty.append(record.fan_duty)
            time_series.chiller_duty.append(record.chiller_duty)
            time_series.refrigerant_duty.append(record.refrigerant_duty)
            time_series.heat_rejected_w.append(record.heat_rejected_w)
            time_series.chiller_heat_removed_w.append(record.chiller_heat_removed_w)
            time_series.refrigerant_heat_removed_w.append(record.refrigerant_heat_removed_w)
            time_series.active_limit_event_types.append(record.active_limit_event_types)

        motor_loss_j += record.motor_loss_w * dt_s
        inverter_loss_j += record.inverter_loss_w * dt_s
        battery_loss_j += record.battery_loss_w * dt_s
        radiator_rejected_j += record.heat_rejected_w * dt_s
        chiller_rejected_j += record.chiller_heat_removed_w * dt_s
        refrigerant_rejected_j += record.refrigerant_heat_removed_w * dt_s
        aux_j += record.aux_power_w * dt_s
        cooling_parasitic_j += record.cooling_parasitic_w * dt_s

    energy_totals = EnergyTotals(
        consumed_j=consumed_j,
        regen_recovered_j=regen_recovered_j,
        motor_loss_j=motor_loss_j,
        inverter_loss_j=inverter_loss_j,
        battery_loss_j=battery_loss_j,
        radiator_rejected_j=radiator_rejected_j,
        chiller_rejected_j=chiller_rejected_j,
        refrigerant_rejected_j=refrigerant_rejected_j,
        aux_j=aux_j,
        cooling_parasitic_j=cooling_parasitic_j,
    )

    return RunResult(
        final_state=state,
        time_series=time_series,
        causal_events=all_causal_events,
        energy_totals=energy_totals,
    )


def run_charging_session(
    profile: ManufacturerProfile,
    scenario: Scenario,
    duration_s: float,
    charge_power_request_w: float,
    initial_state: SimState | None = None,
    pump_aggressiveness: float = DEFAULT_PUMP_AGGRESSIVENESS,
    record_interval_s: float = 0.0,
) -> RunResult:
    """Milestone E2e: run a DC charging session for `duration_s` seconds at a
    constant `charge_power_request_w` power request -- peer to `run_scenario`,
    looping `advance_charging_step` instead of `advance_one_step`. The
    actually-applied current tapers naturally as `BatterySpec.charge_current_
    limit_curve` and the voltage-limit ceiling bind (see `battery.available_
    charge_current_a`); no separate taper mechanism exists or is needed.

    Fixed `duration_s`, not an open-ended "until target SOC reached"
    condition -- matches every other archetype's fixed-duration convention.
    If the placeholder taper curve means a session doesn't reach a realistic
    end SOC in that fixed duration, that's diagnostic information for
    whoever characterizes it, not something this function papers over.

    `RunResult.energy_totals.regen_recovered_j` accumulates a charging
    session's energy too, not just literal regen braking -- consistent with
    that field's own documented meaning ("total electrical energy returned
    to the battery (charge)"), which was already generic despite its name.
    No new `EnergyTotals` field was added for this; the existing one's
    docstring already covers it.
    """
    if initial_state is not None:
        state = initial_state
    else:
        state = initialize_sim_state(profile, scenario, scenario.random_seed)

    coolant_nodes = _coolant_thermal_nodes(profile.thermal_architecture.cooling_loops, state.node_temps_k)
    network = _combined_thermal_network(profile, coolant_nodes)

    zero_duty_by_loop = {loop.name: 0.0 for loop in profile.thermal_architecture.cooling_loops}
    zero_duty_by_refrigerant_loop = {loop.name: 0.0 for loop in profile.thermal_architecture.refrigerant_loops}
    time_series = SimTimeSeries(
        time_s=[state.sim_time_s],
        speed_mps=[state.speed_mps],
        requested_torque_nm=[0.0],
        commanded_torque_nm=[0.0],
        motor_electrical_power_w=[0.0],
        soc=[state.battery_soc],
        battery_current_a=[0.0],
        charging=[True],
        node_temps_k={name: [temp] for name, temp in state.node_temps_k.items()},
        pump_duty=[dict(zero_duty_by_loop)],
        fan_duty=[dict(zero_duty_by_loop)],
        chiller_duty=[dict(zero_duty_by_loop)],
        refrigerant_duty=[dict(zero_duty_by_refrigerant_loop)],
        heat_rejected_w=[0.0],
        chiller_heat_removed_w=[0.0],
        refrigerant_heat_removed_w=[0.0],
        active_limit_event_types=[[]],
    )
    all_causal_events: list[CausalEvent] = []
    consumed_j = 0.0
    regen_recovered_j = 0.0
    motor_loss_j = 0.0
    inverter_loss_j = 0.0
    battery_loss_j = 0.0
    radiator_rejected_j = 0.0
    chiller_rejected_j = 0.0
    refrigerant_rejected_j = 0.0
    aux_j = 0.0
    cooling_parasitic_j = 0.0

    last_recorded_time_s = state.sim_time_s

    end_time_s = state.sim_time_s + duration_s
    while state.sim_time_s < end_time_s:
        remaining_s = end_time_s - state.sim_time_s
        record = advance_charging_step(
            profile,
            scenario,
            state,
            charge_power_request_w,
            max_dt_s=remaining_s,
            pump_aggressiveness=pump_aggressiveness,
            network=network,
        )
        dt_s = record.new_state.sim_time_s - state.sim_time_s
        state = record.new_state
        all_causal_events.extend(record.causal_events)

        if record.battery_electrical_power_w >= 0.0:
            consumed_j += record.battery_electrical_power_w * dt_s
        else:
            regen_recovered_j += -record.battery_electrical_power_w * dt_s

        should_record = (
            record_interval_s <= 0.0
            or (state.sim_time_s - last_recorded_time_s) >= record_interval_s
            or state.sim_time_s >= end_time_s
        )
        if should_record:
            last_recorded_time_s = state.sim_time_s
            time_series.time_s.append(state.sim_time_s)
            time_series.speed_mps.append(state.speed_mps)
            time_series.requested_torque_nm.append(record.requested_torque_nm)
            time_series.commanded_torque_nm.append(record.commanded_torque_nm)
            time_series.motor_electrical_power_w.append(record.motor_electrical_power_w)
            time_series.soc.append(state.battery_soc)
            time_series.battery_current_a.append(record.applied_current_a)
            time_series.charging.append(record.charging)
            for name, temp in state.node_temps_k.items():
                time_series.node_temps_k.setdefault(name, []).append(temp)
            time_series.pump_duty.append(record.pump_duty)
            time_series.fan_duty.append(record.fan_duty)
            time_series.chiller_duty.append(record.chiller_duty)
            time_series.refrigerant_duty.append(record.refrigerant_duty)
            time_series.heat_rejected_w.append(record.heat_rejected_w)
            time_series.chiller_heat_removed_w.append(record.chiller_heat_removed_w)
            time_series.refrigerant_heat_removed_w.append(record.refrigerant_heat_removed_w)
            time_series.active_limit_event_types.append(record.active_limit_event_types)

        motor_loss_j += record.motor_loss_w * dt_s
        inverter_loss_j += record.inverter_loss_w * dt_s
        battery_loss_j += record.battery_loss_w * dt_s
        radiator_rejected_j += record.heat_rejected_w * dt_s
        chiller_rejected_j += record.chiller_heat_removed_w * dt_s
        refrigerant_rejected_j += record.refrigerant_heat_removed_w * dt_s
        aux_j += record.aux_power_w * dt_s
        cooling_parasitic_j += record.cooling_parasitic_w * dt_s

    energy_totals = EnergyTotals(
        consumed_j=consumed_j,
        regen_recovered_j=regen_recovered_j,
        motor_loss_j=motor_loss_j,
        inverter_loss_j=inverter_loss_j,
        battery_loss_j=battery_loss_j,
        radiator_rejected_j=radiator_rejected_j,
        chiller_rejected_j=chiller_rejected_j,
        refrigerant_rejected_j=refrigerant_rejected_j,
        aux_j=aux_j,
        cooling_parasitic_j=cooling_parasitic_j,
    )

    return RunResult(
        final_state=state,
        time_series=time_series,
        causal_events=all_causal_events,
        energy_totals=energy_totals,
    )
