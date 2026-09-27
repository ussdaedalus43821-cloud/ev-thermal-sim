"""Data models for the EV Thermal Simulator.

This module defines every data shape the simulator operates on: vehicle
specs, environment/usage inputs, simulation state, and the fork/causal/
Monte-Carlo bookkeeping types. It contains no simulation logic — see
`sim.py`, `thermal.py`, `battery.py`, etc. for physics.

Units are SI throughout (kelvin, seconds, joules, watts, kilograms, meters,
ohms); each field's unit is encoded in its name and documented in its
`Field(description=...)`. Any value that is empirically sourced rather than
a physical constant is wrapped in `Sourced[T]` — see that class for why.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Confidence(StrEnum):
    """How much to trust a `Sourced` value when sampling it in Monte Carlo runs."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HeatPumpType(StrEnum):
    """A vehicle's cabin/battery heat-generation architecture. A closed taxonomy
    (unlike e.g. `MotorSpec.motor_type`'s free text) because comparative views
    across the six reference vehicles want to filter/group on this directly.
    """

    NONE = "none"
    RESISTIVE_PTC = "resistive_ptc"
    HEAT_PUMP = "heat_pump"
    INTEGRATED_MULTI_MODE = "integrated_multi_mode"


class Sourced(BaseModel, Generic[T]):
    """Wraps any empirically-uncertain real-world value.

    Every component spec, usage assumption, or measured coefficient that
    isn't a physical constant goes through this wrapper, so the simulator
    always knows how much to trust a number and where it came from — and
    Monte Carlo runs can later use `confidence` to size the sampling
    distribution around `value`.
    """

    value: T
    confidence: Confidence
    source: str = Field(
        ..., description="Citation: datasheet, teardown, forum measurement, or estimate rationale."
    )
    note: str | None = Field(default=None, description="Optional free-text caveat about this value.")


class ThermalNode(BaseModel):
    """A single lumped thermal mass in a `ThermalNetwork` (e.g. a battery pack, a motor stator)."""

    name: str
    capacitance_j_per_k: Sourced[float] = Field(
        ..., description="Thermal mass: energy to raise this node 1 K, in J/K."
    )
    resistance_to_coolant_k_per_w: Sourced[float] = Field(
        ..., description="Thermal resistance from this node to its coolant loop, in K/W."
    )
    initial_temp_k: float = Field(..., description="Starting temperature for a sim run, in kelvin.")
    max_temp_k: Sourced[float] = Field(
        ..., description="Absolute thermal limit before damage/shutdown, in kelvin."
    )
    derate_temp_k: Sourced[float] = Field(
        ..., description="Temperature at which power derating begins, in kelvin."
    )
    resistance_to_ambient_k_per_w: Sourced[float] | None = Field(
        default=None,
        description=(
            "Thermal resistance from this node directly to ambient air, in K/W — "
            "e.g. passive convective cooling off a battery pack's case, independent "
            "of any coolant loop. None means no ambient path at all (fully "
            "adiabatic), which is only physically correct for a genuinely isolated "
            "node; every real component loses at least some heat to ambient, so "
            "None should be treated as a modeling gap to fill, not a real answer. "
            "A property of the node's own physical environment (a pack sitting in "
            "air vs. submerged in coolant), not of the network's structure — "
            "compare `ThermalNetwork.ambient_coupling`, which is deprecated in "
            "favor of this field."
        ),
    )


class ThermalNetwork(BaseModel):
    """A graph of `ThermalNode`s connected by thermal conductances.

    Kept as a generic node/edge graph rather than a fixed topology because
    the six reference vehicles wire their loops differently enough (shared
    vs. separate battery/motor/inverter cooling, presence/absence of a
    chiller) that a fixed shape would fight some of them.
    """

    nodes: dict[str, ThermalNode] = Field(..., description="Node name -> ThermalNode.")
    edges: list[tuple[str, str, Sourced[float]]] = Field(
        default_factory=list,
        description=(
            "(node_a_name, node_b_name, conductance_w_per_k) — conductance "
            "between two named nodes, in W/K."
        ),
    )
    ambient_coupling: dict[str, Sourced[float]] = Field(
        default_factory=dict,
        description=(
            "DEPRECATED: not used; node-level ThermalNode.resistance_to_ambient_k_per_w "
            "supersedes this. Kept for now to avoid breaking serialized state; "
            "scheduled for removal once fork-replay tests confirm nothing depends "
            "on it. Node name -> conductance to ambient air, in W/K."
        ),
    )


class CoolingLoop(BaseModel):
    """A single coolant loop (e.g. battery loop, powertrain loop) and the hardware driving it."""

    name: str
    coolant_type: str = Field(..., description="e.g. '50/50 water-glycol'.")
    loop_volume_l: Sourced[float] = Field(..., description="Coolant volume in this loop, in liters.")
    pump_max_flow_l_per_min: Sourced[float] = Field(
        ..., description="Maximum coolant flow rate, in L/min."
    )
    pump_power_w: Sourced[float] = Field(
        ..., description="Electrical power drawn by the pump at max flow, in watts."
    )
    radiator_ua_w_per_k: Sourced[float] = Field(
        ..., description="Radiator heat-rejection coefficient (UA), in W/K."
    )
    fan_power_w: Sourced[float] = Field(
        ..., description="Electrical power drawn by the radiator fan(s), in watts."
    )
    chiller_present: bool = Field(..., description="Whether this loop has an active refrigerant chiller.")
    chiller_capacity_w: Sourced[float] | None = Field(
        default=None, description="Chiller heat-removal capacity, in watts, if chiller_present."
    )
    chiller_power_w: Sourced[float] | None = Field(
        default=None,
        description=(
            "Electrical power the chiller itself draws at full duty, in watts, if chiller_present. "
            "Distinct from chiller_capacity_w (how much heat it can remove) -- a real refrigerant "
            "chiller's own running cost, charged to the battery the same way pump_power_w/fan_power_w "
            "are (see cooling.step_cooling)."
        ),
    )
    serves: list[str] = Field(
        ..., description="Names of components this loop cools, e.g. ['battery'] or ['motor', 'inverter']."
    )
    coolant_specific_heat_j_per_kg_k: Sourced[float] = Field(
        ..., description="Coolant specific heat, in J/(kg*K)."
    )
    coolant_density_kg_per_m3: Sourced[float] = Field(..., description="Coolant density, in kg/m^3.")
    thermostat_temp_k: Sourced[float] = Field(
        ..., description="Coolant temperature at/below which the thermostat is fully closed (radiator bypassed)."
    )
    thermostat_full_open_temp_k: Sourced[float] = Field(
        ...,
        description=(
            "Coolant temperature at/above which the thermostat is fully open. Opens "
            "linearly between thermostat_temp_k and this value."
        ),
    )
    radiator_frontal_area_m2: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder. Radiator core frontal area, in m^2, "
            "for computing ram-air mass flow from vehicle speed."
        ),
    )
    fan_max_air_velocity_mps: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder. Air velocity through the radiator "
            "core the fan alone drives at fan_duty=1 (vehicle stationary), in m/s."
        ),
    )


class RefrigerantLoop(BaseModel):
    """A direct-expansion (DX) refrigerant cooling path (e.g. the BMW i3's battery
    cooling) — structurally parallel to `CoolingLoop` but for a system with no
    glycol, pump, radiator, or fan: refrigerant evaporates directly against the
    served component(s), so there is no separate coolant thermal mass (see
    docs/MILESTONES.md's Milestone D). Capable of holding a served component
    BELOW ambient, the same capability `CoolingLoop.chiller_present` adds to a
    glycol loop, but as the sole/primary cooling mechanism rather than a second
    stage bolted onto a radiator path.
    """

    name: str
    refrigerant_type: str = Field(..., description="e.g. 'R134a'.")
    capacity_w: Sourced[float] = Field(
        ..., description="Heat-removal capacity at full duty, in watts."
    )
    power_w: Sourced[float] = Field(
        ..., description="Compressor electrical draw at full duty, in watts."
    )
    activation_temp_k: Sourced[float] = Field(
        ..., description="Duty starts ramping above this served-component temperature."
    )
    full_duty_temp_k: Sourced[float] = Field(
        ..., description="Duty reaches 1.0 at/above this served-component temperature."
    )
    serves: list[str] = Field(
        ..., description="Names of components this loop cools directly, e.g. ['battery_pack']."
    )


class VehicleDynamics(BaseModel):
    """Physical properties needed for later energy-consumption modeling in `vehicle.py`/`driver.py`."""

    curb_mass_kg: Sourced[float] = Field(..., description="Curb weight, in kilograms.")
    drag_coefficient: Sourced[float] = Field(..., description="Aerodynamic drag coefficient, dimensionless.")
    frontal_area_m2: Sourced[float] = Field(..., description="Frontal cross-sectional area, in square meters.")
    rolling_resistance_coefficient: Sourced[float] = Field(
        ..., description="Rolling resistance coefficient, dimensionless."
    )
    wheel_radius_m: Sourced[float] = Field(..., description="Wheel rolling radius, in meters.")
    gear_ratio: Sourced[float] = Field(
        ..., description="Fixed gear reduction ratio between motor shaft and wheels, dimensionless."
    )
    driveline_efficiency: Sourced[float] = Field(
        ..., description="Driveline (gearbox + differential) mechanical efficiency, as a fraction 0-1."
    )
    tire_road_friction_coefficient: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder. Coefficient of friction between "
            "tire and road, dimensionless, for the mechanical traction (wheel-slip) "
            "torque limit: max_traction_force = coefficient * mass * g."
        ),
    )


class BatterySpec(BaseModel):
    """The traction battery pack for one manufacturer profile."""

    chemistry: str = Field(..., description="e.g. 'NMC', 'LFP', 'NCA'.")
    capacity_kwh_nominal: Sourced[float] = Field(..., description="Nameplate pack capacity, in kWh.")
    capacity_kwh_usable: Sourced[float] = Field(..., description="Usable pack capacity, in kWh.")
    nominal_voltage_v: Sourced[float] = Field(..., description="Nominal pack voltage, in volts.")
    internal_resistance_ohm: Sourced[float] = Field(..., description="Pack internal resistance, in ohms.")
    mass_kg: Sourced[float] = Field(..., description="Pack mass, in kilograms.")
    specific_heat_j_per_kg_k: Sourced[float] = Field(
        ..., description="Effective specific heat of the pack, in J/(kg*K)."
    )
    max_charge_c_rate: Sourced[float] = Field(
        ..., description="Maximum charge rate, in C (capacity multiples/hour)."
    )
    max_discharge_c_rate: Sourced[float] = Field(
        ..., description="Maximum discharge rate, in C (capacity multiples/hour)."
    )
    thermal_node: ThermalNode = Field(..., description="Lumped thermal model of the pack.")
    capacity_nominal_ah: Sourced[float] = Field(
        ..., description="Pack capacity in amp-hours, for coulomb counting."
    )
    reference_temp_k: Sourced[float] = Field(
        ...,
        description=(
            "Reference temperature ocv_curve_v, r0_curve_ohm, and the cold-derate "
            "curve were all characterized at, in kelvin."
        ),
    )
    ocv_curve_v: list[tuple[Sourced[float], Sourced[float]]] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder Li-ion-shaped curve, not a measured "
            "chemistry curve. (soc_fraction, open_circuit_voltage_v) points at "
            "reference_temp_k, interpolated linearly. Must be monotonically "
            "increasing in soc_fraction."
        ),
    )
    docv_dt_v_per_k: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder entropic coefficient d(OCV)/dT, in "
            "V/K. Also used to shift ocv_curve_v away from reference_temp_k. 0.0 is "
            "a valid placeholder meaning the entropic-heat term is currently inert "
            "(battery.step_battery warns when this is the case) — until a real "
            "curve is sourced, fast-charge scenarios will under-predict thermal "
            "load, since entropic heat is largest exactly there."
        ),
    )
    r0_curve_ohm: list[tuple[Sourced[float], Sourced[float]]] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder, not a measured curve. "
            "(soc_fraction, series_resistance_ohm) points at reference_temp_k, "
            "interpolated linearly, before the Arrhenius temperature correction."
        ),
    )
    r0_arrhenius_activation_temp_k: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder pseudo-activation temperature (Ea/R) "
            "for scaling r0_curve_ohm away from reference_temp_k: "
            "R0(T) = R0(reference_temp_k) * exp(activation_temp_k * (1/T - 1/reference_temp_k)). "
            "Higher values make R0 rise more steeply as the cell gets colder."
        ),
    )
    r1_ohm: Sourced[float] = Field(
        ..., description="TODO(spec-sourcing): placeholder single-RC-pair resistance, in ohms."
    )
    c1_farad: Sourced[float] = Field(
        ..., description="TODO(spec-sourcing): placeholder single-RC-pair capacitance, in farads."
    )
    min_terminal_voltage_v: Sourced[float] = Field(
        ..., description="Minimum allowed pack terminal voltage before discharge current is clamped, in volts."
    )
    max_terminal_voltage_v: Sourced[float] = Field(
        ..., description="Maximum allowed pack terminal voltage before charge current is clamped, in volts."
    )
    min_operating_temp_k: Sourced[float] = Field(
        ..., description="Cell temperature at/below which all current is cut off, in kelvin."
    )
    cold_derate_threshold_temp_k: Sourced[float] = Field(
        ..., description="Cell temperature below which the cold-temperature power derate begins, in kelvin."
    )
    cold_derate_activation_temp_k: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder Arrhenius-ish pseudo-activation "
            "temperature governing how sharply available power falls off below "
            "cold_derate_threshold_temp_k: scale(T) = exp(activation_temp_k * "
            "(1/cold_derate_threshold_temp_k - 1/T)) for T below the threshold, "
            "1.0 at/above it."
        ),
    )
    soc_floor: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder BMS reserve-capacity policy, distinct "
            "from the voltage-limit physics: discharge is fully cut off at/below "
            "this SOC to protect cycle life, ramping up to full availability at "
            "soc_floor_full_available_soc, regardless of what the voltage limit "
            "would otherwise still allow."
        ),
    )
    soc_floor_full_available_soc: Sourced[float] = Field(
        ..., description="SOC at/above which the soc_floor discharge restriction is fully lifted."
    )
    cooling_mechanism: Sourced[str] | None = Field(
        default=None,
        description=(
            "How the pack sheds heat, e.g. 'passive air (no active cooling)', "
            "'indirect liquid (glycol loop)', 'direct expansion refrigerant "
            "(R134a through the pack, no glycol loop)'. Free text, not an enum: "
            "real packs sometimes combine mechanisms in ways a closed taxonomy "
            "would misrepresent. Descriptive only — not read by battery.py yet."
        ),
    )
    min_charge_temp_k: Sourced[float] | None = Field(
        default=None,
        description=(
            "Cell temperature at/below which CHARGE current is cut off entirely, "
            "if different from min_operating_temp_k (which remains the discharge-"
            "side, and default/shared, hard cutoff). None means charge shares "
            "min_operating_temp_k's cutoff, i.e. today's symmetric behavior is "
            "unchanged. Read by battery.available_charge_current_a and "
            "battery.compute_battery_electrical_step via _min_temp_cutoff_k — "
            "discharge always uses min_operating_temp_k, unaffected by this field."
        ),
    )
    calendar_aging_rate_per_s: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): empirical calendar-aging base rate, fraction of "
            "SOH lost per second at reference_temp_k and 50% SOC. See aging.py's "
            "module docstring, 'Coefficient provenance', for how this was fit and "
            "its validated temperature envelope."
        ),
    )
    calendar_aging_activation_temp_k: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): empirical pseudo-activation temperature (Ea/R) "
            "scaling calendar_aging_rate_per_s away from reference_temp_k: "
            "rate(T) = calendar_aging_rate_per_s * exp(activation_temp_k * "
            "(1/reference_temp_k - 1/T)). Do not treat as a literal SEI-kinetics "
            "activation energy — see aging.py's docstring."
        ),
    )
    calendar_aging_soc_sensitivity: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): dimensionless multiplier on calendar aging rate "
            "as (1 + calendar_aging_soc_sensitivity * (soc - 0.5)); higher SOC "
            "ages faster. See aging.py's docstring."
        ),
    )
    cycle_aging_rate_per_efc: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): empirical cycle-aging base rate, fraction of "
            "SOH lost per equivalent full cycle (EFC) of throughput at "
            "reference_temp_k and 1C, where one EFC = capacity_nominal_ah worth "
            "of amp-hour throughput — i.e. throughput is normalized by "
            "capacity_nominal_ah (the pack's fixed, UNAGED nominal capacity, "
            "never the current SOH-scaled capacity: normalizing by current "
            "capacity would introduce an uncalibrated positive-feedback loop, "
            "SOH -> EFC rate -> SOH, that this simulator does not model or "
            "validate anywhere else). This makes the resulting SOH trajectory a "
            "bounded approximation, most accurate at SOH >= 0.7; below that, "
            "using the fixed nominal (rather than shrinking) capacity as the "
            "EFC denominator understates each EFC's true throughput relative to "
            "the pack's actual present capacity. See aging.py's module "
            "docstring, 'Coefficient provenance' — this coefficient is a "
            "placeholder assumption, not a calibrated fit, and was renamed and "
            "re-derived from a prior cycle_aging_rate_per_ah (Ah-normalized, "
            "capacity-confounded) form; see docs/DESIGN_QUESTIONS.md Q-0006/"
            "Q-0007 for the re-derivation record."
        ),
    )
    cycle_aging_activation_temp_k: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): empirical pseudo-activation temperature (Ea/R) "
            "scaling cycle_aging_rate_per_efc away from reference_temp_k, same "
            "functional form as calendar_aging_activation_temp_k. Unconstrained by "
            "any calibration anchor — see aging.py's docstring."
        ),
    )
    cycle_aging_c_rate_sensitivity: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): exponent n in rate * (applied C-rate)^n scaling "
            "cycle aging by how hard the pack is being cycled. See aging.py's "
            "docstring — a placeholder assumption, not a calibrated fit."
        ),
    )
    cold_charge_plating_onset_temp_k: Sourced[float] = Field(
        ...,
        description=(
            "Milestone: cold-charging lithium plating (Q-0028). Cell temperature "
            "at/above which charging causes no extra (lithium-plating) aging beyond "
            "cycle_aging_rate_per_efc's own SEI-growth term, at the reference C-rate "
            "of 1.0 -- below it, cold_charge_plating_rate_per_efc_per_k's term "
            "activates. See aging.py's module docstring, 'Cold-charging lithium "
            "plating', for the real literature this was sourced from and why it's "
            "held fixed at a single reference C-rate rather than the full C-rate-"
            "coupled onset surface the source literature shows."
        ),
    )
    cold_charge_plating_rate_per_efc_per_k: Sourced[float] = Field(
        ...,
        description=(
            "Milestone: cold-charging lithium plating (Q-0028). Fraction of SOH "
            "lost per equivalent full cycle (EFC) of CHARGING throughput per "
            "Kelvin the cell is below cold_charge_plating_onset_temp_k, at the "
            "reference C-rate of 1.0. Zero contribution during discharge and "
            "at/above the onset temperature -- see aging.py's docstring for the "
            "full formula and calibration provenance."
        ),
    )
    cold_charge_plating_temp_sensitivity: Sourced[float] = Field(
        ...,
        description=(
            "Milestone: cold-charging lithium plating (Q-0028). Exponent n in "
            "rate * (onset_temp_k - cell_temp_k)^n -- a placeholder assumption "
            "(linear), not a calibrated fit; see aging.py's docstring."
        ),
    )
    cold_charge_plating_c_rate_sensitivity: Sourced[float] = Field(
        ...,
        description=(
            "Milestone: cold-charging lithium plating (Q-0028). Exponent n in "
            "rate * (applied C-rate)^n for the cold-charging plating term -- a "
            "placeholder assumption, not a calibrated fit; independent of (not "
            "reused from) cycle_aging_c_rate_sensitivity, since the two "
            "mechanisms' real C-rate sensitivities are not the same. See "
            "aging.py's docstring."
        ),
    )
    resistance_growth_per_soh_loss: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): fractional growth in internal resistance per "
            "unit of SOH lost: R(soh) = R(soh=1) * (1 + resistance_growth_per_soh_loss "
            "* (1 - soh)). Placeholder, not sourced from real teardown/fleet data."
        ),
    )
    cold_soak_offset_k: Sourced[float] = Field(
        ...,
        description=(
            "Milestone E2b: how far below ambient the pack sits at the start of a "
            "COLD_START_COMMUTE characterization run, in K, after an overnight cold "
            "soak. Deliberately small — the pack has real thermal mass and isn't as "
            "exposed as e.g. a windshield, so most of a cold-start's 'cold' comes from "
            "the ambient temperature itself, not this offset. Climate-invariant "
            "placeholder shared across all six profiles; see long_horizon.py."
        ),
    )
    hot_soak_offset_k: Sourced[float] = Field(
        ...,
        description=(
            "Milestone E2b: how far above ambient the pack sits at the start of a "
            "HOT_SOAK_COMMUTE characterization run, in K, after a daytime solar/body-"
            "heating soak. Deliberately much smaller than cabin air's well-documented "
            "20K+ excess in direct sun — the pack is thermally massive and at least "
            "partly shielded in all six reference vehicles. Climate-invariant: "
            "physically this should scale with Climate.solar_irradiance_w_per_m2 (a "
            "pack in Niger at noon vs. Seattle at noon see very different solar "
            "loading), but that refinement is deferred until real soak data exists to "
            "calibrate against. See long_horizon.py."
        ),
    )
    charge_current_limit_curve: list[tuple[Sourced[float], Sourced[float]]] = Field(
        ...,
        description=(
            "Milestone E2e: BMS-enforced charge current ceiling, as a SOC -> scale-"
            "factor curve (values MUST be <=1.0, trusted as an invariant, not "
            "runtime-validated -- same convention as ocv_curve_v/r0_curve_ohm), "
            "multiplying max_charge_c_rate. NOT a fast-charge-only concept despite "
            "motivating this milestone: this is the same cell-protection physics "
            "(lithium-plating risk rises with charge current, especially at high "
            "SOC) that also governs regen braking, just rarely binding there since "
            "regen current is far lower than fast-charge current -- it is why real "
            "EVs show reduced-regen warnings near full charge. "
            "Combined with the existing hot/cold temperature derate scales "
            "(thermal_scale = hot_scale * cold_scale) as an independent "
            "multiplicative factor, applied via battery.available_charge_current_a's "
            "existing final multiplication, not a separate step: a SEPARABLE "
            "approximation (SOC and temperature contributions multiply). Real BMS "
            "limits can be non-separable, particularly where the taper knee shifts "
            "with pack temperature -- revisit if sourced curves become available. "
            "Deliberately IDENTICAL across all six profiles (chemistry is a "
            "controlled variable, per aging.py's convention): any Leaf-vs-Model3 "
            "fast-charge difference must emerge from thermal architecture (Leaf's "
            "hot_scale dropping sharply from no active battery cooling, so its "
            "*effective* ceiling throttles hard even though the *curve* is "
            "identical) rather than from a profile-specific curve shape -- giving "
            "the Leaf a steeper curve later would double-count the throttle effect "
            "its own thermal design already produces and break the causal "
            "attribution this comparison exists to demonstrate. "
            "TODO(spec-sourcing): full C-rate (scale=1.0) to 60% SOC, linear taper "
            "60%->95% down to scale=0.2, constant scale=0.2 ceiling 95%->100%."
        ),
    )


class MotorSpec(BaseModel):
    """A single traction motor. `ManufacturerProfile.motors` holds one or more of these (single/dual-motor)."""

    motor_type: str = Field(..., description="e.g. 'PMSM', 'induction'.")
    peak_power_kw: Sourced[float] = Field(..., description="Peak power output, in kW.")
    continuous_power_kw: Sourced[float] = Field(
        ..., description="Continuous (thermally sustainable) power, in kW."
    )
    peak_torque_nm: Sourced[float] = Field(..., description="Peak torque, in N*m.")
    efficiency_peak: Sourced[float] = Field(..., description="Peak efficiency, as a fraction 0-1.")
    mass_kg: Sourced[float] = Field(..., description="Motor mass, in kilograms.")
    max_winding_temp_k: Sourced[float] = Field(
        ..., description="Maximum stator winding temperature, in kelvin."
    )
    thermal_node: ThermalNode = Field(..., description="Lumped thermal model of the motor.")
    winding_resistance_ref_ohm: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder until real datasheet/teardown data is "
            "available. Lumped winding resistance at winding_resistance_ref_temp_k, in ohms."
        ),
    )
    winding_resistance_ref_temp_k: Sourced[float] = Field(
        ..., description="Reference temperature winding_resistance_ref_ohm was measured at, in kelvin."
    )
    torque_constant_nm_per_a: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder. Motor torque constant Kt relating "
            "torque to current, in N*m/A."
        ),
    )
    iron_loss_coefficient_w_per_rad_per_s: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder analytical loss model coefficient. "
            "Core/iron loss = coefficient * |angular_velocity_rad_per_s|, in W per (rad/s)."
        ),
    )
    mechanical_loss_coefficient_w_per_rad3_per_s3: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder analytical loss model coefficient. "
            "Windage/friction loss = coefficient * |angular_velocity_rad_per_s|^3, in W per (rad/s)^3."
        ),
    )
    regen_extra_loss_fraction: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder. Fractional extra copper loss applied "
            "during regen braking vs. motoring at the same current, dimensionless."
        ),
    )
    cooling_mechanism: Sourced[str] | None = Field(
        default=None,
        description=(
            "How the motor sheds heat, e.g. 'water jacket', 'oil-cooled stator + "
            "rotor with dedicated end-winding oil jets', 'rotor-internal coolant + "
            "stator jacket + bearing-plate cooling lances'. Free text, not an enum: "
            "real motors commonly combine several simultaneous mechanisms, which a "
            "single closed category would misrepresent. Descriptive only — not "
            "read by motor.py yet."
        ),
    )


class InverterSpec(BaseModel):
    """The traction inverter driving one motor."""

    topology: str = Field(..., description="e.g. 'SiC three-phase', 'Si IGBT three-phase'.")
    peak_power_kw: Sourced[float] = Field(..., description="Peak power throughput, in kW.")
    efficiency_peak: Sourced[float] = Field(..., description="Peak efficiency, as a fraction 0-1.")
    switching_frequency_hz: Sourced[float] = Field(..., description="Switching frequency, in Hz.")
    max_junction_temp_k: Sourced[float] = Field(
        ..., description="Maximum semiconductor junction temperature, in kelvin."
    )
    mass_kg: Sourced[float] = Field(..., description="Inverter mass, in kilograms.")
    thermal_node: ThermalNode = Field(..., description="Lumped thermal model of the inverter.")
    peak_current_a: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder. Rated peak AC current, in amps — the "
            "un-derated ceiling inverter.available_current_a scales down from."
        ),
    )
    r_ds_on_ref_ohm: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder until real datasheet data is available. "
            "Switch on-resistance at r_ds_on_ref_temp_k, in ohms."
        ),
    )
    r_ds_on_ref_temp_k: Sourced[float] = Field(
        ..., description="Reference junction temperature r_ds_on_ref_ohm was measured at, in kelvin."
    )
    r_ds_on_temp_coefficient_per_k: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder. Fractional change in r_ds_on per kelvin "
            "above r_ds_on_ref_temp_k, per kelvin."
        ),
    )
    switching_energy_coefficient_j_per_v: Sourced[float] = Field(
        ...,
        description=(
            "TODO(spec-sourcing): placeholder analytical loss model coefficient. "
            "Switching loss = switching_frequency_hz * coefficient * dc_bus_voltage_v, in J/V."
        ),
    )


class ThermalArchitecture(BaseModel):
    """How a vehicle's cooling hardware is wired together."""

    cooling_loops: list[CoolingLoop]
    refrigerant_loops: list[RefrigerantLoop] = Field(
        default_factory=list,
        description=(
            "Direct-expansion refrigerant cooling paths (Milestone D) — separate from "
            "cooling_loops since a DX system has no coolant thermal mass of its own. "
            "Empty for every profile except the BMW i3."
        ),
    )
    thermal_network: ThermalNetwork
    shared_loop: bool = Field(
        ..., description="True if battery and powertrain (motor/inverter) share one coolant loop."
    )
    heat_pump_present: bool = Field(
        ..., description="Whether cabin heating uses a heat pump vs. resistive heat."
    )
    heat_pump_type: Sourced[HeatPumpType] | None = Field(
        default=None,
        description=(
            "The vehicle's overall heat-generation architecture (distinct from "
            "AuxLoads.cabin_heater_type, which is the cabin-air-specific free-text "
            "description used for HVAC power accounting): whether there's no heat "
            "pump at all, a resistive PTC heater, a heat pump, or an integrated "
            "multi-mode system (e.g. Tesla's octovalve) that shares hardware "
            "across cabin/battery/powertrain thermal management. None means not "
            "yet researched/populated for this profile."
        ),
    )
    heat_pump_recovers_powertrain_waste_heat: Sourced[bool] | None = Field(
        default=None,
        description=(
            "Whether the heat pump system reclaims motor/inverter waste heat "
            "(into the cabin loop and/or the battery loop) rather than only "
            "moving heat between cabin air and ambient. True for e.g. Tesla's "
            "octovalve, Audi e-tron, Kia Niro's heat-pump trim. None means not "
            "yet researched/populated for this profile."
        ),
    )
    battery_preconditioning: bool = Field(
        ..., description="Whether the vehicle can pre-heat/pre-cool the battery before a planned fast charge."
    )
    preconditioning_heater_power_w: Sourced[float] | None = Field(
        default=None,
        description=(
            "Milestone: real preconditioning. Electrical power drawn from the battery to actively "
            "pre-heat the pack before a fast charge, in watts -- None when battery_preconditioning is "
            "False. Named for this specific mechanism, not a general-purpose pack heater this project "
            "doesn't otherwise model. Treated as ~100% resistive (all drawn power becomes heat "
            "deposited directly on the battery node, distinct from the battery's own incidental R0 "
            "loss from carrying that current) -- unlike CoolingLoop.chiller_capacity_w/RefrigerantLoop."
            "capacity_w, no separate capacity/COP figure: a resistive preconditioning heater doesn't "
            "have the compressor-style efficiency gap those mechanisms do."
        ),
    )
    preconditioning_target_temp_k: Sourced[float] | None = Field(
        default=None,
        description=(
            "Milestone: real preconditioning. The cell temperature the preconditioning heater stops "
            "at (a real BMS controls to a target, it doesn't just run for a fixed duration regardless) "
            "-- None when battery_preconditioning is False. If the heater's own duration budget "
            "(long_horizon.PRECONDITIONING_DURATION_S) elapses before this temperature is reached "
            "(realistic in genuinely cold conditions), preconditioning simply ends at whatever "
            "temperature it got to -- this field is a stopping condition, not a promise."
        ),
    )


class ControlStrategy(BaseModel):
    """The thermal management control policy for a manufacturer profile."""

    derate_curve: list[tuple[Sourced[float], Sourced[float]]] = Field(
        ..., description="(temp_k, power_fraction) points defining how output power is derated with temperature."
    )
    pump_control_policy: str = Field(
        ..., description="Description of coolant pump control logic, e.g. 'on/off at 35C'."
    )
    fan_control_policy: str = Field(..., description="Description of radiator fan control logic.")
    target_battery_temp_range_k: Sourced[tuple[float, float]] = Field(
        ..., description="(min, max) target battery temperature band the controller tries to hold, in kelvin."
    )


class AuxLoads(BaseModel):
    """Non-traction electrical loads that draw pack energy and generate/consume heat."""

    hvac_power_w: Sourced[float] = Field(..., description="Typical cabin HVAC electrical draw, in watts.")
    cabin_heater_type: str = Field(..., description="e.g. 'resistive PTC', 'heat pump'.")
    accessory_load_w: Sourced[float] = Field(
        ..., description="Baseline 12V/accessory electrical draw, in watts."
    )
    dc_dc_converter_efficiency: Sourced[float] = Field(
        ..., description="Efficiency of the HV-to-12V DC-DC converter, as a fraction 0-1."
    )


class ManufacturerProfile(BaseModel):
    """A complete vehicle drivetrain + thermal spec — one of the six reference vehicles, or a counterfactual."""

    name: str
    model_year_range: tuple[int, int]
    vehicle_dynamics: VehicleDynamics
    battery: BatterySpec
    motors: list[MotorSpec] = Field(
        ..., description="One entry for single-motor vehicles, two for dual-motor AWD."
    )
    inverter: InverterSpec
    thermal_architecture: ThermalArchitecture
    control_strategy: ControlStrategy
    aux_loads: AuxLoads


class Climate(BaseModel):
    """A simplified parametric ambient-temperature environment for a scenario."""

    location_name: str
    mean_annual_temp_k: Sourced[float] = Field(
        ..., description="Mean annual ambient temperature, in kelvin."
    )
    seasonal_amplitude_k: Sourced[float] = Field(
        ..., description="Peak deviation from mean across the year, in kelvin."
    )
    daily_amplitude_k: Sourced[float] = Field(
        ..., description="Peak deviation from the day's mean across a 24h cycle, in kelvin."
    )
    altitude_m: Sourced[float] = Field(..., description="Elevation above sea level, in meters.")
    solar_irradiance_w_per_m2: Sourced[float] = Field(
        ..., description="Typical peak solar irradiance, in W/m^2, for cabin heat-gain modeling."
    )


class TripType(StrEnum):
    """The representative-trip taxonomy for Milestone E2's long-horizon aging
    extrapolation. A closed set — unlike `UsageProfile.driving_style`'s free
    text — because the day-type-mix weighting and the dashboard's per-type
    SOH-loss attribution both need to group/filter on this directly, same
    rationale as `HeatPumpType`.

    Deliberately excludes a "parked/soak" member: parked time is not a
    discrete trip occurrence, it's the continuous background state between
    occurrences of these five, modeled by long_horizon.py's own ambient-
    relaxation model rather than characterized via a Tier-1 run.
    """

    COLD_START_COMMUTE = "cold_start_commute"
    HOT_SOAK_COMMUTE = "hot_soak_commute"
    HIGHWAY = "highway"
    CITY_STOP_AND_GO = "city_stop_and_go"
    FAST_CHARGE_SESSION = "fast_charge_session"


class TripPhaseAging(BaseModel):
    """One named phase within a representative trip's aging-relevant signature.

    Coarser than a full per-step trace, deliberately: aging.py's
    compute_aging_step is a closed-form function of (cell_temp_k, soc,
    applied_current_a, dt) only, so a Tier-2 long-horizon run can reconstruct
    a trip occurrence's calendar_delta/cycle_delta from these per-phase
    averages/peaks without replaying the trip's full driver/controller/
    thermal physics. Phase-level (not whole-trip-averaged) because both
    calendar aging's Arrhenius term and cycle aging's C-rate power law are
    convex in their inputs — averaging temperature/current across an entire
    trip before applying those nonlinearities understates aging versus a
    trip with a brief hot/high-current phase and a long idle tail.
    """

    phase_name: str = Field(..., description="e.g. 'warm_up', 'cruise', 'fast_charge_ramp'.")
    duration_s: float
    mean_cell_temp_k: float = Field(
        ..., description="Battery pack ThermalNode temperature, averaged over this phase."
    )
    peak_cell_temp_k: float
    mean_soc: float = Field(..., description="0-1.")
    mean_c_rate: float = Field(
        ..., description="abs(current_a) / capacity_nominal_ah, averaged over this phase."
    )
    mean_current_a: float = Field(
        ...,
        description=(
            "Milestone: cold-charging lithium plating (Q-0028). SIGNED mean "
            "current over this phase (this project's convention: negative = "
            "charging, positive = discharging) -- unlike mean_c_rate, NOT "
            "abs()'d. Needed because compute_aging_step's cold_charge_plating "
            "term is charge-only (gated on applied_current_a < 0), and a Tier-2 "
            "long-horizon run never replays real per-step current -- it "
            "reconstructs aging purely from this phase-level summary, so the "
            "sign has to survive here or the charge-only gate is silently "
            "unreachable outside Tier-1's own per-second stepping."
        ),
    )
    throughput_ah: float = Field(
        ..., description="Amp-hours of charge moved (either direction) during this phase."
    )


class TripCharacterization(BaseModel):
    """Output of a Tier-1 (full-fidelity `run_scenario`) characterization run for
    one `TripType` at one ambient-temperature bucket, for one specific
    `ManufacturerProfile`.

    NOT reusable across profiles or climates — must be recomputed per
    profile x trip_type x ambient bucket, same as every other profile-
    specific quantity in this project. `ambient_bucket_temp_k` is the
    constant ambient the Tier-1 run was executed under; Tier-2 interpolates
    between a TripType's bucketed characterizations against the actual
    simulated day's ambient temperature.
    """

    trip_type: TripType
    ambient_bucket_temp_k: float = Field(
        ..., description="Constant ambient temperature this characterization run used, in kelvin."
    )
    phases: list[TripPhaseAging] = Field(..., description="Ordered phases; see TripPhaseAging.")
    odometer_km_delta: float = Field(..., description="Distance covered during this trip, in km.")
    final_soc: float = Field(
        ..., description="Battery SOC at trip end, 0-1 — the starting SOC for whatever follows."
    )
    final_node_temps_k: dict[str, float] = Field(
        ...,
        description=(
            "ThermalNode name -> temperature at trip end, in kelvin — thermal "
            "continuity into the next occurrence or parked period."
        ),
    )


class AnnualDayTypeMix(BaseModel):
    """How many times per year one `TripType` occurs, for Tier-2 long-horizon
    extrapolation. Days not covered by any entry in `UsageProfile.day_type_mix`
    are parked/soak days.
    """

    trip_type: TripType
    occurrences_per_year: Sourced[int] = Field(
        ...,
        description=(
            "How many days per year this trip type occurs — a usage "
            "assumption, same Sourced status as UsageProfile's other fields."
        ),
    )


class UsageProfile(BaseModel):
    """How the vehicle is driven over its lifetime."""

    annual_mileage_km: Sourced[float] = Field(
        ..., description="Average annual distance driven, in km."
    )
    mean_trip_km: Sourced[float] = Field(..., description="Mean single-trip distance, in km.")
    trip_km_std: Sourced[float] = Field(
        ..., description="Standard deviation of single-trip distance, in km."
    )
    driving_style: str = Field(
        ..., description="e.g. 'eco', 'normal', 'aggressive' — maps to duty-cycle intensity."
    )
    garage_parked: bool = Field(
        ..., description="Whether the vehicle is typically parked in a garage (affects thermal soak)."
    )
    day_type_mix: list[AnnualDayTypeMix] = Field(
        default_factory=list,
        description=(
            "Annual occurrence counts per TripType, for Milestone E2's Tier-2 "
            "long-horizon extrapolation. Empty (default) means long-horizon "
            "extrapolation isn't configured for this profile — every existing "
            "profile YAML and test fixture is unaffected. When non-empty, "
            "validated against annual_mileage_km at long-horizon entry (not by "
            "this type itself), same pattern as profile_loader.py's own "
            "load-time validation."
        ),
    )


class ChargingProfile(BaseModel):
    """How and how often the vehicle is charged."""

    charger_type: str = Field(
        ..., description="Predominant charger type, e.g. 'Level 2', 'DC fast charge'."
    )
    charge_power_kw: Sourced[float] = Field(..., description="Typical charge power, in kW.")
    typical_start_soc: Sourced[float] = Field(
        ..., description="Typical state of charge at charge start, 0-1."
    )
    typical_end_soc: Sourced[float] = Field(
        ..., description="Typical state of charge at charge end, 0-1."
    )
    dc_fast_charge_fraction: Sourced[float] = Field(
        ..., description="Fraction of charging events that are DC fast charges, 0-1."
    )
    preconditioning_enabled: bool = Field(
        ..., description="Whether the driver/vehicle typically preconditions the battery before fast charging."
    )
    dc_charge_power_kw: Sourced[float] = Field(
        ...,
        description=(
            "Milestone E2e: charger-rated power during a DC fast-charge session, "
            "in kW -- distinct from charge_power_kw, which is the predominant "
            "(typically slower) charger type's power. Represents the charger's "
            "rating, not necessarily the vehicle's own max acceptance rate; if a "
            "second charger type is ever modeled, this may need to split between "
            "a charger abstraction and a per-vehicle max-acceptance-rate field, "
            "since real effective charge rate is min(charger, vehicle acceptance)."
        ),
    )
    charge_reset_hour: Sourced[float] = Field(
        ...,
        description=(
            "Hour of day (0-24, local to Tier-2's day-boundary convention -- "
            "see long_horizon.py's simulate_long_horizon) at which the parked-"
            "SOC decay model applies its one daily charge event, instantaneously "
            "setting SOC to typical_end_soc. Named 'reset', not 'completion': "
            "this models the INSTANT SOC is set, not a real charge session's "
            "start/duration/taper -- there is no charge-session physics here "
            "(power caps, taper, thermal coupling), only the resulting daily "
            "SOC trajectory's shape, which is what calendar aging's SOC-"
            "sensitivity term actually needs. A day's trip is simulated "
            "starting at the day boundary (hour 0), so a value late in the "
            "24h range (e.g. ~22.0) represents charging completing shortly "
            "before the NEXT day's trip, matching realistic overnight "
            "charging. See docs/DESIGN_QUESTIONS.md Q-0012."
        ),
    )


class DTPolicy(BaseModel):
    """Adaptive time-stepping policy for the sim loop.

    Deliberately simple for this scaffold: a min/max dt bound plus a
    per-step temperature-change threshold, rather than a full ODE-solver
    error controller. Can be upgraded when `sim.py` is actually built.
    """

    min_dt_s: float = Field(..., description="Smallest allowed simulation step, in seconds.")
    max_dt_s: float = Field(..., description="Largest allowed simulation step, in seconds.")
    max_temp_change_per_step_k: float = Field(
        ..., description="Step size is reduced if any node would change more than this per step, in kelvin."
    )
    method: str = Field(..., description="Integration method identifier, e.g. 'adaptive-euler'.")


class Scenario(BaseModel):
    """A complete, runnable simulation input: a vehicle in an environment, driven and charged a certain way.

    `manufacturer_profile` is optional: it's required for a standalone
    scenario run, but omitted when this `Scenario` is embedded in a
    `MistakeCase`, where `failure_profile`/`counterfactual_profile` supply
    the vehicle instead and this `Scenario` supplies only the shared
    usage/climate/charging context both runs share.
    """

    name: str
    manufacturer_profile: ManufacturerProfile | None = Field(
        default=None, description="The vehicle to simulate. Omitted when embedded in a MistakeCase."
    )
    climate: Climate
    usage_profile: UsageProfile
    charging_profile: ChargingProfile
    duration_years: int = Field(..., description="Simulated vehicle lifetime, in years.")
    overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Counterfactual parameter patch applied on top of manufacturer_profile.",
    )
    dt_policy: DTPolicy
    random_seed: int


class ForkPoint(BaseModel):
    """A branch taken from an existing `SimState` to explore a what-if."""

    fork_id: str
    parent_state_id: str = Field(..., description="The SimState.state_id this fork branched from.")
    sim_time_s: float = Field(..., description="Simulation time at which the fork was created, in seconds.")
    description: str = Field(..., description="Human-readable description of the what-if being explored.")
    overrides: dict[str, Any] = Field(..., description="Parameter patch applied at the fork point.")
    created_at: datetime = Field(..., description="Wall-clock time the fork was created.")


class CausalEvent(BaseModel):
    """One node in the causal trace explaining why the simulation did something.

    Referenced by ID rather than embedded in `SimState`, and stored in an
    external append-only log, so that forking a `SimState` stays cheap even
    across a 20-30 year, many-fork simulation history.
    """

    event_id: str
    sim_time_s: float = Field(..., description="Simulation time this event occurred, in seconds.")
    event_type: str = Field(
        ..., description="e.g. 'DERATE_TRIGGERED', 'PUMP_SPEED_CHANGE', 'FAST_CHARGE_START'."
    )
    description: str = Field(..., description="Human-readable explanation of what happened and why.")
    caused_by: list[str] = Field(
        default_factory=list, description="event_ids of upstream CausalEvents that caused this one."
    )
    affected_component: str = Field(..., description="Name of the component this event pertains to.")
    severity: str = Field(..., description="e.g. 'info', 'warning', 'critical'.")


class SimState(BaseModel):
    """A complete, serializable snapshot of simulation state at one point in time.

    Must round-trip through `model_dump_json`/`model_validate_json` with no
    loss, since forking depends on cloning this state without touching the
    parent run's live objects.
    """

    state_id: str
    scenario_id: str
    sim_time_s: float = Field(..., description="Elapsed simulation time, in seconds.")
    odometer_km: float = Field(..., description="Cumulative distance driven, in km.")
    battery_soc: float = Field(..., description="Battery state of charge, 0-1.")
    battery_soh: float = Field(..., description="Battery state of health (capacity retention), 0-1.")
    node_temps_k: dict[str, float] = Field(
        ..., description="ThermalNode name -> current temperature, in kelvin."
    )
    rng_state: dict[str, Any] = Field(..., description="Serialized numpy Generator bit-generator state.")
    fork_lineage: list[str] = Field(
        default_factory=list, description="Ordered list of fork_ids from the root run to this state."
    )
    step_count: int = Field(..., description="Number of simulation steps taken to reach this state.")
    dt_current_s: float = Field(..., description="Current adaptive step size, in seconds.")
    speed_mps: float = Field(..., description="Current vehicle speed, in m/s.")
    battery_v_rc_v: float = Field(..., description="Battery single-RC-pair state voltage, in volts.")
    driver_integral_error_state: float = Field(
        ..., description="Driver PI controller's integral speed-error term, in meters."
    )
    battery_throughput_ah: float = Field(
        ..., description="Cumulative |current|*dt charge throughput, in amp-hours (aging-model input, unused so far)."
    )
    battery_voltage_v: float = Field(
        ...,
        description=(
            "Battery terminal voltage as of the end of the previous step, in volts. "
            "controller.py's step uses this (not the current step's not-yet-computed "
            "value) for its current<->torque conversions."
        ),
    )
    parked_duration_s: float = Field(
        default=0.0,
        description=(
            "Elapsed time, in seconds, since the vehicle was last driving or "
            "charging. Resets to 0 on driving/charging, increments while parked-"
            "idle. Drives sim.py's extended-parking dt tier: once parked long "
            "enough for thermal transients to settle, dt can jump much further "
            "than the ordinary parked-tier step."
        ),
    )


class AgingStepResult(BaseModel):
    """One step's battery aging delta, returned by `aging.compute_aging_step`."""

    new_soh: float = Field(..., description="Battery state of health after this step's aging, 0-1.")
    calendar_delta: float = Field(
        ..., description="SOH lost this step from calendar aging alone (non-negative)."
    )
    cycle_delta: float = Field(
        ..., description="SOH lost this step from cycle (throughput) aging alone (non-negative)."
    )
    cold_charge_plating_delta: float = Field(
        ...,
        description=(
            "Milestone: cold-charging lithium plating (Q-0028). SOH lost this "
            "step from cold-charging plating alone (non-negative); zero unless "
            "applied_current_a < 0 (charging) and cell_temp_k is below "
            "BatterySpec.cold_charge_plating_onset_temp_k. Kept separate from "
            "cycle_delta, not folded into it, despite both being throughput-"
            "driven -- the two mechanisms have opposite temperature directions "
            "(SEI growth vs. lithium plating) and this project's own causal-"
            "trace goal calls for keeping physically distinct mechanisms "
            "separately attributable, not merged into a less-specific bucket."
        ),
    )


class MistakeCase(BaseModel):
    """A paired teaching unit: a real failure mode and the fix that prevents it.

    A failure alone is just a sad story — the lesson lives in the delta
    between `failure_profile` and `counterfactual_profile`, run under the
    same shared `scenario` (usage + climate + charging), so the only
    variable is the design decision being taught.
    """

    name: str
    failure_mode: str = Field(
        ...,
        description=(
            "Short description of what goes wrong, e.g. 'thermal runaway risk from "
            "passive air cooling in high ambient heat'."
        ),
    )
    failure_profile: ManufacturerProfile = Field(
        ..., description="The vehicle configuration exhibiting the failure mode."
    )
    counterfactual_profile: ManufacturerProfile = Field(
        ..., description="The fixed vehicle configuration that avoids it."
    )
    scenario: Scenario = Field(
        ..., description="Shared usage/climate/charging context both profiles are run under."
    )
    evidence: list[str] = Field(..., description="Citations supporting the failure_mode claim.")
    lesson: str = Field(..., description="The educational takeaway.")
    severity: str = Field(..., description="e.g. 'info', 'warning', 'critical'.")


class MotorHeatResult(BaseModel):
    """Output of `motor.compute_motor_heat`: everything `MotorStepResult` has except
    `winding_temp_k`, since this doesn't advance the motor's own thermal state — see
    that function's docstring for why (sim.py's combined multi-node thermal solve).
    """

    electrical_power_w: float = Field(
        ...,
        description=(
            "Electrical power at the motor terminals, in watts. Positive draws from "
            "the bus (motoring), negative returns power to it (regen)."
        ),
    )
    mechanical_power_w: float = Field(
        ...,
        description=(
            "Mechanical power at the motor shaft, in watts. Positive delivers to "
            "the load, negative absorbs from it."
        ),
    )
    current_a: float = Field(..., description="Motor phase current magnitude, in amps.")
    copper_loss_w: float = Field(..., description="Resistive winding loss, in watts.")
    iron_loss_w: float = Field(..., description="Core/iron loss, in watts.")
    mechanical_loss_w: float = Field(..., description="Windage/friction loss, in watts.")
    total_heat_w: float = Field(..., description="copper_loss_w + iron_loss_w + mechanical_loss_w, in watts.")
    applied_torque_nm: float = Field(
        ...,
        description="Torque actually applied after derating, in N*m (may be less in magnitude than requested).",
    )
    causal_events: list[CausalEvent] = Field(default_factory=list)


class MotorStepResult(BaseModel):
    """Output of one `motor.step_motor` call: losses, power flow, and updated winding temp."""

    electrical_power_w: float = Field(
        ...,
        description=(
            "Electrical power at the motor terminals, in watts. Positive draws from "
            "the bus (motoring), negative returns power to it (regen)."
        ),
    )
    mechanical_power_w: float = Field(
        ...,
        description=(
            "Mechanical power at the motor shaft, in watts. Positive delivers to "
            "the load, negative absorbs from it."
        ),
    )
    current_a: float = Field(..., description="Motor phase current magnitude, in amps.")
    copper_loss_w: float = Field(..., description="Resistive winding loss, in watts.")
    iron_loss_w: float = Field(..., description="Core/iron loss, in watts.")
    mechanical_loss_w: float = Field(..., description="Windage/friction loss, in watts.")
    winding_temp_k: float = Field(..., description="Updated winding temperature after this step, in kelvin.")
    applied_torque_nm: float = Field(
        ...,
        description="Torque actually applied after derating, in N*m (may be less in magnitude than requested).",
    )
    causal_events: list[CausalEvent] = Field(default_factory=list)


class InverterHeatResult(BaseModel):
    """Output of `inverter.compute_inverter_heat`: everything `InverterStepResult` has
    except `junction_temp_k` — see that function's docstring for why.
    """

    conduction_loss_w: float = Field(..., description="I^2 * R_ds_on(T_j) conduction loss, in watts.")
    switching_loss_w: float = Field(..., description="Switching loss, in watts.")
    total_loss_w: float = Field(..., description="Sum of conduction and switching loss, in watts.")
    applied_current_a: float = Field(
        ..., description="Current actually allowed through after derating, in amps (may be less than requested)."
    )
    causal_events: list[CausalEvent] = Field(default_factory=list)


class InverterStepResult(BaseModel):
    """Output of one `inverter.step_inverter` call: losses and updated junction temp."""

    conduction_loss_w: float = Field(..., description="I^2 * R_ds_on(T_j) conduction loss, in watts.")
    switching_loss_w: float = Field(..., description="Switching loss, in watts.")
    total_loss_w: float = Field(..., description="Sum of conduction and switching loss, in watts.")
    junction_temp_k: float = Field(..., description="Updated junction temperature after this step, in kelvin.")
    applied_current_a: float = Field(
        ..., description="Current actually allowed through after derating, in amps (may be less than requested)."
    )
    causal_events: list[CausalEvent] = Field(default_factory=list)


class BatteryElectricalResult(BaseModel):
    """Output of `battery.compute_battery_electrical_step`: everything `BatteryStepResult`
    has except `cell_temp_k` — see that function's docstring for why.
    """

    terminal_voltage_v: float = Field(..., description="V = OCV - I*R0 - V_RC, in volts.")
    ocv_v: float = Field(..., description="Open-circuit voltage at this step's SOC and cell_temp_k, in volts.")
    applied_current_a: float = Field(
        ...,
        description=(
            "Current actually applied after all limits, in amps. Positive is "
            "discharge, negative is charge; may be smaller in magnitude than requested."
        ),
    )
    soc: float = Field(..., description="Updated state of charge after this step, 0-1.")
    v_rc_v: float = Field(..., description="Updated single-RC-pair state voltage, in volts.")
    irreversible_heat_w: float = Field(..., description="I^2*R0 + I^2*R1 resistive heat, in watts.")
    entropic_heat_w: float = Field(
        ..., description="I*T*dOCV/dT reversible heat, in watts. Sign flips between charge and discharge."
    )
    causal_events: list[CausalEvent] = Field(default_factory=list)


class BatteryStepResult(BaseModel):
    """Output of one `battery.step_battery` call: electrical state, losses, and updated SOC/temp."""

    terminal_voltage_v: float = Field(..., description="V = OCV - I*R0 - V_RC, in volts.")
    ocv_v: float = Field(..., description="Open-circuit voltage at this step's SOC and cell_temp_k, in volts.")
    applied_current_a: float = Field(
        ...,
        description=(
            "Current actually applied after all limits, in amps. Positive is "
            "discharge, negative is charge; may be smaller in magnitude than requested."
        ),
    )
    soc: float = Field(..., description="Updated state of charge after this step, 0-1.")
    v_rc_v: float = Field(..., description="Updated single-RC-pair state voltage, in volts.")
    cell_temp_k: float = Field(..., description="Updated pack temperature after this step, in kelvin.")
    irreversible_heat_w: float = Field(..., description="I^2*R0 + I^2*R1 resistive heat, in watts.")
    entropic_heat_w: float = Field(
        ..., description="I*T*dOCV/dT reversible heat, in watts. Sign flips between charge and discharge."
    )
    causal_events: list[CausalEvent] = Field(default_factory=list)


class CoolingStepResult(BaseModel):
    """Output of one `cooling.step_cooling` call: radiator heat exchange and parasitic loads."""

    coolant_outlet_temp_k: float = Field(
        ..., description="Coolant temperature leaving the radiator/bypass mix, in kelvin."
    )
    heat_rejected_w: float = Field(
        ..., description="Heat rejected to ambient air at the RADIATOR only, in watts (chiller is separate)."
    )
    chiller_heat_removed_w: float = Field(
        default=0.0,
        description=(
            "Heat actively removed by the chiller, in watts -- 0.0 if chiller_present is False or "
            "chiller_duty is 0. Unlike heat_rejected_w, not capped at (coolant_temp - ambient): this "
            "is the one mechanism that can pull coolant below ambient."
        ),
    )
    coolant_mass_flow_kg_per_s: float = Field(..., description="Total loop coolant mass flow, in kg/s.")
    pump_power_w: float = Field(..., description="Pump parasitic electrical power draw, in watts.")
    fan_power_w: float = Field(..., description="Fan parasitic electrical power draw, in watts.")
    chiller_power_w: float = Field(
        default=0.0, description="Chiller's own electrical power draw this step, in watts (chiller_power_w * duty)."
    )
    thermostat_open_fraction: float = Field(
        ..., description="Fraction of coolant flow routed through the radiator vs. bypassed, 0-1."
    )


class DriverStepResult(BaseModel):
    """Output of one `driver.step_driver` call: torque request split into regen/mechanical braking."""

    torque_request_nm: float = Field(
        ...,
        description=(
            "Total requested torque after PI control and accel/brake clamping, in N*m. "
            "Positive drives, negative decelerates (regen + mechanical combined)."
        ),
    )
    regen_torque_nm: float = Field(
        ..., description="Portion of a negative torque_request_nm assigned to regen, in N*m (<=0)."
    )
    mechanical_brake_torque_nm: float = Field(
        ...,
        description=(
            "Magnitude of deceleration beyond regen_torque_limit_nm, in N*m (>=0). "
            "TODO: mechanical brakes aren't modeled thermally yet, just as a torque sink."
        ),
    )
    mechanical_brake_active: bool = Field(
        ..., description="True if mechanical_brake_torque_nm > 0, i.e. regen alone couldn't cover the request."
    )
    integral_error_state: float = Field(
        ...,
        description=(
            "Updated PI integral term (integral of speed error over time), in m. Must be "
            "threaded through explicitly so a fork branching mid-run doesn't replay with "
            "the wrong integral history."
        ),
    )


class ControllerStepResult(BaseModel):
    """Output of one `controller.step_controller` call: arbitrated commands and cooling policy."""

    commanded_torque_nm: float = Field(
        ...,
        description=(
            "Driver's torque request clamped to the most restrictive of the motor/inverter/"
            "battery/SOC/traction limits, in N*m."
        ),
    )
    commanded_pump_duty: dict[str, float] = Field(
        ..., description="CoolingLoop.name -> commanded coolant pump duty, 0-1, for that loop."
    )
    commanded_fan_duty: dict[str, float] = Field(
        ..., description="CoolingLoop.name -> commanded radiator fan duty, 0-1, for that loop."
    )
    commanded_chiller_duty: dict[str, float] = Field(
        ...,
        description=(
            "CoolingLoop.name -> commanded chiller duty, 0-1, for that loop. 0.0 for any loop with "
            "chiller_present=False. A genuine second stage (Q-0021), not a scaled copy of fan duty — "
            "see controller.py's module docstring."
        ),
    )
    commanded_refrigerant_duty: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "RefrigerantLoop.name -> commanded duty, 0-1, for that loop (Milestone D). Empty for "
            "profiles with no refrigerant_loops."
        ),
    )
    battery_current_demand_a: float = Field(
        ...,
        description=(
            "Battery pack current implied by commanded_torque_nm via mechanical power alone "
            "(torque*speed/battery_voltage_v), in amps (positive discharge, negative charge). "
            "A rough, energy-INCOMPLETE estimate — it ignores motor/inverter losses entirely, "
            "same simplification as the torque<->current conversions elsewhere in this module. "
            "Good enough for arbitration; NOT what should drive battery.step_battery's actual "
            "current_request_a (that needs motor/inverter's real computed electrical draw, which "
            "isn't known yet at this point in the step sequence — see sim.py's own derivation)."
        ),
    )
    causal_events: list[CausalEvent] = Field(default_factory=list)


class EnergyTotals(BaseModel):
    """Cumulative energy accounting over a `run_scenario` call, in joules."""

    consumed_j: float = Field(..., description="Total electrical energy drawn from the battery (discharge).")
    regen_recovered_j: float = Field(..., description="Total electrical energy returned to the battery (charge).")
    motor_loss_j: float = Field(..., description="Sum of motor copper+iron+mechanical loss over the run.")
    inverter_loss_j: float = Field(..., description="Sum of inverter conduction+switching loss over the run.")
    battery_loss_j: float = Field(..., description="Sum of battery irreversible+entropic heat over the run.")
    radiator_rejected_j: float = Field(..., description="Sum of heat rejected at the radiator over the run.")
    chiller_rejected_j: float = Field(
        default=0.0, description="Sum of heat actively removed by any loop's chiller over the run."
    )
    refrigerant_rejected_j: float = Field(
        default=0.0,
        description="Sum of heat actively removed by any RefrigerantLoop over the run (Milestone D).",
    )
    aux_j: float = Field(..., description="Sum of auxiliary (HVAC/accessory) electrical draw over the run.")
    cooling_parasitic_j: float = Field(
        default=0.0,
        description=(
            "Sum of cooling-loop parasitic electrical draw (pump+fan+chiller+refrigerant compressor) "
            "over the run, charged to the battery the same way aux_j is. Q-0021: this closes a "
            "pre-existing gap where pump/fan draw was computed by step_cooling but never charged to "
            "battery_current_request_a or tracked here. A RefrigerantLoop's compressor draw (Milestone "
            "D) shares this same bucket rather than getting its own — it's the same HV-bus parasitic-"
            "draw category."
        ),
    )


class SimTimeSeries(BaseModel):
    """Per-step history over a `run_scenario` call. Every list is the same length (one
    entry per step, including the initial state).
    """

    time_s: list[float] = Field(default_factory=list)
    speed_mps: list[float] = Field(default_factory=list)
    requested_torque_nm: list[float] = Field(
        default_factory=list, description="Driver's raw torque request each step, in N*m (see SimStepRecord)."
    )
    commanded_torque_nm: list[float] = Field(default_factory=list)
    motor_electrical_power_w: list[float] = Field(default_factory=list)
    soc: list[float] = Field(default_factory=list)
    battery_current_a: list[float] = Field(
        default_factory=list,
        description=(
            "Milestone E2b: SimStepRecord.applied_current_a history, for "
            "long_horizon.py's characterization runner (mean-C-rate extraction)."
        ),
    )
    charging: list[bool] = Field(
        default_factory=list,
        description=(
            "Milestone E2e: SimStepRecord.charging history, so a charging session "
            "is distinguishable from an idle park in the time series."
        ),
    )
    node_temps_k: dict[str, list[float]] = Field(
        default_factory=dict, description="ThermalNode name -> temperature history, in kelvin."
    )
    pump_duty: list[dict[str, float]] = Field(
        default_factory=list, description="Per-step CoolingLoop.name -> commanded pump duty, 0-1."
    )
    fan_duty: list[dict[str, float]] = Field(
        default_factory=list, description="Per-step CoolingLoop.name -> commanded fan duty, 0-1."
    )
    chiller_duty: list[dict[str, float]] = Field(
        default_factory=list, description="Per-step CoolingLoop.name -> commanded chiller duty, 0-1."
    )
    refrigerant_duty: list[dict[str, float]] = Field(
        default_factory=list, description="Per-step RefrigerantLoop.name -> commanded duty, 0-1 (Milestone D)."
    )
    heat_rejected_w: list[float] = Field(default_factory=list, description="Radiator-only, see SimStepRecord.")
    chiller_heat_removed_w: list[float] = Field(default_factory=list)
    refrigerant_heat_removed_w: list[float] = Field(default_factory=list)
    active_limit_event_types: list[list[str]] = Field(
        default_factory=list, description="controller.py CausalEvent.event_types active at each step."
    )


class SimStepRecord(BaseModel):
    """Output of `sim.advance_one_step`: the new state plus everything `run_scenario`
    needs to extend its time series and energy totals without re-deriving them.
    """

    new_state: SimState
    causal_events: list[CausalEvent] = Field(
        default_factory=list, description="Every CausalEvent logged during this one step, across all components."
    )
    requested_torque_nm: float = Field(
        ...,
        description=(
            "Driver's raw torque request this step, in N*m, BEFORE controller.py's "
            "arbitration clamp (see commanded_torque_nm for the post-arbitration value)."
        ),
    )
    commanded_torque_nm: float
    motor_mechanical_power_w: float = Field(
        ..., description="Mechanical power at the motor shaft this step, in watts (see MotorHeatResult)."
    )
    motor_electrical_power_w: float
    battery_electrical_power_w: float = Field(
        ...,
        description=(
            "terminal_voltage_v * applied_current_a, in watts. Positive discharges "
            "(consumed), negative charges (regen-recovered)."
        ),
    )
    battery_ocv_power_w: float = Field(
        ...,
        description=(
            "ocv_v * applied_current_a, in watts: the battery's gross chemical energy "
            "conversion rate, before its own internal (R0/R1) loss. terminal_voltage_v * "
            "applied_current_a (battery_electrical_power_w) is this minus that internal loss."
        ),
    )
    pump_duty: dict[str, float]
    fan_duty: dict[str, float]
    chiller_duty: dict[str, float]
    refrigerant_duty: dict[str, float] = Field(
        default_factory=dict, description="RefrigerantLoop.name -> commanded duty, 0-1, this step (Milestone D)."
    )
    heat_rejected_w: float = Field(..., description="Radiator-only, summed across loops (chiller is separate).")
    chiller_heat_removed_w: float = Field(
        default=0.0, description="Heat actively removed by any loop's chiller this step, summed across loops."
    )
    refrigerant_heat_removed_w: float = Field(
        default=0.0,
        description="Heat actively removed by any RefrigerantLoop this step, summed across loops (Milestone D).",
    )
    motor_loss_w: float
    inverter_loss_w: float
    battery_loss_w: float
    aux_power_w: float
    cooling_parasitic_w: float = Field(
        default=0.0,
        description=(
            "Pump+fan+chiller+refrigerant-compressor electrical draw this step, summed across loops, "
            "in watts. Charged to the battery the same way aux_power_w is (Q-0021's pre-existing-gap "
            "fix; a RefrigerantLoop's compressor draw shares this bucket, Milestone D)."
        ),
    )
    active_limit_event_types: list[str] = Field(
        default_factory=list, description="controller.py's CausalEvent.event_types active this step."
    )
    applied_current_a: float = Field(
        ...,
        description=(
            "Milestone E2b: BatteryElectricalResult.applied_current_a passed through "
            "for long_horizon.py's characterization runner (mean-C-rate extraction "
            "needs current, which wasn't exposed outside battery.py's own step before "
            "this). Positive is discharge, negative is charge, same convention as "
            "battery_electrical_power_w."
        ),
    )
    charging: bool = Field(
        ...,
        description=(
            "Milestone E2e: True only for a step produced by sim.advance_charging_step, "
            "False everywhere else (explicitly passed False by advance_one_step, not a "
            "field default). Without this, a charging session and an idle park look "
            "identical in the resulting time series (speed_mps=0.0, "
            "requested_torque_nm=0.0, everything else quiet) -- a real usability gap "
            "for the dashboard, not just a data-modeling nicety."
        ),
    )


class RunResult(BaseModel):
    """Output of `sim.run_scenario`: the full record of one simulation run."""

    final_state: SimState
    time_series: SimTimeSeries
    causal_events: list[CausalEvent] = Field(default_factory=list)
    energy_totals: EnergyTotals


class LongHorizonCheckpoint(BaseModel):
    """One point in a Tier-2 long-horizon run's SOH trajectory — coarser than
    `SimState` (one lumped pack-temperature proxy, no driver/controller/motor/
    inverter state) because Tier-2 doesn't run per-second physics between
    checkpoints.
    """

    day_index: int = Field(..., description="Days elapsed since day 0 of the long-horizon run.")
    battery_soh: float = Field(..., description="0-1.")
    calendar_soh_loss_cumulative: float = Field(
        ...,
        description=(
            "Cumulative SOH lost to calendar aging alone, since day 0. Kept "
            "separate from cycle_soh_loss_cumulative so degradation can be "
            "attributed to age-at-temperature vs. usage, not collapsed into "
            "a single number."
        ),
    )
    cycle_soh_loss_cumulative: float = Field(
        ..., description="Cumulative SOH lost to cycle (throughput) aging alone, since day 0."
    )
    cold_charge_plating_soh_loss_cumulative: float = Field(
        ...,
        description=(
            "Milestone: cold-charging lithium plating (Q-0028). Cumulative SOH "
            "lost to cold-charging lithium plating alone, since day 0. Kept "
            "separate from cycle_soh_loss_cumulative, not folded into it, for "
            "the same attribution reason calendar_soh_loss_cumulative is kept "
            "separate from cycle_soh_loss_cumulative."
        ),
    )
    battery_soc: float = Field(..., description="0-1, at end of this day.")
    pack_cell_temp_k: float = Field(..., description="Lumped battery pack temperature at end of this day.")
    odometer_km: float


class LongHorizonRunResult(BaseModel):
    """Output of Milestone E2's Tier-2 long-horizon extrapolation engine."""

    checkpoints: list[LongHorizonCheckpoint]
    trip_characterizations: list[TripCharacterization] = Field(
        ...,
        description=(
            "The Tier-1 characterization runs this extrapolation was built "
            "from — kept for provenance/debugging, not replayed."
        ),
    )


class MistakeCaseResult(BaseModel):
    """Output of `mistake.run_mistake_case`: the paired long-horizon runs a
    `MistakeCase` produces, plus the computed delta — per `MistakeCase`'s own
    docstring ("the lesson lives in the delta"), computed once here rather
    than left to every caller to re-derive, matching the precedent
    `mc.MCResult` already established (percentile computation lives in core,
    not dashboard/presentation code).
    """

    case_name: str
    failure_result: LongHorizonRunResult
    counterfactual_result: LongHorizonRunResult
    final_soh_failure: float = Field(..., description="battery_soh at failure_result's final checkpoint, 0-1.")
    final_soh_counterfactual: float = Field(
        ..., description="battery_soh at counterfactual_result's final checkpoint, 0-1."
    )
    soh_delta: float = Field(
        ...,
        description=(
            "final_soh_counterfactual - final_soh_failure. Positive means the "
            "counterfactual's design decision reduced degradation relative to "
            "the failure mode."
        ),
    )


class MCConfig(BaseModel):
    """Monte Carlo run configuration."""

    n_runs: int = Field(..., description="Number of Monte Carlo samples to run.")
    parameters_to_vary: list[str] = Field(..., description="Dotted paths to Sourced fields to sample.")
    random_seed: int
    parallel: bool = Field(..., description="Whether to execute runs in parallel.")
    output_metrics: list[str] = Field(..., description="Names of metrics to collect from each run.")


class MCSampleResult(BaseModel):
    """One Monte Carlo sample's outcome: which perturbed values were actually
    drawn, and the resulting SOH trajectory and endpoint metrics for that draw.
    """

    sample_index: int
    sampled_values: dict[str, float] = Field(
        ..., description="Dotted path (from MCConfig.parameters_to_vary) -> the value drawn for this sample."
    )
    soh_trajectory: list[float] = Field(
        ..., description="battery_soh from each LongHorizonCheckpoint, one entry per simulated day."
    )
    endpoint_metrics: dict[str, float] = Field(
        ..., description="MCConfig.output_metrics name -> value at the final checkpoint of this sample."
    )


class MCPercentileBand(BaseModel):
    """p05/p50/p95 of `MCSampleResult.soh_trajectory` across all samples, at one day."""

    day_index: int
    p05: float
    p50: float
    p95: float


class MCResult(BaseModel):
    """Output of `mc.run_monte_carlo` (see that module's docstring for the
    sampling methodology and its two explicit caveats).

    The band this carries is uncertainty around a PLACEHOLDER aging-
    coefficient value, not uncertainty around real battery chemistry — see
    `mc.py`'s module docstring. It is also conditional on one fixed
    day-type schedule (the schedule-generating RNG seed is held constant
    across every sample, deliberately, so the band isolates sensitivity to
    aging-coefficient uncertainty alone, not schedule-order variance).
    Sampling schedule-order variance is a separate, deferred Monte Carlo
    mode.

    `soh_band[i].p50` is NOT the same quantity as an unperturbed point
    estimate's SOH at checkpoint `i`, and the gap between them grows with
    horizon (negligible at 90 days, ~6% relative at 20 years for the
    reference Leaf/Guam scenario) — see `mc.py`'s module docstring, "p50
    vs. the point estimate," for why this is an expected Jensen's-
    inequality effect of this model's own Arrhenius/exponential aging
    formulation, not a bug or a sampling artifact.
    """

    config: MCConfig
    samples: list[MCSampleResult]
    soh_band: list[MCPercentileBand] = Field(
        ..., description="Time-indexed p05/p50/p95 SOH band, one entry per simulated day."
    )


class SeamTableRow(BaseModel):
    """One `(vehicle_class, climate_zone, duty_cycle_archetype)` row of the
    Transition-seam precomputed SOH-delta table (docs/seam_reconnaissance.md
    §5) — shape, not spec, still evolving. Keyed on axes Transition's own
    schema needs to gain before it could look anything up (see that memo's
    §3/§7): a `manufacturerProfileKey`-style field on `VehicleClass`, and
    per-route duty-cycle detail (gap 2, still deliberately deferred — v1
    rows use a single fixed `duty_cycle_archetype`, not yet meaningfully
    varying with route).
    """

    vehicle_class: str = Field(
        ...,
        description=(
            "Profile YAML filename stem, e.g. 'blue_bird_vision_electric' — matches "
            "profile_loader.load_all_profiles's own keying convention."
        ),
    )
    climate_zone: str = Field(
        ...,
        description=(
            "A descriptive per-location key, e.g. 'columbus_oh' — deliberately NOT Transition's "
            "ClimateZone.key (an ASHRAE zone code like '5A'), which collides across different real "
            "locations sharing the same zone (both current Transition presets are '5A'). Transition-side "
            "wiring will need its own disambiguation, not decided here."
        ),
    )
    duty_cycle_archetype: str = Field(
        ...,
        description=(
            "v1: always 'default' (the profile's own canonical day_type_mix) — Transition's Route has "
            "no per-route duty-cycle detail yet (seam_reconnaissance.md gap 2), so this axis doesn't "
            "vary meaningfully yet."
        ),
    )
    methodology: str = Field(
        ...,
        description=(
            "'point_estimate' (an unperturbed simulate_long_horizon run) or 'monte_carlo' (p05/p50/p95 "
            "from mc.run_monte_carlo). v1 rows are point_estimate only — do NOT read a point estimate as "
            "a median; see mc.py's own 'p50 vs. the point estimate' note for why they diverge, "
            "especially at longer horizons."
        ),
    )
    monthly_soh_delta_pct: list[float] = Field(
        ...,
        description=(
            "SOH percentage points lost in EACH calendar month since conversion (incremental, not "
            "cumulative) — index 0 is month 1. Transition ticks monthly and applies one delta per tick, "
            "matching this shape directly. Aggregated from Tier-2's daily LongHorizonCheckpoints, not a "
            "native monthly simulation."
        ),
    )
    source_seed: int
    source_duration_years: int
    caveats: list[str] = Field(
        default_factory=list,
        description=(
            "Known limitations that travel with THIS row's specific numbers, not just documented in "
            "ev-thermal-sim's own docs — e.g. calibration-range extrapolations, unvalidated aging "
            "coefficients for this vehicle class. Per this project's own 'the caveat must travel with "
            "the number' discipline (see docs/MILESTONES.md's Monte Carlo p50-vs-point-estimate record)."
        ),
    )
    generated_at: str = Field(
        ..., description="ISO 8601 date this row was generated — the underlying model/coefficients may change."
    )


class SeamTable(BaseModel):
    """The full precomputed Transition-seam SOH-delta table
    (docs/seam_reconnaissance.md §5) — a JSON artifact produced offline by
    this repo, consumed as a pure function on Transition's side. Shape, not
    spec: this schema itself may change once Transition-side wiring
    surfaces real requirements this repo can't anticipate alone.
    """

    table_version: str
    rows: list[SeamTableRow]
