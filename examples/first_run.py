"""A self-contained "hello world" for the simulator: loads the real Nissan Leaf
profile (configs/profiles/nissan_leaf.yaml), runs a short commuter scenario,
and prints what happened.

THROWAWAY DEMO SCRIPT, not a permanent CLI: it exists so we can look at real
numbers. The vehicle profile is real (see docs/research/profiles_v1.md), but
the Scenario built around it (climate, usage/charging profile) is still a
demo placeholder, same TODO(spec-sourcing) discipline as before — see
CLAUDE.md for the running list of what's still a placeholder.

Usage:
    uv run python examples/first_run.py
    uv run python examples/first_run.py --plot
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ev_thermal.profile_loader import PROFILES_DIR, load_profile
from ev_thermal.schema import (
    ChargingProfile,
    Climate,
    Confidence,
    DTPolicy,
    ManufacturerProfile,
    RunResult,
    Scenario,
    Sourced,
    UsageProfile,
)
from ev_thermal.sim import COOLANT_NODE_NAME, run_scenario

RUN_DURATION_S = 600.0
CRUISE_SPEED_MPS = 13.4  # ~30 mph
DRIVER_AGGRESSION = 1.1  # "moderate"


def _sourced(value: float, source: str = "placeholder") -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source=source)


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

    profile = load_profile(PROFILES_DIR / "nissan_leaf.yaml")
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
    print(f"Vehicle: {profile.name}")
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
