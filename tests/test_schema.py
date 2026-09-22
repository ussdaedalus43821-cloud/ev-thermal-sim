"""Round-trip serialization, unit/type validation, and Sourced-wrapper tests for `ev_thermal.schema`.

No physics is exercised here — these tests only confirm the schema shapes hold data
correctly and survive a JSON round-trip, since that round-trip is what fork-safety
(`SimState`) depends on.
"""

from __future__ import annotations

from datetime import UTC
from typing import TypeVar

import pytest
from pydantic import ValidationError

from ev_thermal.schema import (
    AuxLoads,
    BatterySpec,
    CausalEvent,
    ChargingProfile,
    Climate,
    Confidence,
    ControlStrategy,
    CoolingLoop,
    DTPolicy,
    ForkPoint,
    InverterSpec,
    ManufacturerProfile,
    MCConfig,
    MistakeCase,
    MotorSpec,
    Scenario,
    SimState,
    Sourced,
    ThermalArchitecture,
    ThermalNetwork,
    ThermalNode,
    UsageProfile,
    VehicleDynamics,
)

_T = TypeVar("_T")


def sourced(value: _T, confidence: Confidence = Confidence.MEDIUM, source: str = "test-fixture") -> Sourced[_T]:
    return Sourced[_T](value=value, confidence=confidence, source=source)


def make_thermal_node(name: str = "battery_pack") -> ThermalNode:
    return ThermalNode(
        name=name,
        capacitance_j_per_k=sourced(50_000.0),
        resistance_to_coolant_k_per_w=sourced(0.05),
        initial_temp_k=298.15,
        max_temp_k=sourced(333.15),
        derate_temp_k=sourced(318.15),
    )


def make_vehicle_dynamics() -> VehicleDynamics:
    return VehicleDynamics(
        curb_mass_kg=sourced(1800.0),
        drag_coefficient=sourced(0.28),
        frontal_area_m2=sourced(2.3),
        rolling_resistance_coefficient=sourced(0.01),
        wheel_radius_m=sourced(0.33),
        gear_ratio=sourced(9.0),
        driveline_efficiency=sourced(0.95),
        tire_road_friction_coefficient=sourced(0.9),
    )


def make_battery_spec() -> BatterySpec:
    return BatterySpec(
        chemistry="NMC",
        capacity_kwh_nominal=sourced(62.0),
        capacity_kwh_usable=sourced(58.0),
        nominal_voltage_v=sourced(350.0),
        internal_resistance_ohm=sourced(0.05),
        mass_kg=sourced(450.0),
        specific_heat_j_per_kg_k=sourced(900.0),
        max_charge_c_rate=sourced(1.5),
        max_discharge_c_rate=sourced(3.0),
        thermal_node=make_thermal_node("battery_pack"),
        capacity_nominal_ah=sourced(180.0),
        reference_temp_k=sourced(298.15),
        ocv_curve_v=[(sourced(0.0), sourced(300.0)), (sourced(1.0), sourced(395.0))],
        docv_dt_v_per_k=sourced(0.0),
        r0_curve_ohm=[(sourced(0.0), sourced(0.09)), (sourced(1.0), sourced(0.10))],
        r0_arrhenius_activation_temp_k=sourced(3000.0),
        r1_ohm=sourced(0.015),
        c1_farad=sourced(3000.0),
        min_terminal_voltage_v=sourced(290.0),
        max_terminal_voltage_v=sourced(410.0),
        min_operating_temp_k=sourced(253.15),
        cold_derate_threshold_temp_k=sourced(283.15),
        cold_derate_activation_temp_k=sourced(1000.0),
        soc_floor=sourced(0.05),
        soc_floor_full_available_soc=sourced(0.1),
    )


def make_motor_spec() -> MotorSpec:
    return MotorSpec(
        motor_type="PMSM",
        peak_power_kw=sourced(150.0),
        continuous_power_kw=sourced(75.0),
        peak_torque_nm=sourced(310.0),
        efficiency_peak=sourced(0.96),
        mass_kg=sourced(70.0),
        max_winding_temp_k=sourced(453.15),
        thermal_node=make_thermal_node("motor"),
        winding_resistance_ref_ohm=sourced(0.05),
        winding_resistance_ref_temp_k=sourced(298.15),
        torque_constant_nm_per_a=sourced(1.0),
        iron_loss_coefficient_w_per_rad_per_s=sourced(2.0),
        mechanical_loss_coefficient_w_per_rad3_per_s3=sourced(0.0002),
        regen_extra_loss_fraction=sourced(0.15),
    )


def make_inverter_spec() -> InverterSpec:
    return InverterSpec(
        topology="SiC three-phase",
        peak_power_kw=sourced(150.0),
        efficiency_peak=sourced(0.98),
        switching_frequency_hz=sourced(10_000.0),
        max_junction_temp_k=sourced(423.15),
        mass_kg=sourced(15.0),
        thermal_node=make_thermal_node("inverter"),
        peak_current_a=sourced(500.0),
        r_ds_on_ref_ohm=sourced(0.002),
        r_ds_on_ref_temp_k=sourced(298.15),
        r_ds_on_temp_coefficient_per_k=sourced(0.005),
        switching_energy_coefficient_j_per_v=sourced(0.00003),
    )


def make_cooling_loop() -> CoolingLoop:
    return CoolingLoop(
        name="battery_loop",
        coolant_type="50/50 water-glycol",
        loop_volume_l=sourced(6.0),
        pump_max_flow_l_per_min=sourced(20.0),
        pump_power_w=sourced(80.0),
        radiator_ua_w_per_k=sourced(150.0),
        fan_power_w=sourced(120.0),
        chiller_present=True,
        chiller_capacity_w=sourced(3000.0),
        serves=["battery"],
        coolant_specific_heat_j_per_kg_k=sourced(3600.0),
        coolant_density_kg_per_m3=sourced(1050.0),
        thermostat_temp_k=sourced(308.15),
        thermostat_full_open_temp_k=sourced(313.15),
        radiator_frontal_area_m2=sourced(0.25),
        fan_max_air_velocity_mps=sourced(6.0),
    )


def make_thermal_network() -> ThermalNetwork:
    return ThermalNetwork(
        nodes={"battery_pack": make_thermal_node("battery_pack")},
        edges=[],
        ambient_coupling={"battery_pack": sourced(5.0)},
    )


def make_thermal_architecture() -> ThermalArchitecture:
    return ThermalArchitecture(
        cooling_loops=[make_cooling_loop()],
        thermal_network=make_thermal_network(),
        shared_loop=False,
        heat_pump_present=True,
        battery_preconditioning=True,
    )


def make_control_strategy() -> ControlStrategy:
    return ControlStrategy(
        derate_curve=[(sourced(313.15), sourced(1.0)), (sourced(333.15), sourced(0.2))],
        pump_control_policy="proportional above 30C",
        fan_control_policy="on above 35C",
        target_battery_temp_range_k=sourced((288.15, 308.15)),
    )


def make_aux_loads() -> AuxLoads:
    return AuxLoads(
        hvac_power_w=sourced(3000.0),
        cabin_heater_type="heat pump",
        accessory_load_w=sourced(200.0),
        dc_dc_converter_efficiency=sourced(0.93),
    )


def make_manufacturer_profile(name: str = "Test EV", chemistry: str = "NMC") -> ManufacturerProfile:
    battery = make_battery_spec()
    battery = battery.model_copy(update={"chemistry": chemistry})
    return ManufacturerProfile(
        name=name,
        model_year_range=(2022, 2024),
        vehicle_dynamics=make_vehicle_dynamics(),
        battery=battery,
        motors=[make_motor_spec()],
        inverter=make_inverter_spec(),
        thermal_architecture=make_thermal_architecture(),
        control_strategy=make_control_strategy(),
        aux_loads=make_aux_loads(),
    )


def make_climate() -> Climate:
    return Climate(
        location_name="Phoenix, AZ",
        mean_annual_temp_k=sourced(295.0),
        seasonal_amplitude_k=sourced(12.0),
        daily_amplitude_k=sourced(8.0),
        altitude_m=sourced(330.0),
        solar_irradiance_w_per_m2=sourced(950.0),
    )


def make_usage_profile() -> UsageProfile:
    return UsageProfile(
        annual_mileage_km=sourced(18_000.0),
        mean_trip_km=sourced(15.0),
        trip_km_std=sourced(10.0),
        driving_style="normal",
        garage_parked=False,
    )


def make_charging_profile() -> ChargingProfile:
    return ChargingProfile(
        charger_type="DC fast charge",
        charge_power_kw=sourced(100.0),
        typical_start_soc=sourced(0.2),
        typical_end_soc=sourced(0.8),
        dc_fast_charge_fraction=sourced(0.4),
        preconditioning_enabled=False,
    )


def make_dt_policy() -> DTPolicy:
    return DTPolicy(min_dt_s=0.1, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")


def make_scenario(*, with_profile: bool = True) -> Scenario:
    return Scenario(
        name="test scenario",
        manufacturer_profile=make_manufacturer_profile() if with_profile else None,
        climate=make_climate(),
        usage_profile=make_usage_profile(),
        charging_profile=make_charging_profile(),
        duration_years=10,
        dt_policy=make_dt_policy(),
        random_seed=42,
    )


# --- Confidence enum ---


def test_confidence_enum_accepts_known_values() -> None:
    assert Confidence("low") is Confidence.LOW
    assert Confidence("medium") is Confidence.MEDIUM
    assert Confidence("high") is Confidence.HIGH


def test_confidence_enum_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        Confidence("extreme")


# --- Sourced wrapper ---


def test_sourced_round_trip() -> None:
    original = sourced(42.0, confidence=Confidence.HIGH, source="OEM datasheet")
    restored = Sourced[float].model_validate_json(original.model_dump_json())
    assert restored == original


def test_sourced_requires_source() -> None:
    with pytest.raises(ValidationError):
        Sourced(value=1.0, confidence=Confidence.LOW)  # type: ignore[call-arg]


def test_sourced_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        Sourced(value=1.0, confidence="extreme", source="test")


def test_sourced_note_defaults_to_none() -> None:
    assert sourced(1.0).note is None


# --- ThermalNode ---


def test_thermal_node_round_trip() -> None:
    node = make_thermal_node()
    restored = ThermalNode.model_validate_json(node.model_dump_json())
    assert restored == node


def test_thermal_node_rejects_non_numeric_capacitance() -> None:
    with pytest.raises(ValidationError):
        ThermalNode(
            name="battery_pack",
            capacitance_j_per_k=sourced("not-a-number"),
            resistance_to_coolant_k_per_w=sourced(0.05),
            initial_temp_k=298.15,
            max_temp_k=sourced(333.15),
            derate_temp_k=sourced(318.15),
        )


# --- ManufacturerProfile (deep nesting) ---


def test_manufacturer_profile_round_trip() -> None:
    profile = make_manufacturer_profile()
    restored = ManufacturerProfile.model_validate_json(profile.model_dump_json())
    assert restored == profile
    assert restored.battery.chemistry == "NMC"
    assert restored.motors[0].motor_type == "PMSM"


def test_manufacturer_profile_supports_dual_motor() -> None:
    profile = make_manufacturer_profile()
    profile = profile.model_copy(update={"motors": [make_motor_spec(), make_motor_spec()]})
    restored = ManufacturerProfile.model_validate_json(profile.model_dump_json())
    assert len(restored.motors) == 2


# --- Scenario: optional manufacturer_profile ---


def test_scenario_with_profile_round_trips() -> None:
    scenario = make_scenario(with_profile=True)
    restored = Scenario.model_validate_json(scenario.model_dump_json())
    assert restored == scenario
    assert restored.manufacturer_profile is not None


def test_scenario_without_profile_is_valid() -> None:
    scenario = make_scenario(with_profile=False)
    restored = Scenario.model_validate_json(scenario.model_dump_json())
    assert restored.manufacturer_profile is None


# --- MistakeCase: paired failure/fix teaching unit ---


def test_mistake_case_round_trip_and_pairing() -> None:
    failure = make_manufacturer_profile(name="Failure EV", chemistry="NMC")
    fixed = failure.model_copy(
        update={
            "name": "Fixed EV",
            "thermal_architecture": make_thermal_architecture().model_copy(
                update={"battery_preconditioning": True}
            ),
        }
    )
    case = MistakeCase(
        name="No active battery cooling in hot climates",
        failure_mode=(
            "Passive/undersized cooling lets the pack sit near its thermal "
            "limit under repeated DC fast charging."
        ),
        failure_profile=failure,
        counterfactual_profile=fixed,
        scenario=make_scenario(with_profile=False),
        evidence=["https://example.com/teardown-report"],
        lesson="Active thermal management pays for itself over the pack's life in retained capacity.",
        severity="warning",
    )
    restored = MistakeCase.model_validate_json(case.model_dump_json())
    assert restored == case
    assert restored.failure_profile.name != restored.counterfactual_profile.name
    assert restored.scenario.manufacturer_profile is None


# --- SimState: fork-safety round trip ---


def test_sim_state_round_trip_with_rng_state() -> None:
    import numpy as np

    rng = np.random.default_rng(seed=7)
    state = SimState(
        state_id="state-0001",
        scenario_id="scenario-0001",
        sim_time_s=3600.0,
        odometer_km=42.0,
        battery_soc=0.65,
        battery_soh=0.98,
        node_temps_k={"battery_pack": 305.2, "motor": 320.1},
        rng_state=rng.bit_generator.state,
        fork_lineage=["root"],
        step_count=120,
        dt_current_s=1.0,
        speed_mps=22.0,
        battery_v_rc_v=0.4,
        driver_integral_error_state=1.2,
        battery_throughput_ah=15.0,
        event_ids=["evt-0001"],
        battery_voltage_v=350.0,
    )
    restored = SimState.model_validate_json(state.model_dump_json())
    assert restored == state
    assert restored.rng_state["bit_generator"] == "PCG64"


# --- ForkPoint / CausalEvent ---


def test_fork_point_round_trip() -> None:
    from datetime import datetime

    fork = ForkPoint(
        fork_id="fork-0001",
        parent_state_id="state-0001",
        sim_time_s=7200.0,
        description="What if this vehicle always DC fast charges?",
        overrides={"charging_profile.dc_fast_charge_fraction.value": 1.0},
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    restored = ForkPoint.model_validate_json(fork.model_dump_json())
    assert restored == fork


def test_causal_event_round_trip_and_caused_by_chain() -> None:
    upstream = CausalEvent(
        event_id="evt-0001",
        sim_time_s=100.0,
        event_type="FAST_CHARGE_START",
        description="Driver started a DC fast charge at 20% SOC.",
        affected_component="battery",
        severity="info",
    )
    downstream = CausalEvent(
        event_id="evt-0002",
        sim_time_s=340.0,
        event_type="DERATE_TRIGGERED",
        description="Pack temperature crossed the derate threshold during fast charge.",
        caused_by=[upstream.event_id],
        affected_component="battery",
        severity="warning",
    )
    restored = CausalEvent.model_validate_json(downstream.model_dump_json())
    assert restored.caused_by == [upstream.event_id]


# --- MCConfig ---


def test_mc_config_round_trip() -> None:
    config = MCConfig(
        n_runs=500,
        parameters_to_vary=["battery.internal_resistance_ohm", "climate.mean_annual_temp_k"],
        random_seed=123,
        parallel=True,
        output_metrics=["battery_soh_at_end", "max_temp_reached_k"],
    )
    restored = MCConfig.model_validate_json(config.model_dump_json())
    assert restored == config
