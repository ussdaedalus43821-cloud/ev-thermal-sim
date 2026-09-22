"""Battery electro-thermal model: converts pack current plus a `BatterySpec` into
terminal voltage, SOC evolution, heat generation, and charge/discharge limit
enforcement. (Capacity/resistance aging is a separate, later milestone.)

Single-RC-pair equivalent-circuit model: V = OCV(SOC, T) - I*R0(SOC, T) - V_RC,
with V_RC a persistent state variable relaxing toward I*R1 with time constant
R1*C1. OCV and R0 curves, the RC pair, and every temperature-correction
coefficient on `BatterySpec` marked TODO(spec-sourcing) are physically-plausible
placeholders, not measured chemistry curves, per CLAUDE.md's "don't invent
parameters" rule waived for this milestone.

Sign convention: positive current is discharge, negative is charge (matches
dSOC/dt = -I / (3600 * capacity_nominal_ah)).

Two independent cold-temperature effects exist and are DELIBERATELY not both
applied to both directions:
- R0's Arrhenius rise raises resistance for both charge and discharge, which
  naturally shrinks the voltage-limited current in both directions. This is
  the resistance/voltage-sag story and always applies.
- The explicit `cold_derate_threshold_temp_k` multiplier (`_cold_derate_scale`)
  is applied to CHARGE only. It models a distinct, charge-specific
  phenomenon (BMS-imposed charge-current restriction to avoid lithium
  plating at cold temperatures), not a second copy of the resistance story.
  Applying it to discharge too would double-count the same underlying cold
  effect and produce an unrealistically severe combined derate. Don't
  restore it to discharge without re-deriving the combined magnitude.

Entropic heat (`I*T*dOCV/dT`) is real and included, but is fully inert
whenever `docv_dt_v_per_k` is 0.0 (the placeholder default in every test
fixture so far). A run using a zero-dOCV/dT spec will under-predict thermal
load during fast charging, where entropic heat is largest.
`compute_battery_electrical_step` emits a one-time (per Python
warning-filter default) `InertEntropicHeatWarning` in that case so this
isn't silently wrong.

`available_discharge_current_a`/`available_charge_current_a` are the single
source of truth for the battery's current ceiling — combining C-rate,
voltage, thermal, and (discharge-only) SOC-floor limits — used both by
`step_battery` (clamping a request to it: `min(requested, ceiling)`, not
multiplying the request by a scale factor, which would incorrectly shrink
even small requests already under the ceiling) and by `controller.py`
(converted to torque, compared against other components' limits).

`soc_floor`/`soc_floor_full_available_soc` model a BMS reserve-capacity
policy (protecting cycle life), applied to discharge only — distinct from
the voltage-limit physics, not a re-derivation of it, the same reasoning as
the charge-only cold derate above.
"""

from __future__ import annotations

import math
import warnings

from ev_thermal.causal import make_causal_event
from ev_thermal.schema import (
    BatteryElectricalResult,
    BatterySpec,
    BatteryStepResult,
    CausalEvent,
    Sourced,
    ThermalNetwork,
)
from ev_thermal.thermal import step_thermal_network


class BatteryStabilityError(Exception):
    """Raised when `dt_s` meets or exceeds the RC pair's time constant R1*C1,
    which would make explicit Euler integration of V_RC diverge.
    """


class InertEntropicHeatWarning(UserWarning):
    """Warns that `docv_dt_v_per_k` is 0.0, so entropic heat is always zero.

    Relies on Python's default warning filter (shows each distinct call site
    once per process) rather than tracked state, matching this module's
    stateless-function design.
    """


def _interpolate(curve: list[tuple[Sourced[float], Sourced[float]]], x: float) -> float:
    """Linearly interpolate a (x, y) breakpoint curve, clamping outside its range.

    Assumes `curve` is sorted ascending by x, like `ControlStrategy.derate_curve`
    elsewhere in schema.py.
    """
    points = [(p[0].value, p[1].value) for p in curve]
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]  # unreachable given the bounds checks above


def _hot_derate_scale(temp_k: float, derate_temp_k: float, max_temp_k: float) -> float:
    """1.0 at/below derate_temp_k, ramping linearly to 0.0 at max_temp_k."""
    if temp_k <= derate_temp_k:
        return 1.0
    if temp_k >= max_temp_k:
        return 0.0
    return 1.0 - (temp_k - derate_temp_k) / (max_temp_k - derate_temp_k)


def _cold_derate_scale(temp_k: float, threshold_temp_k: float, activation_temp_k: float) -> float:
    """1.0 at/above threshold_temp_k, Arrhenius-ish falloff below it.

    Anchored at threshold_temp_k itself (not reference_temp_k) so the curve is
    continuous: scale(threshold_temp_k) == 1.0 exactly.
    """
    if temp_k >= threshold_temp_k:
        return 1.0
    return math.exp(activation_temp_k * (1.0 / threshold_temp_k - 1.0 / temp_k))


def _soc_floor_scale(soc: float, floor: float, full_available_soc: float) -> float:
    """0.0 at/below floor, ramping linearly to 1.0 at/above full_available_soc.

    Discharge-only: a BMS reserve-capacity policy protecting cycle life, not a
    re-derivation of the voltage-limit physics (see module docstring).
    """
    if soc <= floor:
        return 0.0
    if soc >= full_available_soc:
        return 1.0
    return (soc - floor) / (full_available_soc - floor)


def _ocv_and_r0_ohm(spec: BatterySpec, soc: float, cell_temp_k: float) -> tuple[float, float]:
    ocv_v = _interpolate(spec.ocv_curve_v, soc) + spec.docv_dt_v_per_k.value * (
        cell_temp_k - spec.reference_temp_k.value
    )
    r0_ohm = _interpolate(spec.r0_curve_ohm, soc) * math.exp(
        spec.r0_arrhenius_activation_temp_k.value * (1.0 / cell_temp_k - 1.0 / spec.reference_temp_k.value)
    )
    return ocv_v, r0_ohm


def available_discharge_current_a(spec: BatterySpec, soc: float, cell_temp_k: float, v_rc_v: float) -> float:
    """The battery's discharge current ceiling: single source of truth for
    `step_battery` (clamping a request to it) and `controller.py` (converted
    to torque, compared against other components' limits).
    """
    ocv_v, r0_ohm = _ocv_and_r0_ohm(spec, soc, cell_temp_k)
    c_rate_limit_a = spec.max_discharge_c_rate.value * spec.capacity_nominal_ah.value
    voltage_limit_a = max((ocv_v - v_rc_v - spec.min_terminal_voltage_v.value) / r0_ohm, 0.0)
    derate_temp_k = spec.thermal_node.derate_temp_k.value
    max_temp_k = spec.thermal_node.max_temp_k.value
    hot_scale = _hot_derate_scale(cell_temp_k, derate_temp_k, max_temp_k)
    thermal_scale = 0.0 if cell_temp_k <= spec.min_operating_temp_k.value else hot_scale
    soc_scale = _soc_floor_scale(soc, spec.soc_floor.value, spec.soc_floor_full_available_soc.value)
    return min(c_rate_limit_a, voltage_limit_a) * thermal_scale * soc_scale


def available_charge_current_a(spec: BatterySpec, soc: float, cell_temp_k: float, v_rc_v: float) -> float:
    """The battery's charge current ceiling (magnitude): single source of
    truth for `step_battery` and `controller.py`, mirroring
    `available_discharge_current_a`.
    """
    ocv_v, r0_ohm = _ocv_and_r0_ohm(spec, soc, cell_temp_k)
    c_rate_limit_a = spec.max_charge_c_rate.value * spec.capacity_nominal_ah.value
    voltage_limit_a = max((spec.max_terminal_voltage_v.value - ocv_v + v_rc_v) / r0_ohm, 0.0)
    derate_temp_k = spec.thermal_node.derate_temp_k.value
    max_temp_k = spec.thermal_node.max_temp_k.value
    hot_scale = _hot_derate_scale(cell_temp_k, derate_temp_k, max_temp_k)
    cold_scale = _cold_derate_scale(
        cell_temp_k, spec.cold_derate_threshold_temp_k.value, spec.cold_derate_activation_temp_k.value
    )
    thermal_scale = 0.0 if cell_temp_k <= spec.min_operating_temp_k.value else hot_scale * cold_scale
    return min(c_rate_limit_a, voltage_limit_a) * thermal_scale


def compute_battery_electrical_step(
    spec: BatterySpec,
    current_request_a: float,
    soc: float,
    cell_temp_k: float,
    v_rc_v: float,
    dt_s: float,
    sim_time_s: float,
) -> BatteryElectricalResult:
    """Everything about one battery step except advancing the pack's own
    temperature. Split out from `step_battery` for the same reason as
    `motor.compute_motor_heat` — see that function's docstring. Unlike
    motor/inverter, most of a battery step (voltage, SOC, V_RC) doesn't touch
    `thermal.step_thermal_network` at all even in `step_battery` — only
    `cell_temp_k`'s own advancement does, so this function is nearly all of
    `step_battery`'s logic; only the final thermal call is pulled out.
    """
    causal_events: list[CausalEvent] = []

    ocv_v, r0_ohm = _ocv_and_r0_ohm(spec, soc, cell_temp_k)

    capacity_ah = spec.capacity_nominal_ah.value
    is_discharge = current_request_a >= 0.0
    requested_mag_a = abs(current_request_a)
    direction = "discharge" if is_discharge else "charge"

    if is_discharge:
        c_rate_limit_a = spec.max_discharge_c_rate.value * capacity_ah
        voltage_limit_a = max((ocv_v - v_rc_v - spec.min_terminal_voltage_v.value) / r0_ohm, 0.0)
        available_a = available_discharge_current_a(spec, soc, cell_temp_k, v_rc_v)
    else:
        c_rate_limit_a = spec.max_charge_c_rate.value * capacity_ah
        voltage_limit_a = max((spec.max_terminal_voltage_v.value - ocv_v + v_rc_v) / r0_ohm, 0.0)
        available_a = available_charge_current_a(spec, soc, cell_temp_k, v_rc_v)

    applied_mag_a = min(requested_mag_a, available_a)
    applied_current_a = applied_mag_a if is_discharge else -applied_mag_a

    derate_temp_k = spec.thermal_node.derate_temp_k.value
    max_temp_k = spec.thermal_node.max_temp_k.value
    hot_scale = _hot_derate_scale(cell_temp_k, derate_temp_k, max_temp_k)
    below_min_operating_temp = cell_temp_k <= spec.min_operating_temp_k.value

    cold_threshold_k = spec.cold_derate_threshold_temp_k.value
    cold_scale = 1.0
    if not is_discharge and not below_min_operating_temp:
        cold_scale = _cold_derate_scale(cell_temp_k, cold_threshold_k, spec.cold_derate_activation_temp_k.value)

    soc_scale = 1.0
    if is_discharge:
        soc_scale = _soc_floor_scale(soc, spec.soc_floor.value, spec.soc_floor_full_available_soc.value)

    request_was_reduced = applied_mag_a < requested_mag_a

    if requested_mag_a > c_rate_limit_a:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="C_RATE_LIMIT_TRIGGERED",
                description=(
                    f"Requested {direction} current {requested_mag_a:.1f}A exceeds "
                    f"C-rate limit {c_rate_limit_a:.1f}A."
                ),
                affected_component="battery",
                severity="warning",
            )
        )
    if requested_mag_a > voltage_limit_a:
        bound_name = "min_terminal_voltage_v" if is_discharge else "max_terminal_voltage_v"
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="VOLTAGE_LIMIT_TRIGGERED",
                description=(
                    f"Requested {direction} current {requested_mag_a:.1f}A would violate "
                    f"{bound_name}; limited to {voltage_limit_a:.1f}A."
                ),
                affected_component="battery",
                severity="warning",
            )
        )
    if request_was_reduced and hot_scale < 1.0:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="DERATE_TRIGGERED",
                description=(
                    f"Cell temp {cell_temp_k:.1f}K exceeded derate threshold {derate_temp_k:.1f}K; "
                    f"current scaled to {hot_scale:.1%}."
                ),
                affected_component="battery",
                severity="warning",
            )
        )
    if below_min_operating_temp:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="DERATE_TRIGGERED",
                description=(
                    f"Cell temp {cell_temp_k:.1f}K at/below min_operating_temp_k "
                    f"{spec.min_operating_temp_k.value:.1f}K; all current cut off."
                ),
                affected_component="battery",
                severity="critical",
            )
        )
    elif request_was_reduced and cold_scale < 1.0:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="DERATE_TRIGGERED",
                description=(
                    f"Cell temp {cell_temp_k:.1f}K below cold-derate threshold {cold_threshold_k:.1f}K; "
                    f"current scaled to {cold_scale:.1%}."
                ),
                affected_component="battery",
                severity="warning",
            )
        )
    if request_was_reduced and soc_scale < 1.0:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="SOC_FLOOR_LIMIT_TRIGGERED",
                description=(
                    f"SOC {soc:.3f} below soc_floor_full_available_soc "
                    f"{spec.soc_floor_full_available_soc.value:.3f}; discharge current scaled to {soc_scale:.1%}."
                ),
                affected_component="battery",
                severity="warning",
            )
        )

    terminal_voltage_v = ocv_v - applied_current_a * r0_ohm - v_rc_v

    r1_ohm = spec.r1_ohm.value
    c1_farad = spec.c1_farad.value
    irreversible_heat_w = applied_current_a**2 * (r0_ohm + r1_ohm)
    if spec.docv_dt_v_per_k.value == 0.0:
        warnings.warn(
            "BatterySpec.docv_dt_v_per_k is 0.0: entropic heat is inert, so thermal "
            "load will be under-predicted during fast charging until a real dOCV/dT "
            "curve is sourced.",
            InertEntropicHeatWarning,
            stacklevel=2,
        )
    entropic_heat_w = applied_current_a * cell_temp_k * spec.docv_dt_v_per_k.value

    tau_rc_s = r1_ohm * c1_farad
    if dt_s >= tau_rc_s:
        raise BatteryStabilityError(
            f"dt_s={dt_s:g} exceeds the RC pair's time constant tau_s={tau_rc_s:g}; "
            "explicit Euler would diverge. Reduce dt_s."
        )
    new_v_rc_v = v_rc_v + dt_s * (applied_current_a * r1_ohm - v_rc_v) / tau_rc_s

    new_soc_raw = soc - dt_s * applied_current_a / (3600.0 * capacity_ah)
    new_soc = min(max(new_soc_raw, 0.0), 1.0)
    if new_soc_raw < 0.0 or new_soc_raw > 1.0:
        causal_events.append(
            make_causal_event(
                sim_time_s=sim_time_s,
                event_type="SOC_LIMIT_REACHED",
                description=f"SOC would go to {new_soc_raw:.4f}; clamped to {new_soc:.4f}.",
                affected_component="battery",
                severity="info",
            )
        )

    return BatteryElectricalResult(
        terminal_voltage_v=terminal_voltage_v,
        ocv_v=ocv_v,
        applied_current_a=applied_current_a,
        soc=new_soc,
        v_rc_v=new_v_rc_v,
        irreversible_heat_w=irreversible_heat_w,
        entropic_heat_w=entropic_heat_w,
        causal_events=causal_events,
    )


def step_battery(
    spec: BatterySpec,
    current_request_a: float,
    soc: float,
    cell_temp_k: float,
    v_rc_v: float,
    coolant_temp_k: float,
    coolant_flow_l_per_min: float,
    ambient_temp_k: float,
    dt_s: float,
    sim_time_s: float,
) -> BatteryStepResult:
    """Advance the battery by one step of size `dt_s`, standalone (isolated-node
    thermal step — see `compute_battery_electrical_step`'s docstring for why
    `sim.py` doesn't use this).

    `ambient_temp_k` is accepted for interface consistency with
    `thermal.step_thermal_network` but currently has no effect, since
    `BatterySpec` doesn't yet carry an ambient conductance for its node
    (coolant is the only modeled sink).
    """
    electrical_result = compute_battery_electrical_step(
        spec, current_request_a, soc, cell_temp_k, v_rc_v, dt_s, sim_time_s
    )

    node_name = spec.thermal_node.name
    total_heat_w = electrical_result.irreversible_heat_w + electrical_result.entropic_heat_w
    network = ThermalNetwork(nodes={node_name: spec.thermal_node}, edges=[], ambient_coupling={})
    new_temps_k = step_thermal_network(
        network,
        {node_name: cell_temp_k},
        heat_inputs_w={node_name: total_heat_w},
        ambient_temp_k=ambient_temp_k,
        coolant_temp_k=coolant_temp_k,
        coolant_flow_l_per_min=coolant_flow_l_per_min,
        dt_s=dt_s,
    )

    return BatteryStepResult(
        terminal_voltage_v=electrical_result.terminal_voltage_v,
        ocv_v=electrical_result.ocv_v,
        applied_current_a=electrical_result.applied_current_a,
        soc=electrical_result.soc,
        v_rc_v=electrical_result.v_rc_v,
        cell_temp_k=new_temps_k[node_name],
        irreversible_heat_w=electrical_result.irreversible_heat_w,
        entropic_heat_w=electrical_result.entropic_heat_w,
        causal_events=electrical_result.causal_events,
    )
