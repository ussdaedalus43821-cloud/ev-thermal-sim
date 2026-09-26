"""Arbitration layer: this is where derating decisions live now (see
docs/MILESTONES.md's note this migration is completing). `motor.py`,
`inverter.py`, and `battery.py` no longer have the final word on how much
torque/current is granted — they only expose an "available" ceiling callable
(`motor.available_torque_nm`, `inverter.available_current_a`,
`battery.available_discharge_current_a`/`available_charge_current_a`).
`step_controller` calls those, converts everything to a common torque
currency, takes the most restrictive one, and clamps the driver's request to
it. The component modules' own internal clamping (inside `step_motor` etc.)
still runs too and is now redundant with this — that's fine, defense in
depth, but it means their own derate events are now upstream noise; the
authoritative derate events are the ones this module logs.

Cross-domain conversion is a placeholder simplification: `power_w = torque_nm
* speed_rad_per_s`, `current_a = power_w / battery_voltage_v`, ignoring
motor/inverter conversion losses entirely. A fully loss-aware conversion
would need the actual operating-point efficiency, which is circular at this
supervisory layer (it depends on the very current/torque being solved for).
TODO(spec-sourcing): revisit once a real efficiency-map-aware conversion is
available. Below `MIN_SPEED_FOR_TORQUE_CONVERSION_RAD_PER_S`, current-based
ceilings aren't converted (division blows up as speed->0) and are treated as
non-binding; real stall-torque limiting needs a separate current-limit table,
not modeled here yet.

Cooling policy (`_duty_ramp`) is an explicit placeholder, TODO(tuning): pump
duty scales with the hottest component *that loop actually serves*
(`CoolingLoop.serves`) above that loop's own thermostat threshold; fan duty
scales with that same loop's own coolant temperature above the same
threshold. Genuinely per-loop as of the per-loop-duty milestone: `commanded_
pump_duty`/`commanded_fan_duty` are `dict[str, float]` keyed by `CoolingLoop.
name`, one entry per loop in `cooling_loops`, not one shared value broadcast
to every loop (that was the prior milestone's deliberate, documented
compromise — see docs/MILESTONES.md). Neither goes below its own minimum duty — real
pumps/fans idle rather than fully stopping. `pump_aggressiveness` (default
1.0, matching the original unparameterized behavior) scales `DUTY_RAMP_SPAN_K`
down for every loop's ramp uniformly (no per-loop aggressiveness knob):
a higher value reaches full duty over a narrower temperature window above
thermostat (a more eager response), a lower value spreads the same ramp
over a wider window. It's a multiplier on an already-placeholder shape, not
a real calibrated control law.

`commanded_chiller_duty` (Q-0021) is a genuine SECOND STAGE, not a scaled
copy of fan duty: it ramps against the loop's own coolant temperature too,
but the ramp's threshold is `thermostat_full_open_temp_k` (where the
fan/radiator path is already fully engaged), not `thermostat_temp_k` —
real BMS logic avoids running a chiller when the radiator alone would do,
since it has a real running cost. `min_duty=0.0` (unlike pump/fan's nonzero
floors): a real refrigerant compressor genuinely idles at zero, it doesn't
need to keep spinning below its engagement point. Always `0.0` for a loop
with `chiller_present=False`.

`coolant_temps_k` and the `serves`-matching below are keyed by `CoolingLoop.
name`/`ThermalNode.name`, never by sim.py's internal `coolant_{loop_name}`
node-naming convention — this module's interface is loop-level, staying
decoupled from how sim.py happens to name coolant nodes in its combined
network. A `CoolingLoop` with an empty `serves` list is a data error, not
a valid configuration reaching this function: `profile_loader.load_profile`
rejects it at load time, so `max(served_temps_k)` below is trusted to
never receive an empty sequence rather than silently falling back to a
plausible-looking default.

SOC-floor limiting is NOT a separate term in the min() below: it's already
folded into `battery.available_discharge_current_a`'s own ceiling (see that
function's docstring), so taking min() with the battery's combined ceiling
already reflects it. When the battery ceiling binds because of SOC floor
specifically, this module's BATTERY_LIMIT_TRIGGERED event description says
so, using only the public `soc_floor_full_available_soc` field, not any of
battery.py's private scale-curve helpers.
"""

from __future__ import annotations

import math

from ev_thermal.battery import available_charge_current_a, available_discharge_current_a
from ev_thermal.causal import make_causal_event
from ev_thermal.inverter import available_current_a
from ev_thermal.motor import available_torque_nm
from ev_thermal.schema import (
    BatterySpec,
    CausalEvent,
    ControllerStepResult,
    CoolingLoop,
    InverterSpec,
    MotorSpec,
    VehicleDynamics,
)
from ev_thermal.vehicle import STANDARD_GRAVITY_M_PER_S2, motor_torque_request_nm

MIN_PUMP_DUTY = 0.2
MIN_FAN_DUTY = 0.1
DUTY_RAMP_SPAN_K = 15.0
"""TODO(tuning): placeholder cooling-command policy shape, not derived from
any real vehicle's thermal management calibration."""

MIN_SPEED_FOR_TORQUE_CONVERSION_RAD_PER_S = 1.0


def _duty_ramp(temp_k: float, threshold_temp_k: float, span_k: float, min_duty: float) -> float:
    """min_duty at/below threshold_temp_k, ramping linearly to 1.0 over span_k above it."""
    if temp_k <= threshold_temp_k:
        return min_duty
    fraction = min((temp_k - threshold_temp_k) / span_k, 1.0)
    return min_duty + (1.0 - min_duty) * fraction


def _current_magnitude_to_torque_magnitude_nm(current_a: float, voltage_v: float, speed_rad_per_s: float) -> float:
    if abs(speed_rad_per_s) < MIN_SPEED_FOR_TORQUE_CONVERSION_RAD_PER_S:
        return math.inf
    return (current_a * voltage_v) / abs(speed_rad_per_s)


def step_controller(
    driver_torque_request_nm: float,
    motor_spec: MotorSpec,
    inverter_spec: InverterSpec,
    battery_spec: BatterySpec,
    vehicle_dynamics: VehicleDynamics,
    cooling_loops: list[CoolingLoop],
    motor_winding_temp_k: float,
    inverter_junction_temp_k: float,
    battery_soc: float,
    battery_cell_temp_k: float,
    battery_v_rc_v: float,
    battery_voltage_v: float,
    speed_rad_per_s: float,
    coolant_temps_k: dict[str, float],
    sim_time_s: float,
    pump_aggressiveness: float = 1.0,
) -> ControllerStepResult:
    """Arbitrate the driver's torque request against every component's limit
    and produce commanded torque, cooling duties, and battery current demand.
    """
    causal_events: list[CausalEvent] = []
    requested_mag_nm = abs(driver_torque_request_nm)

    motor_ceiling_nm = available_torque_nm(motor_spec, motor_winding_temp_k)

    inverter_current_ceiling_a = available_current_a(inverter_spec, inverter_junction_temp_k)
    inverter_ceiling_nm = _current_magnitude_to_torque_magnitude_nm(
        inverter_current_ceiling_a, battery_voltage_v, speed_rad_per_s
    )

    if driver_torque_request_nm >= 0.0:
        battery_current_ceiling_a = available_discharge_current_a(
            battery_spec, battery_soc, battery_cell_temp_k, battery_v_rc_v
        )
    else:
        battery_current_ceiling_a = available_charge_current_a(
            battery_spec, battery_soc, battery_cell_temp_k, battery_v_rc_v
        )
    battery_ceiling_nm = _current_magnitude_to_torque_magnitude_nm(
        battery_current_ceiling_a, battery_voltage_v, speed_rad_per_s
    )

    traction_ceiling_nm = motor_torque_request_nm(
        wheel_torque_nm=(
            vehicle_dynamics.tire_road_friction_coefficient.value
            * vehicle_dynamics.curb_mass_kg.value
            * STANDARD_GRAVITY_M_PER_S2
            * vehicle_dynamics.wheel_radius_m.value
        ),
        gear_ratio=vehicle_dynamics.gear_ratio.value,
        driveline_efficiency=vehicle_dynamics.driveline_efficiency.value,
    )

    overall_ceiling_nm = min(motor_ceiling_nm, inverter_ceiling_nm, battery_ceiling_nm, traction_ceiling_nm)
    commanded_torque_nm = max(-overall_ceiling_nm, min(driver_torque_request_nm, overall_ceiling_nm))

    if requested_mag_nm > motor_ceiling_nm:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="MOTOR_LIMIT_TRIGGERED",
                description=(
                    f"Requested torque {driver_torque_request_nm:.1f}Nm exceeds motor's "
                    f"available torque {motor_ceiling_nm:.1f}Nm at winding temp {motor_winding_temp_k:.1f}K."
                ),
                affected_component="motor",
                severity="warning",
            )
        )
    if requested_mag_nm > inverter_ceiling_nm:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="INVERTER_LIMIT_TRIGGERED",
                description=(
                    f"Requested torque {driver_torque_request_nm:.1f}Nm exceeds inverter's "
                    f"available torque {inverter_ceiling_nm:.1f}Nm at junction temp {inverter_junction_temp_k:.1f}K."
                ),
                affected_component="inverter",
                severity="warning",
            )
        )
    if requested_mag_nm > battery_ceiling_nm:
        reason = (
            "SOC floor"
            if battery_soc < battery_spec.soc_floor_full_available_soc.value
            else "C-rate/voltage/thermal limit"
        )
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="BATTERY_LIMIT_TRIGGERED",
                description=(
                    f"Requested torque {driver_torque_request_nm:.1f}Nm exceeds battery's "
                    f"available torque {battery_ceiling_nm:.1f}Nm ({reason}, SOC {battery_soc:.3f}, "
                    f"cell temp {battery_cell_temp_k:.1f}K)."
                ),
                affected_component="battery",
                severity="warning",
            )
        )
    if requested_mag_nm > traction_ceiling_nm:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="TRACTION_LIMIT_TRIGGERED",
                description=(
                    f"Requested torque {driver_torque_request_nm:.1f}Nm exceeds the mechanical "
                    f"traction limit {traction_ceiling_nm:.1f}Nm (wheel-slip risk)."
                ),
                affected_component="vehicle",
                severity="warning",
            )
        )

    battery_current_demand_a = (commanded_torque_nm * speed_rad_per_s) / battery_voltage_v

    component_temps_k = {
        motor_spec.thermal_node.name: motor_winding_temp_k,
        inverter_spec.thermal_node.name: inverter_junction_temp_k,
        battery_spec.thermal_node.name: battery_cell_temp_k,
    }
    effective_span_k = DUTY_RAMP_SPAN_K / pump_aggressiveness

    commanded_pump_duty: dict[str, float] = {}
    commanded_fan_duty: dict[str, float] = {}
    commanded_chiller_duty: dict[str, float] = {}
    for loop in cooling_loops:
        thermostat_temp_k = loop.thermostat_temp_k.value
        served_temps_k = [component_temps_k[name] for name in loop.serves if name in component_temps_k]
        max_served_temp_k = max(served_temps_k)  # loop.serves is guaranteed non-empty (load-time check)
        commanded_pump_duty[loop.name] = _duty_ramp(
            max_served_temp_k, thermostat_temp_k, effective_span_k, MIN_PUMP_DUTY
        )
        commanded_fan_duty[loop.name] = _duty_ramp(
            coolant_temps_k[loop.name], thermostat_temp_k, effective_span_k, MIN_FAN_DUTY
        )
        commanded_chiller_duty[loop.name] = (
            _duty_ramp(coolant_temps_k[loop.name], loop.thermostat_full_open_temp_k.value, effective_span_k, 0.0)
            if loop.chiller_present
            else 0.0
        )

    return ControllerStepResult(
        commanded_torque_nm=commanded_torque_nm,
        commanded_pump_duty=commanded_pump_duty,
        commanded_fan_duty=commanded_fan_duty,
        commanded_chiller_duty=commanded_chiller_duty,
        battery_current_demand_a=battery_current_demand_a,
        causal_events=causal_events,
    )
