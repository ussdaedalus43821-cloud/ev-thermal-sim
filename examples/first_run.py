"""A self-contained "hello world" for the simulator: builds an in-memory
ManufacturerProfile with placeholder values (no YAML loading yet — the
profile YAMLs in configs/profiles/ are still empty stubs), runs a short
commuter scenario, and prints what happened.

THROWAWAY DEMO SCRIPT, not a permanent CLI: it exists so we can look at real
numbers before building the dashboard. Every numeric placeholder here is the
same kind of TODO(spec-sourcing)/TODO(tuning) value used throughout the
component modules' own test fixtures — see CLAUDE.md for the running list of
what's still a placeholder.

Usage:
    uv run python examples/first_run.py
    uv run python examples/first_run.py --plot
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ev_thermal.schema import (
    AuxLoads,
    BatterySpec,
    ChargingProfile,
    Climate,
    Confidence,
    ControlStrategy,
    CoolingLoop,
    DTPolicy,
    InverterSpec,
    ManufacturerProfile,
    MotorSpec,
    RunResult,
    Scenario,
    Sourced,
    ThermalArchitecture,
    ThermalNetwork,
    ThermalNode,
    UsageProfile,
    VehicleDynamics,
)
from ev_thermal.sim import COOLANT_NODE_NAME, run_scenario

RUN_DURATION_S = 600.0
CRUISE_SPEED_MPS = 13.4  # ~30 mph
DRIVER_AGGRESSION = 1.1  # "moderate"


def _sourced(value: float, source: str = "placeholder") -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source=source)


def _thermal_node(name: str, capacitance_j_per_k: float, resistance_to_coolant_k_per_w: float) -> ThermalNode:
    return ThermalNode(
        name=name,
        capacitance_j_per_k=_sourced(capacitance_j_per_k),
        resistance_to_coolant_k_per_w=_sourced(resistance_to_coolant_k_per_w),
        initial_temp_k=298.15,
        max_temp_k=_sourced(333.15 if name == "battery_pack" else 423.15),
        derate_temp_k=_sourced(318.15 if name == "battery_pack" else 398.15),
    )


def build_profile() -> ManufacturerProfile:
    vehicle_dynamics = VehicleDynamics(
        curb_mass_kg=_sourced(1800.0),
        drag_coefficient=_sourced(0.28),
        frontal_area_m2=_sourced(2.3),
        rolling_resistance_coefficient=_sourced(0.01),
        wheel_radius_m=_sourced(0.33),
        gear_ratio=_sourced(9.0),
        driveline_efficiency=_sourced(0.95),
        tire_road_friction_coefficient=_sourced(0.9),
    )

    battery = BatterySpec(
        chemistry="NMC",
        capacity_kwh_nominal=_sourced(62.0),
        capacity_kwh_usable=_sourced(58.0),
        nominal_voltage_v=_sourced(350.0),
        internal_resistance_ohm=_sourced(0.05),
        mass_kg=_sourced(450.0),
        specific_heat_j_per_kg_k=_sourced(900.0),
        max_charge_c_rate=_sourced(1.5),
        max_discharge_c_rate=_sourced(3.0),
        thermal_node=_thermal_node("battery_pack", 400_000.0, 0.02),
        capacity_nominal_ah=_sourced(180.0),
        reference_temp_k=_sourced(298.15),
        ocv_curve_v=[
            (_sourced(0.0), _sourced(300.0)),
            (_sourced(0.1), _sourced(330.0)),
            (_sourced(0.5), _sourced(348.0)),
            (_sourced(0.9), _sourced(365.0)),
            (_sourced(1.0), _sourced(395.0)),
        ],
        docv_dt_v_per_k=_sourced(0.0),
        r0_curve_ohm=[
            (_sourced(0.0), _sourced(0.09)),
            (_sourced(0.5), _sourced(0.05)),
            (_sourced(1.0), _sourced(0.10)),
        ],
        r0_arrhenius_activation_temp_k=_sourced(3000.0),
        r1_ohm=_sourced(0.015),
        c1_farad=_sourced(3000.0),
        min_terminal_voltage_v=_sourced(290.0),
        max_terminal_voltage_v=_sourced(410.0),
        min_operating_temp_k=_sourced(253.15),
        cold_derate_threshold_temp_k=_sourced(283.15),
        cold_derate_activation_temp_k=_sourced(1000.0),
        soc_floor=_sourced(0.05),
        soc_floor_full_available_soc=_sourced(0.1),
    )

    motor = MotorSpec(
        motor_type="PMSM",
        peak_power_kw=_sourced(150.0),
        continuous_power_kw=_sourced(75.0),
        peak_torque_nm=_sourced(310.0),
        efficiency_peak=_sourced(0.96),
        mass_kg=_sourced(70.0),
        max_winding_temp_k=_sourced(453.15),
        thermal_node=_thermal_node("motor", 30_000.0, 0.03),
        winding_resistance_ref_ohm=_sourced(0.05),
        winding_resistance_ref_temp_k=_sourced(298.15),
        torque_constant_nm_per_a=_sourced(1.0),
        iron_loss_coefficient_w_per_rad_per_s=_sourced(0.4),
        mechanical_loss_coefficient_w_per_rad3_per_s3=_sourced(3e-6),
        regen_extra_loss_fraction=_sourced(0.15),
    )

    inverter = InverterSpec(
        topology="SiC three-phase",
        peak_power_kw=_sourced(150.0),
        efficiency_peak=_sourced(0.98),
        switching_frequency_hz=_sourced(10_000.0),
        max_junction_temp_k=_sourced(423.15),
        mass_kg=_sourced(15.0),
        thermal_node=_thermal_node("inverter", 3_000.0, 0.05),
        peak_current_a=_sourced(500.0),
        r_ds_on_ref_ohm=_sourced(0.002),
        r_ds_on_ref_temp_k=_sourced(298.15),
        r_ds_on_temp_coefficient_per_k=_sourced(0.005),
        switching_energy_coefficient_j_per_v=_sourced(0.00003),
    )

    cooling_loop = CoolingLoop(
        name="powertrain_loop",
        coolant_type="50/50 water-glycol",
        loop_volume_l=_sourced(6.0),
        pump_max_flow_l_per_min=_sourced(20.0),
        pump_power_w=_sourced(80.0),
        radiator_ua_w_per_k=_sourced(400.0),
        fan_power_w=_sourced(150.0),
        chiller_present=False,
        serves=["motor", "inverter", "battery_pack"],
        coolant_specific_heat_j_per_kg_k=_sourced(3600.0),
        coolant_density_kg_per_m3=_sourced(1050.0),
        thermostat_temp_k=_sourced(308.15),
        thermostat_full_open_temp_k=_sourced(313.15),
        radiator_frontal_area_m2=_sourced(0.25),
        fan_max_air_velocity_mps=_sourced(6.0),
    )

    thermal_architecture = ThermalArchitecture(
        cooling_loops=[cooling_loop],
        thermal_network=ThermalNetwork(
            nodes={"battery_pack": battery.thermal_node}, edges=[], ambient_coupling={}
        ),
        shared_loop=True,
        heat_pump_present=False,
        battery_preconditioning=False,
    )

    control_strategy = ControlStrategy(
        derate_curve=[(_sourced(313.15), _sourced(1.0)), (_sourced(333.15), _sourced(0.2))],
        pump_control_policy="see controller.py",
        fan_control_policy="see controller.py",
        target_battery_temp_range_k=Sourced[tuple[float, float]](
            value=(288.15, 308.15), confidence=Confidence.LOW, source="placeholder"
        ),
    )

    aux_loads = AuxLoads(
        hvac_power_w=_sourced(1000.0),
        cabin_heater_type="resistive PTC",
        accessory_load_w=_sourced(200.0),
        dc_dc_converter_efficiency=_sourced(0.9),
    )

    return ManufacturerProfile(
        name="Demo EV",
        model_year_range=(2024, 2024),
        vehicle_dynamics=vehicle_dynamics,
        battery=battery,
        motors=[motor],
        inverter=inverter,
        thermal_architecture=thermal_architecture,
        control_strategy=control_strategy,
        aux_loads=aux_loads,
    )


def build_scenario(profile: ManufacturerProfile) -> Scenario:
    climate = Climate(
        location_name="Demo City",
        mean_annual_temp_k=_sourced(298.15),  # 25C
        seasonal_amplitude_k=_sourced(10.0),
        daily_amplitude_k=_sourced(5.0),
        altitude_m=_sourced(100.0),
        solar_irradiance_w_per_m2=_sourced(800.0),
    )
    usage_profile = UsageProfile(
        annual_mileage_km=_sourced(15_000.0),
        mean_trip_km=_sourced(8.05),  # 5 miles
        trip_km_std=_sourced(3.0),
        driving_style="normal",
        garage_parked=True,
    )
    charging_profile = ChargingProfile(
        charger_type="Level 2",
        charge_power_kw=_sourced(7.2),
        typical_start_soc=_sourced(0.5),
        typical_end_soc=_sourced(0.8),
        dc_fast_charge_fraction=_sourced(0.05),
        preconditioning_enabled=False,
    )
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")

    return Scenario(
        name="commuter-demo",
        manufacturer_profile=profile,
        climate=climate,
        usage_profile=usage_profile,
        charging_profile=charging_profile,
        duration_years=1,
        dt_policy=dt_policy,
        random_seed=42,
    )


def target_speed_mps(t: float) -> float:
    """A little stop-and-go commute segment, not a real drive-cycle model
    (that doesn't exist yet — see CLAUDE.md's driver.py scope note): ramp up
    to cruise, a red-light stop partway through (shows regen braking), then
    back to cruise.
    """
    if t < 30.0:
        return CRUISE_SPEED_MPS * (t / 30.0)
    if 200.0 <= t < 220.0:
        return max(0.0, CRUISE_SPEED_MPS * (1.0 - (t - 200.0) / 20.0))
    if 220.0 <= t < 240.0:
        return 0.0
    if 240.0 <= t < 260.0:
        return CRUISE_SPEED_MPS * ((t - 240.0) / 20.0)
    return CRUISE_SPEED_MPS


def k_to_c(temp_k: float) -> float:
    return temp_k - 273.15


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plot", action="store_true", help="Save a matplotlib PNG of the time series.")
    args = parser.parse_args()

    profile = build_profile()
    scenario = build_scenario(profile)
    motor_name = profile.motors[0].thermal_node.name
    inverter_name = profile.inverter.thermal_node.name
    battery_name = profile.battery.thermal_node.name

    result = run_scenario(
        profile,
        scenario,
        duration_s=RUN_DURATION_S,
        target_speed_mps=target_speed_mps,
        driver_aggression=DRIVER_AGGRESSION,
    )

    ts = result.time_series
    print(
        f"{'t(s)':>6} {'v(m/s)':>7} {'SOC(%)':>7} {'motor(C)':>9} {'batt(C)':>8} "
        f"{'coolant(C)':>10} {'pump':>5} {'torque(Nm)':>11}  limits"
    )
    row_stride = max(1, len(ts.time_s) // 40)
    for i in range(0, len(ts.time_s), row_stride):
        limits = ",".join(ts.active_limit_event_types[i]) or "-"
        print(
            f"{ts.time_s[i]:6.0f} {ts.speed_mps[i]:7.2f} {ts.soc[i] * 100:7.2f} "
            f"{k_to_c(ts.node_temps_k[motor_name][i]):9.1f} "
            f"{k_to_c(ts.node_temps_k[battery_name][i]):8.1f} "
            f"{k_to_c(ts.node_temps_k[COOLANT_NODE_NAME][i]):10.1f} "
            f"{ts.pump_duty[i]:5.2f} {ts.commanded_torque_nm[i]:11.1f}  {limits}"
        )

    derated_s = sum(
        ts.time_s[i] - ts.time_s[i - 1] for i in range(1, len(ts.time_s)) if ts.active_limit_event_types[i]
    )
    energy_totals = result.energy_totals

    print()
    print("--- summary ---")
    print(f"Final SOC:              {result.final_state.battery_soc * 100:.2f}%")
    print(f"Energy consumed:        {energy_totals.consumed_j / 3600:.1f} Wh")
    print(f"Energy regen-recovered: {energy_totals.regen_recovered_j / 3600:.1f} Wh")
    print(f"Energy rejected (rad.): {energy_totals.radiator_rejected_j / 3600:.1f} Wh")
    print(f"Aux energy draw:        {energy_totals.aux_j / 3600:.1f} Wh")
    print(f"Peak motor temp:        {k_to_c(max(ts.node_temps_k[motor_name])):.1f} C")
    print(f"Peak inverter temp:     {k_to_c(max(ts.node_temps_k[inverter_name])):.1f} C")
    print(f"Peak battery temp:      {k_to_c(max(ts.node_temps_k[battery_name])):.1f} C")
    print(f"Peak coolant temp:      {k_to_c(max(ts.node_temps_k[COOLANT_NODE_NAME])):.1f} C")
    print(f"Total time derated:     {derated_s:.0f}s of {ts.time_s[-1]:.0f}s")

    if args.plot:
        save_plot(result, motor_name, inverter_name, battery_name)


def save_plot(result: RunResult, motor_name: str, inverter_name: str, battery_name: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ts = result.time_series
    fig, axes = plt.subplots(4, 1, figsize=(9, 11), sharex=True)

    axes[0].plot(ts.time_s, ts.speed_mps, label="speed (m/s)")
    axes[0].plot(ts.time_s, ts.commanded_torque_nm, label="torque (Nm)", alpha=0.6)
    axes[0].set_ylabel("speed / torque")
    axes[0].legend(loc="upper right")

    axes[1].plot(ts.time_s, [soc * 100 for soc in ts.soc])
    axes[1].set_ylabel("SOC (%)")

    temp_series = [
        (motor_name, "motor"),
        (inverter_name, "inverter"),
        (battery_name, "battery"),
        (COOLANT_NODE_NAME, "coolant"),
    ]
    for name, label in temp_series:
        axes[2].plot(ts.time_s, [k_to_c(t) for t in ts.node_temps_k[name]], label=label)
    axes[2].set_ylabel("temp (C)")
    axes[2].legend(loc="upper left")

    axes[3].plot(ts.time_s, ts.pump_duty, label="pump duty")
    axes[3].plot(ts.time_s, ts.fan_duty, label="fan duty")
    axes[3].set_ylabel("duty")
    axes[3].set_xlabel("time (s)")
    axes[3].legend(loc="upper right")

    fig.suptitle("EV Thermal Simulator — first_run demo")
    fig.tight_layout()

    output_path = Path(__file__).resolve().parent / "first_run.png"
    fig.savefig(output_path, dpi=150)
    print(f"\nSaved plot to {output_path}")


if __name__ == "__main__":
    main()
