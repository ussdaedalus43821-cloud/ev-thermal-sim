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
        description="Node name -> conductance to ambient air, in W/K, for nodes exposed to ambient.",
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
    thermal_network: ThermalNetwork
    shared_loop: bool = Field(
        ..., description="True if battery and powertrain (motor/inverter) share one coolant loop."
    )
    heat_pump_present: bool = Field(
        ..., description="Whether cabin heating uses a heat pump vs. resistive heat."
    )
    battery_preconditioning: bool = Field(
        ..., description="Whether the vehicle can pre-heat/pre-cool the battery before a planned fast charge."
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
    copper_loss_w: float = Field(..., description="Resistive winding loss, in watts.")
    iron_loss_w: float = Field(..., description="Core/iron loss, in watts.")
    mechanical_loss_w: float = Field(..., description="Windage/friction loss, in watts.")
    winding_temp_k: float = Field(..., description="Updated winding temperature after this step, in kelvin.")
    applied_torque_nm: float = Field(
        ...,
        description="Torque actually applied after derating, in N*m (may be less in magnitude than requested).",
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
    heat_rejected_w: float = Field(..., description="Heat rejected to ambient air at the radiator, in watts.")
    coolant_mass_flow_kg_per_s: float = Field(..., description="Total loop coolant mass flow, in kg/s.")
    pump_power_w: float = Field(..., description="Pump parasitic electrical power draw, in watts.")
    fan_power_w: float = Field(..., description="Fan parasitic electrical power draw, in watts.")
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
    commanded_pump_duty: float = Field(..., description="Commanded coolant pump duty, 0-1.")
    commanded_fan_duty: float = Field(..., description="Commanded radiator fan duty, 0-1.")
    battery_current_demand_a: float = Field(
        ...,
        description=(
            "Battery pack current implied by commanded_torque_nm, in amps (positive discharge, "
            "negative charge), for battery.step_battery's current_request_a input."
        ),
    )
    causal_events: list[CausalEvent] = Field(default_factory=list)


class MCConfig(BaseModel):
    """Monte Carlo run configuration."""

    n_runs: int = Field(..., description="Number of Monte Carlo samples to run.")
    parameters_to_vary: list[str] = Field(..., description="Dotted paths to Sourced fields to sample.")
    random_seed: int
    parallel: bool = Field(..., description="Whether to execute runs in parallel.")
    output_metrics: list[str] = Field(..., description="Names of metrics to collect from each run.")
