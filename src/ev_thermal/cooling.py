"""Coolant loop dynamics: simulates flow, pump/fan control response, and heat
rejection through a `CoolingLoop`'s radiator into ambient, using the
effectiveness-NTU (epsilon-NTU) method.

`coolant_inlet_temp_k` is the coolant temperature already arriving at the
radiator (i.e., already reflecting whatever heat components upstream added to
it, via thermal.py's per-component calls) — this module doesn't itself
consume a separate heat-load number; that's only used by tests to pick a
steady-state operating point and check `heat_rejected_w` against it.

Chiller behavior (`CoolingLoop.chiller_present`/`chiller_capacity_w`) is out
of scope for this milestone — only the radiator/thermostat/pump/fan path is
modeled here.

Air-side and radiator-frontal-area coefficients on `CoolingLoop` marked
TODO(spec-sourcing) are physically-plausible placeholders, not measured
hardware curves, per CLAUDE.md's "don't invent parameters" rule waived for
this milestone. Air density and specific heat are treated as fixed physical
constants (not temperature/altitude corrected), matching how `vehicle.py`
treats gravity.
"""

from __future__ import annotations

import math

from ev_thermal.schema import CoolingLoop, CoolingStepResult

AIR_DENSITY_KG_PER_M3 = 1.225
AIR_SPECIFIC_HEAT_J_PER_KG_K = 1005.0


def _thermostat_open_fraction(temp_k: float, closed_temp_k: float, full_open_temp_k: float) -> float:
    """0.0 at/below closed_temp_k, ramping linearly to 1.0 at/above full_open_temp_k."""
    if temp_k <= closed_temp_k:
        return 0.0
    if temp_k >= full_open_temp_k:
        return 1.0
    return (temp_k - closed_temp_k) / (full_open_temp_k - closed_temp_k)


def _effectiveness_counterflow(ntu: float, c_r: float) -> float:
    """Standard counter-flow heat exchanger effectiveness-NTU relation."""
    if c_r >= 1.0 - 1e-9:
        return ntu / (1.0 + ntu)
    exponent = -ntu * (1.0 - c_r)
    numerator = 1.0 - math.exp(exponent)
    denominator = 1.0 - c_r * math.exp(exponent)
    return numerator / denominator


def step_cooling(
    spec: CoolingLoop,
    coolant_inlet_temp_k: float,
    ambient_temp_k: float,
    vehicle_speed_mps: float,
    pump_duty: float,
    fan_duty: float,
) -> CoolingStepResult:
    """Compute one instantaneous cooling-loop state; no persistent state of its own.

    Air velocity through the radiator core is `max(ram-air, fan-induced)`, not
    a sum: both act on the same air passing through the same core, so a
    stationary vehicle's fan and a moving vehicle's ram air don't add, and
    whichever effect moves more air dominates. This is what makes fan duty
    matter a lot when stationary and comparatively little at highway speed.
    """
    max_coolant_mass_flow_kg_per_s = (
        spec.pump_max_flow_l_per_min.value * spec.coolant_density_kg_per_m3.value / 60_000.0
    )
    coolant_mass_flow_kg_per_s = pump_duty * max_coolant_mass_flow_kg_per_s
    pump_power_w = spec.pump_power_w.value * pump_duty**2
    fan_power_w = spec.fan_power_w.value * fan_duty**2

    if coolant_mass_flow_kg_per_s <= 0.0:
        return CoolingStepResult(
            coolant_outlet_temp_k=coolant_inlet_temp_k,
            heat_rejected_w=0.0,
            coolant_mass_flow_kg_per_s=0.0,
            pump_power_w=pump_power_w,
            fan_power_w=fan_power_w,
            thermostat_open_fraction=0.0,
        )

    open_fraction = _thermostat_open_fraction(
        coolant_inlet_temp_k, spec.thermostat_temp_k.value, spec.thermostat_full_open_temp_k.value
    )
    radiator_mass_flow_kg_per_s = coolant_mass_flow_kg_per_s * open_fraction

    ram_air_velocity_mps = vehicle_speed_mps
    fan_air_velocity_mps = fan_duty * spec.fan_max_air_velocity_mps.value
    effective_air_velocity_mps = max(ram_air_velocity_mps, fan_air_velocity_mps)
    air_mass_flow_kg_per_s = AIR_DENSITY_KG_PER_M3 * spec.radiator_frontal_area_m2.value * effective_air_velocity_mps

    if radiator_mass_flow_kg_per_s <= 0.0 or air_mass_flow_kg_per_s <= 0.0:
        heat_rejected_w = 0.0
        radiator_outlet_temp_k = coolant_inlet_temp_k
    else:
        c_coolant_w_per_k = radiator_mass_flow_kg_per_s * spec.coolant_specific_heat_j_per_kg_k.value
        c_air_w_per_k = air_mass_flow_kg_per_s * AIR_SPECIFIC_HEAT_J_PER_KG_K
        c_min_w_per_k = min(c_coolant_w_per_k, c_air_w_per_k)
        c_max_w_per_k = max(c_coolant_w_per_k, c_air_w_per_k)
        capacity_ratio = c_min_w_per_k / c_max_w_per_k
        ntu = spec.radiator_ua_w_per_k.value / c_min_w_per_k
        effectiveness = _effectiveness_counterflow(ntu, capacity_ratio)
        max_heat_rejectable_w = c_min_w_per_k * (coolant_inlet_temp_k - ambient_temp_k)
        heat_rejected_w = effectiveness * max_heat_rejectable_w
        radiator_outlet_temp_k = coolant_inlet_temp_k - heat_rejected_w / c_coolant_w_per_k

    coolant_outlet_temp_k = (
        open_fraction * radiator_outlet_temp_k + (1.0 - open_fraction) * coolant_inlet_temp_k
    )

    return CoolingStepResult(
        coolant_outlet_temp_k=coolant_outlet_temp_k,
        heat_rejected_w=heat_rejected_w,
        coolant_mass_flow_kg_per_s=coolant_mass_flow_kg_per_s,
        pump_power_w=pump_power_w,
        fan_power_w=fan_power_w,
        thermostat_open_fraction=open_fraction,
    )
