"""Behavioral tests for `ev_thermal.sim`'s orchestration loop.

Reuses the motor/inverter/battery/cooling/vehicle-dynamics fixtures from
their own test modules, matching `test_controller.py`'s pattern, so the
magnitudes here stay consistent with what those modules' own tests exercise.
"""

from __future__ import annotations

import copy
import math

import pytest

from ev_thermal.cooling import step_cooling
from ev_thermal.schema import (
    AuxLoads,
    ChargingProfile,
    Climate,
    Confidence,
    ControlStrategy,
    DTPolicy,
    ManufacturerProfile,
    Scenario,
    Sourced,
    ThermalArchitecture,
    ThermalNetwork,
    UsageProfile,
)
from ev_thermal.sim import (
    COOLANT_NEAR_AMBIENT_THRESHOLD_K,
    EXTENDED_PARKING_SETTLING_S,
    EXTENDED_PARKING_TARGET_DT_S,
    PARKED_WARM_DT_S,
    _choose_target_dt_s,
    _combined_thermal_network,
    _coolant_thermal_nodes,
    advance_one_step,
    coolant_node_name,
    coolant_node_names,
    initialize_sim_state,
    run_scenario,
)
from ev_thermal.thermal import step_thermal_network
from tests.test_battery import _make_battery_spec
from tests.test_controller import _make_vehicle_dynamics
from tests.test_cooling import _make_cooling_loop
from tests.test_inverter import _make_inverter_spec
from tests.test_motor import _make_motor_spec


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="placeholder")


def _make_profile() -> ManufacturerProfile:
    """WARNING: this fixture's battery is fully adiabatic (no
    `resistance_to_ambient_k_per_w`). It is suitable for short-horizon tests
    only. Any long-horizon or thermal-soak test must use
    `tests.test_long_horizon._make_long_horizon_profile()` instead, or it will
    produce unbounded temperature growth (confirmed: 302K -> 2544K over a
    5-year `simulate_long_horizon` run against this fixture as-is, vs. bounded
    289-309K against the real Nissan Leaf profile, which has ambient
    coupling). See CLAUDE.md's Milestone E2c note.
    """
    battery_spec = _make_battery_spec()
    thermal_architecture = ThermalArchitecture(
        cooling_loops=[_make_cooling_loop()],
        thermal_network=ThermalNetwork(
            nodes={"battery_pack": battery_spec.thermal_node}, edges=[], ambient_coupling={}
        ),
        shared_loop=True,
        heat_pump_present=False,
        battery_preconditioning=False,
    )
    aux_loads = AuxLoads(
        hvac_power_w=_sourced(1000.0),
        cabin_heater_type="resistive PTC",
        accessory_load_w=_sourced(200.0),
        dc_dc_converter_efficiency=_sourced(0.9),
    )
    return ManufacturerProfile(
        name="Test EV",
        model_year_range=(2022, 2024),
        vehicle_dynamics=_make_vehicle_dynamics(),
        battery=battery_spec,
        motors=[_make_motor_spec()],
        inverter=_make_inverter_spec(),
        thermal_architecture=thermal_architecture,
        control_strategy=ControlStrategy(
            derate_curve=[(_sourced(313.15), _sourced(1.0)), (_sourced(333.15), _sourced(0.2))],
            pump_control_policy="see controller.py",
            fan_control_policy="see controller.py",
            target_battery_temp_range_k=Sourced[tuple[float, float]](
                value=(288.15, 308.15), confidence=Confidence.LOW, source="placeholder"
            ),
        ),
        aux_loads=aux_loads,
    )


def _make_scenario(profile: ManufacturerProfile, ambient_temp_k: float = 298.15, seed: int = 42) -> Scenario:
    climate = Climate(
        location_name="Test City",
        mean_annual_temp_k=_sourced(ambient_temp_k),
        seasonal_amplitude_k=_sourced(10.0),
        daily_amplitude_k=_sourced(5.0),
        altitude_m=_sourced(100.0),
        solar_irradiance_w_per_m2=_sourced(800.0),
    )
    usage_profile = UsageProfile(
        annual_mileage_km=_sourced(15_000.0),
        mean_trip_km=_sourced(20.0),
        trip_km_std=_sourced(10.0),
        driving_style="normal",
        garage_parked=True,
    )
    charging_profile = ChargingProfile(
        charger_type="Level 2",
        charge_power_kw=_sourced(11.0),
        typical_start_soc=_sourced(0.5),
        typical_end_soc=_sourced(0.8),
        dc_fast_charge_fraction=_sourced(0.1),
        preconditioning_enabled=False,
        dc_charge_power_kw=_sourced(50.0),
    )
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")
    return Scenario(
        name="test-scenario",
        manufacturer_profile=profile,
        climate=climate,
        usage_profile=usage_profile,
        charging_profile=charging_profile,
        duration_years=1,
        dt_policy=dt_policy,
        random_seed=seed,
    )


def test_60s_cruise_converges_with_no_nan_or_inf() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    result = run_scenario(profile, scenario, duration_s=60.0, target_speed_mps=lambda _t: 20.0)

    for value in [
        result.final_state.speed_mps,
        result.final_state.battery_soc,
        *result.final_state.node_temps_k.values(),
        *result.time_series.commanded_torque_nm,
        *result.time_series.speed_mps,
    ]:
        assert math.isfinite(value)

    assert abs(result.final_state.speed_mps - 20.0) < 0.5
    last_torques = result.time_series.commanded_torque_nm[-5:]
    assert max(last_torques) - min(last_torques) < 5.0


def test_energy_balance_within_2_percent() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    state = initialize_sim_state(profile, scenario, scenario.random_seed)

    battery_ocv_output_j = 0.0
    motor_mechanical_work_j = 0.0
    motor_loss_j = 0.0
    inverter_loss_j = 0.0
    battery_loss_j = 0.0
    aux_j = 0.0

    total_duration_s = 60.0
    while state.sim_time_s < total_duration_s:
        record = advance_one_step(profile, scenario, state, 20.0, max_dt_s=total_duration_s - state.sim_time_s)
        dt_s = record.new_state.sim_time_s - state.sim_time_s
        battery_ocv_output_j += record.battery_ocv_power_w * dt_s
        motor_mechanical_work_j += record.motor_mechanical_power_w * dt_s
        motor_loss_j += record.motor_loss_w * dt_s
        inverter_loss_j += record.inverter_loss_w * dt_s
        battery_loss_j += record.battery_loss_w * dt_s
        aux_j += record.aux_power_w * dt_s
        state = record.new_state

    predicted_j = motor_mechanical_work_j + motor_loss_j + inverter_loss_j + battery_loss_j + aux_j
    relative_error = abs(battery_ocv_output_j - predicted_j) / abs(predicted_j)
    assert relative_error < 0.02


def test_determinism_same_seed_gives_same_final_state_and_events() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile, seed=7)

    result_a = run_scenario(profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0)
    result_b = run_scenario(profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0)

    assert result_a.final_state == result_b.final_state
    assert [e.event_type for e in result_a.causal_events] == [e.event_type for e in result_b.causal_events]


def test_fork_replay_from_saved_state_is_identical() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile, seed=99)

    first_leg = run_scenario(profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 18.0)
    saved_state = first_leg.final_state

    continuation_a = run_scenario(
        profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 18.0, initial_state=copy.deepcopy(saved_state)
    )
    continuation_b = run_scenario(
        profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 18.0, initial_state=copy.deepcopy(saved_state)
    )

    assert continuation_a.final_state == continuation_b.final_state
    assert continuation_a.time_series == continuation_b.time_series


def test_hot_ambient_produces_higher_peak_component_temps_than_cool() -> None:
    profile = _make_profile()
    hot_scenario = _make_scenario(profile, ambient_temp_k=313.15)  # 40C
    cool_scenario = _make_scenario(profile, ambient_temp_k=293.15)  # 20C

    hot_result = run_scenario(profile, hot_scenario, duration_s=120.0, target_speed_mps=lambda _t: 20.0)
    cool_result = run_scenario(profile, cool_scenario, duration_s=120.0, target_speed_mps=lambda _t: 20.0)

    for node_name in hot_result.time_series.node_temps_k:
        hot_peak = max(hot_result.time_series.node_temps_k[node_name])
        cool_peak = max(cool_result.time_series.node_temps_k[node_name])
        assert hot_peak > cool_peak


def test_coolant_above_ambient_below_component_temps_after_5_minutes() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    result = run_scenario(profile, scenario, duration_s=300.0, target_speed_mps=lambda _t: 20.0)

    ambient_temp_k = scenario.climate.mean_annual_temp_k.value
    coolant_names = set(coolant_node_names(profile))
    assert len(coolant_names) == 1, "this fixture's single shared loop should produce exactly one coolant node"
    coolant_temp_k = result.final_state.node_temps_k[next(iter(coolant_names))]
    component_temps_k = [
        temp for name, temp in result.final_state.node_temps_k.items() if name not in coolant_names
    ]

    assert coolant_temp_k > ambient_temp_k
    assert coolant_temp_k < max(component_temps_k)


def test_driver_pi_settles_within_30s_to_within_1kmh_at_dt_1s() -> None:
    """Regression guard for sim.py's dt=1.0s driver gains (SIM_DRIVER_KP_NM_PER_MPS/
    SIM_DRIVER_KI_NM_PER_M): a constant-target cruise, well inside every component's
    thermal/current ceiling, must settle and stay settled — this isolates the PI
    loop's own stability from the separate thermal-derate-driven limit-cycling that
    a torque ceiling flapping near a component's max_temp_k can otherwise produce.
    """
    profile = _make_profile()
    scenario = _make_scenario(profile)
    target_mps = 20.0
    one_kmh_mps = 1.0 / 3.6

    result = run_scenario(profile, scenario, duration_s=60.0, target_speed_mps=lambda _t: target_mps)

    for time_s, speed_mps in zip(result.time_series.time_s, result.time_series.speed_mps, strict=True):
        if time_s >= 30.0:
            assert abs(speed_mps - target_mps) < one_kmh_mps


def test_requested_torque_recorded_and_matches_commanded_when_unclamped() -> None:
    """A gentle cruise well under every component's limit should never get
    clamped by controller.py, so the driver's raw request and the commanded
    torque recorded in the time series should match at every step.
    """
    profile = _make_profile()
    scenario = _make_scenario(profile)

    result = run_scenario(profile, scenario, duration_s=10.0, target_speed_mps=lambda _t: 5.0)

    assert len(result.time_series.requested_torque_nm) == len(result.time_series.commanded_torque_nm)
    for requested, commanded in zip(
        result.time_series.requested_torque_nm[1:], result.time_series.commanded_torque_nm[1:], strict=True
    ):
        assert requested == pytest.approx(commanded)


def test_uphill_grade_requires_more_torque_than_flat_at_same_cruise_speed() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    flat = run_scenario(profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0, grade_rad=0.0)
    uphill = run_scenario(
        profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0, grade_rad=math.radians(5.0)
    )

    assert uphill.time_series.requested_torque_nm[-1] > flat.time_series.requested_torque_nm[-1]


def test_downhill_grade_requires_less_torque_than_flat_at_same_cruise_speed() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    flat = run_scenario(profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0, grade_rad=0.0)
    downhill = run_scenario(
        profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0, grade_rad=math.radians(-5.0)
    )

    assert downhill.time_series.requested_torque_nm[-1] < flat.time_series.requested_torque_nm[-1]


def test_higher_pump_aggressiveness_commands_higher_duty_above_thermostat() -> None:
    """Runs advance_one_step (not the full run_scenario loop) with the battery
    already above the loop's thermostat threshold, so the duty ramp is
    actually active — a full-length run_scenario call at these fixtures'
    default ambient never gets components hot enough to cross the
    thermostat, which would make pump_aggressiveness's effect unobservable
    through peak temps alone.
    """
    profile = _make_profile()
    scenario = _make_scenario(profile)
    battery_name = profile.battery.thermal_node.name
    loop_name = profile.thermal_architecture.cooling_loops[0].name
    thermostat_temp_k = profile.thermal_architecture.cooling_loops[0].thermostat_temp_k.value

    state = initialize_sim_state(profile, scenario, scenario.random_seed)
    hot_temps_k = {**state.node_temps_k, battery_name: thermostat_temp_k + 5.0}
    state = state.model_copy(update={"node_temps_k": hot_temps_k})

    mild = advance_one_step(profile, scenario, state, target_speed_mps=20.0, pump_aggressiveness=0.5)
    aggressive = advance_one_step(profile, scenario, state, target_speed_mps=20.0, pump_aggressiveness=2.0)

    assert aggressive.pump_duty[loop_name] > mild.pump_duty[loop_name]


def test_zero_pump_duty_leaves_combined_network_with_no_escape_path() -> None:
    """controller.py's cooling policy never commands a literal pump_duty=0 (it
    floors at MIN_PUMP_DUTY, since real pumps idle rather than stop) — so
    this test bypasses the controller and drives the underlying
    cooling.step_cooling + thermal.step_thermal_network composition directly
    with pump_duty=0, to check the boundary case those functions describe:
    with the radiator path disabled (zero coolant flow -> zero heat
    rejection) and no ambient_coupling on any node (motor/inverter/battery
    ambient coupling is still inert, per CLAUDE.md), the combined system has
    no path to ambient at all, so heat only redistributes among the four
    nodes and every temperature should monotonically rise together.
    """
    profile = _make_profile()
    scenario = _make_scenario(profile)
    cooling_spec = profile.thermal_architecture.cooling_loops[0]
    coolant_name = coolant_node_name(cooling_spec.name)
    ambient_temp_k = scenario.climate.mean_annual_temp_k.value

    temps_k = {
        profile.motors[0].thermal_node.name: profile.motors[0].thermal_node.initial_temp_k,
        profile.inverter.thermal_node.name: profile.inverter.thermal_node.initial_temp_k,
        profile.battery.thermal_node.name: profile.battery.thermal_node.initial_temp_k,
        coolant_name: ambient_temp_k,
    }
    coolant_nodes = _coolant_thermal_nodes([cooling_spec], temps_k)
    network = _combined_thermal_network(profile, coolant_nodes)

    heat_inputs_w = {
        profile.motors[0].thermal_node.name: 500.0,
        profile.inverter.thermal_node.name: 100.0,
        profile.battery.thermal_node.name: 200.0,
    }

    previous_temps_k = dict(temps_k)
    for _ in range(10):
        cooling_result = step_cooling(cooling_spec, previous_temps_k[coolant_name], ambient_temp_k, 20.0, 0.0, 0.0)
        assert cooling_result.heat_rejected_w == 0.0

        step_heat_inputs_w = {**heat_inputs_w, coolant_name: -cooling_result.heat_rejected_w}
        new_temps_k = step_thermal_network(
            network,
            previous_temps_k,
            step_heat_inputs_w,
            ambient_temp_k=ambient_temp_k,
            coolant_temp_k=0.0,
            coolant_flow_l_per_min=0.0,
            dt_s=1.0,
        )
        for node_name in new_temps_k:
            assert new_temps_k[node_name] >= previous_temps_k[node_name]
        previous_temps_k = new_temps_k

    for node_name in previous_temps_k:
        assert previous_temps_k[node_name] > temps_k[node_name]


def test_soh_feedback_lowers_battery_current_ceiling_and_commanded_torque() -> None:
    """Integration check that `SimState.battery_soh` actually threads through
    `advance_one_step` (via `aging.apply_soh_to_battery_spec`) into a real
    effect on the arbitrated torque, not just into `aging.py`'s own unit
    tests. A hot pack plus a hard launch pushes the battery current ceiling
    into binding territory (mirrors test_controller.py's
    test_hot_battery_and_cold_motor_battery_limit_dominates), so a degraded
    pack's higher effective resistance should command less torque than an
    identical-but-fresh pack under the exact same request.
    """
    profile = _make_profile()
    scenario = _make_scenario(profile)
    battery_name = profile.battery.thermal_node.name

    # speed_mps=5.5 -> motor_speed_rad_per_s=150 (this fixture's wheel_radius_m=0.33,
    # gear_ratio=9.0), matching test_controller.py's own battery-limit-binding
    # fixture; +4.667 m/s of target speed error times SIM_DRIVER_KP_NM_PER_MPS=60
    # gives a ~280Nm P-term request (integral term is 0 on a fresh state) -- under
    # accel_limit_nm=310 so the driver's own clamp doesn't saturate it first, but
    # large enough at this speed to bind the hot battery's current ceiling, mirroring
    # test_controller.py's test_hot_battery_and_cold_motor_battery_limit_dominates.
    base_state = initialize_sim_state(profile, scenario, scenario.random_seed)
    base_state = base_state.model_copy(
        update={"node_temps_k": {**base_state.node_temps_k, battery_name: 330.0}, "speed_mps": 5.5}
    )
    fresh_state = base_state.model_copy(update={"battery_soh": 1.0})
    aged_state = base_state.model_copy(update={"battery_soh": 0.6})

    fresh_record = advance_one_step(profile, scenario, fresh_state, target_speed_mps=10.167)
    aged_record = advance_one_step(profile, scenario, aged_state, target_speed_mps=10.167)

    assert aged_record.commanded_torque_nm < fresh_record.commanded_torque_nm < 310.0


def test_extended_parking_tier_recovers_after_settling_even_with_elevated_coolant() -> None:
    """Regression guard for the hysteresis bug the extended-parking tier
    fixes: the pre-existing coolant-near-ambient check alone can pin dt at
    PARKED_WARM_DT_S forever if a steady parasitic heat load keeps coolant
    at an equilibrium offset from ambient that never closes back inside
    COOLANT_NEAR_AMBIENT_THRESHOLD_K. Gating a fourth tier on elapsed parked
    TIME instead means that even with coolant PERMANENTLY elevated, once
    enough parked time has passed, dt recovers to the coarse extended tier.
    """
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")
    ambient_temp_k = 298.15
    stuck_coolant_temp_k = ambient_temp_k + COOLANT_NEAR_AMBIENT_THRESHOLD_K + 1.0

    before_settling = _choose_target_dt_s(
        0.0, 0.0, [stuck_coolant_temp_k], ambient_temp_k, dt_policy, parked_duration_s=0.0
    )
    after_settling = _choose_target_dt_s(
        0.0, 0.0, [stuck_coolant_temp_k], ambient_temp_k, dt_policy, parked_duration_s=EXTENDED_PARKING_SETTLING_S
    )

    assert before_settling == PARKED_WARM_DT_S
    assert after_settling == EXTENDED_PARKING_TARGET_DT_S


def test_prebuilt_network_produces_identical_step_to_freshly_built_one() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    state = initialize_sim_state(profile, scenario, scenario.random_seed)

    coolant_nodes = _coolant_thermal_nodes(profile.thermal_architecture.cooling_loops, state.node_temps_k)
    prebuilt_network = _combined_thermal_network(profile, coolant_nodes)

    fresh_record = advance_one_step(profile, scenario, state, target_speed_mps=20.0)
    cached_record = advance_one_step(profile, scenario, state, target_speed_mps=20.0, network=prebuilt_network)

    assert fresh_record.new_state == cached_record.new_state


def test_record_interval_decimates_time_series_but_not_final_state() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    full = run_scenario(profile, scenario, duration_s=60.0, target_speed_mps=lambda _t: 20.0)
    decimated = run_scenario(
        profile, scenario, duration_s=60.0, target_speed_mps=lambda _t: 20.0, record_interval_s=10.0
    )

    assert decimated.final_state == full.final_state
    assert len(decimated.time_series.time_s) < len(full.time_series.time_s)
    assert decimated.time_series.time_s[0] == full.time_series.time_s[0]
    assert decimated.time_series.time_s[-1] == full.time_series.time_s[-1] == 60.0


def test_long_horizon_determinism_same_seed_gives_same_final_state() -> None:
    """Determinism check for a long-horizon (multi-day, mostly-parked)
    scenario, exercising the aging step and the extended-parking dt tier —
    not the literal multi-year duration the milestone's original scope
    named, deliberately shrunk (short drive + a multi-day parked tail,
    rather than years) to keep this in the default test suite: a true
    multi-year run is still bottlenecked by the battery's own R1*C1 Euler-
    stability cap on dt (see CLAUDE.md's deferred-work note on
    `_stabilize_dt_s`), which the extended-parking tier's larger target dt
    gets clipped down to every step regardless of how long the vehicle has
    been parked -- see this milestone's note in CLAUDE.md on that finding.
    """

    def target_speed_mps(t: float) -> float:
        return 15.0 if t < 60.0 else 0.0

    profile = _make_profile()
    scenario = _make_scenario(profile, seed=13)

    result_a = run_scenario(
        profile, scenario, duration_s=3.0 * 86400.0, target_speed_mps=target_speed_mps, record_interval_s=3600.0
    )
    result_b = run_scenario(
        profile, scenario, duration_s=3.0 * 86400.0, target_speed_mps=target_speed_mps, record_interval_s=3600.0
    )

    assert result_a.final_state == result_b.final_state
    assert result_a.final_state.battery_soh < 1.0
    assert result_a.final_state.parked_duration_s > EXTENDED_PARKING_SETTLING_S
