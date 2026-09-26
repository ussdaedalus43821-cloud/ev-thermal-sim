"""Tests for Milestone E2b/E2c/E2e (`ev_thermal.long_horizon`): Tier-1
representative-trip characterization (the ambient sinusoid, the ambient-bucket
grid, waypoint interpolation, phase-aging aggregation, `characterize_trip`),
Tier-2 long-horizon extrapolation (day-type shuffling, parked-period
relaxation, trip-start chaining, ambient-bucket interpolation,
`simulate_long_horizon`), and fast-charge characterization (`characterize_
fast_charge_session`, `_fast_charge_phases`) -- all against the shared
`tests.test_sim` fixture profile, except where noted.
"""

from __future__ import annotations

import math
from typing import TypeVar

import numpy as np
import pytest

from ev_thermal.aging import compute_aging_step
from ev_thermal.long_horizon import (
    ANNUAL_PEAK_DAY_OF_YEAR,
    DIURNAL_PEAK_HOUR,
    FAST_CHARGE_SESSION_DURATION_S,
    FAST_CHARGE_SOC_KNEE,
    _archetype_assumed_start_temp_k,
    _fast_charge_phases,
    _interpolate_characterization,
    _parked_soc_trajectory,
    _phase_aging,
    _pump_soc_decrement,
    _soak_patched_temps_k,
    _speed_from_waypoints,
    ambient_bucket_temps_k,
    ambient_temp_k,
    apply_trip_start_offset,
    characterize_all_trips,
    characterize_fast_charge_session,
    characterize_trip,
    parked_calendar_aging,
    relax_parked_temp_k,
    shuffle_day_type_schedule,
    simulate_long_horizon,
)
from ev_thermal.schema import AnnualDayTypeMix, Confidence, ManufacturerProfile, Sourced, TripType
from ev_thermal.sim import coolant_node_name
from tests.test_cooling import _make_cooling_loop
from tests.test_sim import _make_profile, _make_scenario

_T = TypeVar("_T")


def _sourced(value: _T) -> Sourced[_T]:
    return Sourced[_T](value=value, confidence=Confidence.LOW, source="test-fixture")


def _make_long_horizon_profile() -> ManufacturerProfile:
    """`_make_profile()` with a real ambient-coupling path added to the battery
    node -- `_make_profile()`'s own battery is fully adiabatic (no
    `resistance_to_ambient_k_per_w`), which is fine for short scenario tests but
    means a multi-year `simulate_long_horizon` run has no heat-rejection path at
    all: repeated per-trip self-heating with nowhere to go compounds without
    bound (confirmed by direct testing -- this is correct behavior for a
    genuinely adiabatic node, not a Tier-2 bug, but not a useful test fixture
    for long-horizon behavior). tau = 0.25 K/W * 400,000 J/K = 100,000s (~28h),
    same order of magnitude as the real Leaf profile's placeholder.
    """
    profile = _make_profile()
    battery_node = profile.battery.thermal_node.model_copy(
        update={"resistance_to_ambient_k_per_w": _sourced(0.25)}
    )
    battery = profile.battery.model_copy(update={"thermal_node": battery_node})
    return profile.model_copy(update={"battery": battery})

# --- ambient_temp_k ---


def test_ambient_temp_k_peaks_at_annual_and_diurnal_peak() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    climate = scenario.climate
    result = ambient_temp_k(climate, ANNUAL_PEAK_DAY_OF_YEAR, DIURNAL_PEAK_HOUR)
    expected = climate.mean_annual_temp_k.value + climate.seasonal_amplitude_k.value + climate.daily_amplitude_k.value
    assert result == pytest.approx(expected)


def test_ambient_temp_k_troughs_half_a_year_and_half_a_day_later() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    climate = scenario.climate
    result = ambient_temp_k(climate, ANNUAL_PEAK_DAY_OF_YEAR + 365.25 / 2.0, DIURNAL_PEAK_HOUR + 12.0)
    expected = climate.mean_annual_temp_k.value - climate.seasonal_amplitude_k.value - climate.daily_amplitude_k.value
    assert result == pytest.approx(expected)


def test_ambient_temp_k_ignores_altitude_and_solar_irradiance() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    baseline = ambient_temp_k(scenario.climate, 50.0, 10.0)
    perturbed_climate = scenario.climate.model_copy(
        update={
            "altitude_m": scenario.climate.altitude_m.model_copy(update={"value": 5000.0}),
            "solar_irradiance_w_per_m2": scenario.climate.solar_irradiance_w_per_m2.model_copy(
                update={"value": 1.0}
            ),
        }
    )
    assert ambient_temp_k(perturbed_climate, 50.0, 10.0) == pytest.approx(baseline)


# --- ambient_bucket_temps_k ---


def test_ambient_bucket_temps_k_evenly_spans_climate_range() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    climate = scenario.climate
    buckets = ambient_bucket_temps_k(climate, n_buckets=4)
    total_swing = climate.seasonal_amplitude_k.value + climate.daily_amplitude_k.value
    assert buckets[0] == pytest.approx(climate.mean_annual_temp_k.value - total_swing)
    assert buckets[-1] == pytest.approx(climate.mean_annual_temp_k.value + total_swing)
    assert len(buckets) == 4
    gaps = [buckets[i + 1] - buckets[i] for i in range(len(buckets) - 1)]
    assert gaps == pytest.approx([gaps[0]] * len(gaps))


def test_ambient_bucket_temps_k_single_bucket_is_the_mean() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    buckets = ambient_bucket_temps_k(scenario.climate, n_buckets=1)
    assert buckets == [scenario.climate.mean_annual_temp_k.value]


# --- _speed_from_waypoints ---


def test_speed_from_waypoints_interpolates_linearly_and_clamps() -> None:
    speed_fn = _speed_from_waypoints([(0.0, 0.0), (10.0, 10.0), (20.0, 10.0), (30.0, 0.0)])
    assert speed_fn(-5.0) == pytest.approx(0.0)
    assert speed_fn(5.0) == pytest.approx(5.0)
    assert speed_fn(15.0) == pytest.approx(10.0)
    assert speed_fn(25.0) == pytest.approx(5.0)
    assert speed_fn(35.0) == pytest.approx(0.0)


# --- _phase_aging ---


def test_phase_aging_matches_hand_computed_time_weighted_average() -> None:
    phase = _phase_aging(
        phase_name="test",
        time_s=[0.0, 10.0, 20.0],
        cell_temp_k=[300.0, 310.0, 290.0],
        soc=[0.5, 0.5, 0.5],
        current_a=[10.0, 20.0, 0.0],
        capacity_ah=100.0,
        start_s=0.0,
        end_s=20.0,
    )
    assert phase.duration_s == pytest.approx(20.0)
    assert phase.mean_cell_temp_k == pytest.approx(305.0)
    assert phase.peak_cell_temp_k == pytest.approx(310.0)
    assert phase.mean_soc == pytest.approx(0.5)
    assert phase.mean_c_rate == pytest.approx(0.15)
    assert phase.throughput_ah == pytest.approx(300.0 / 3600.0)


def test_phase_aging_raises_on_empty_window() -> None:
    with pytest.raises(ValueError, match="no samples"):
        _phase_aging(
            phase_name="empty",
            time_s=[0.0, 10.0],
            cell_temp_k=[300.0, 300.0],
            soc=[0.5, 0.5],
            current_a=[0.0, 0.0],
            capacity_ah=100.0,
            start_s=50.0,
            end_s=60.0,
        )


# --- characterize_trip ---


def test_characterize_trip_cold_start_commute_has_expected_phases_and_cold_initial_pack() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile, ambient_temp_k=283.15)

    characterization = characterize_trip(profile, scenario, TripType.COLD_START_COMMUTE, 283.15)

    assert characterization.trip_type is TripType.COLD_START_COMMUTE
    assert characterization.ambient_bucket_temp_k == pytest.approx(283.15)
    assert [phase.phase_name for phase in characterization.phases] == [
        "warm_up",
        "cruise_and_stops",
        "final_decel",
    ]
    assert sum(phase.duration_s for phase in characterization.phases) == pytest.approx(1200.0)
    assert characterization.odometer_km_delta > 0.0
    assert 0.0 <= characterization.final_soc <= 1.0
    for phase in characterization.phases:
        assert phase.throughput_ah >= 0.0
        assert phase.mean_c_rate >= 0.0


def test_characterize_trip_hot_soak_starts_warmer_than_cold_start_same_ambient() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile, ambient_temp_k=298.15)

    cold = characterize_trip(profile, scenario, TripType.COLD_START_COMMUTE, 298.15)
    hot = characterize_trip(profile, scenario, TripType.HOT_SOAK_COMMUTE, 298.15)

    # Same drive, same climate, same vehicle -- only the initial pack temperature
    # differs, so the warm_up phase's mean cell temp must reflect that.
    assert hot.phases[0].mean_cell_temp_k > cold.phases[0].mean_cell_temp_k


def test_soak_patched_temps_k_patches_battery_and_its_serving_coolant_loop() -> None:
    loop = _make_cooling_loop()  # serves battery_pack, motor, inverter
    base_temps_k = {
        "battery_pack": 298.15,
        "motor": 298.15,
        "inverter": 298.15,
        coolant_node_name(loop.name): 298.15,
    }
    patched = _soak_patched_temps_k(base_temps_k, "battery_pack", 317.15, [loop])

    assert patched["battery_pack"] == 317.15
    assert patched[coolant_node_name(loop.name)] == 317.15
    # Motor/inverter are deliberately untouched -- same precedent as
    # apply_trip_start_offset (Tier-2 doesn't track them between trips).
    assert patched["motor"] == 298.15
    assert patched["inverter"] == 298.15


def test_soak_patched_temps_k_ignores_a_loop_that_does_not_serve_the_battery() -> None:
    loop = _make_cooling_loop().model_copy(update={"name": "motor_only_loop", "serves": ["motor"]})
    base_temps_k = {"battery_pack": 298.15, "motor": 298.15, coolant_node_name(loop.name): 298.15}
    patched = _soak_patched_temps_k(base_temps_k, "battery_pack", 317.15, [loop])

    assert patched["battery_pack"] == 317.15
    assert patched[coolant_node_name(loop.name)] == 298.15


def test_hot_soak_coolant_patch_lets_chiller_engage_where_battery_only_patch_would_not() -> None:
    """Direct regression check for `_soak_patched_temps_k`'s actual effect
    (checking `mean_cell_temp_k > ambient` alone is NOT a valid regression
    test here -- verified directly: it stays true even with only the
    battery patched, since that alone already starts the cell above
    ambient, regardless of the coolant node's own temperature). What
    actually changed is whether a coolant-GATED mechanism -- the chiller's
    second-stage duty -- can see the soak condition at all. Compares two
    otherwise-identical initial states built from the same base: one with
    only the battery patched (this function's pre-fix behavior), one with
    the battery AND its serving coolant loop patched (via the real
    `_soak_patched_temps_k`, this fix) -- the chiller must engage in the
    latter and stay off in the former.
    """
    from ev_thermal.sim import initialize_sim_state, run_scenario
    from tests.test_cooling import _make_chiller_loop

    profile = _make_profile()
    chiller_loop = _make_chiller_loop().model_copy(update={"serves": ["battery_pack", "motor", "inverter"]})
    new_thermal_architecture = profile.thermal_architecture.model_copy(update={"cooling_loops": [chiller_loop]})
    profile = profile.model_copy(update={"thermal_architecture": new_thermal_architecture})
    ambient_k = chiller_loop.thermostat_full_open_temp_k.value - 10.0
    hot_pack_temp_k = chiller_loop.thermostat_full_open_temp_k.value + 10.0
    scenario = _make_scenario(profile, ambient_temp_k=ambient_k)
    base_state = initialize_sim_state(profile, scenario, scenario.random_seed)

    battery_only_temps_k = dict(base_state.node_temps_k, battery_pack=hot_pack_temp_k)
    battery_only_state = base_state.model_copy(update={"node_temps_k": battery_only_temps_k})
    battery_and_coolant_temps_k = _soak_patched_temps_k(
        base_state.node_temps_k, "battery_pack", hot_pack_temp_k, [chiller_loop]
    )
    battery_and_coolant_state = base_state.model_copy(update={"node_temps_k": battery_and_coolant_temps_k})

    battery_only_result = run_scenario(
        profile, scenario, duration_s=300.0, target_speed_mps=lambda _t: 10.0, initial_state=battery_only_state
    )
    both_result = run_scenario(
        profile, scenario, duration_s=300.0, target_speed_mps=lambda _t: 10.0, initial_state=battery_and_coolant_state
    )

    assert max(step[chiller_loop.name] for step in battery_only_result.time_series.chiller_duty) == 0.0
    assert max(step[chiller_loop.name] for step in both_result.time_series.chiller_duty) > 0.0


def test_characterize_trip_highway_has_three_phases() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    characterization = characterize_trip(profile, scenario, TripType.HIGHWAY, 298.15)
    assert [phase.phase_name for phase in characterization.phases] == ["accel", "cruise", "decel"]
    assert sum(phase.duration_s for phase in characterization.phases) == pytest.approx(1800.0)


def test_characterize_trip_city_stop_and_go_is_a_single_phase() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    characterization = characterize_trip(profile, scenario, TripType.CITY_STOP_AND_GO, 298.15)
    assert [phase.phase_name for phase in characterization.phases] == ["stop_and_go"]
    assert characterization.phases[0].duration_s == pytest.approx(1500.0)


def test_characterize_trip_fast_charge_session_raises_not_implemented() -> None:
    # Milestone E2e: FAST_CHARGE_SESSION is implemented, but via
    # characterize_fast_charge_session, not characterize_trip -- calling the
    # wrong function for this TripType must still raise, with a message
    # pointing at the right one.
    profile = _make_profile()
    scenario = _make_scenario(profile)
    with pytest.raises(NotImplementedError, match="characterize_fast_charge_session"):
        characterize_trip(profile, scenario, TripType.FAST_CHARGE_SESSION, 298.15)


# --- Milestone E2c: Tier-2 long-horizon extrapolation ---


def _make_day_type_mix() -> list[AnnualDayTypeMix]:
    return [
        AnnualDayTypeMix(trip_type=TripType.COLD_START_COMMUTE, occurrences_per_year=_sourced(60)),
        AnnualDayTypeMix(trip_type=TripType.HOT_SOAK_COMMUTE, occurrences_per_year=_sourced(60)),
        AnnualDayTypeMix(trip_type=TripType.HIGHWAY, occurrences_per_year=_sourced(20)),
        AnnualDayTypeMix(trip_type=TripType.CITY_STOP_AND_GO, occurrences_per_year=_sourced(100)),
    ]


def test_shuffle_day_type_schedule_produces_exact_multiset_per_year() -> None:
    mix = _make_day_type_mix()
    schedule = shuffle_day_type_schedule(mix, n_days=365, rng=np.random.default_rng(1))
    assert len(schedule) == 365
    assert schedule.count(TripType.COLD_START_COMMUTE) == 60
    assert schedule.count(TripType.HOT_SOAK_COMMUTE) == 60
    assert schedule.count(TripType.HIGHWAY) == 20
    assert schedule.count(TripType.CITY_STOP_AND_GO) == 100
    assert schedule.count(None) == 365 - (60 + 60 + 20 + 100)


def test_shuffle_day_type_schedule_repeats_the_exact_mix_every_year() -> None:
    mix = _make_day_type_mix()
    schedule = shuffle_day_type_schedule(mix, n_days=365 * 3, rng=np.random.default_rng(1))
    assert len(schedule) == 365 * 3
    for year in range(3):
        year_slice = schedule[year * 365 : (year + 1) * 365]
        assert year_slice.count(TripType.HIGHWAY) == 20


def test_shuffle_day_type_schedule_deterministic_under_fixed_seed() -> None:
    mix = _make_day_type_mix()
    a = shuffle_day_type_schedule(mix, n_days=365, rng=np.random.default_rng(7))
    b = shuffle_day_type_schedule(mix, n_days=365, rng=np.random.default_rng(7))
    assert a == b


def test_shuffle_day_type_schedule_raises_if_occurrences_exceed_days_per_year() -> None:
    mix = [AnnualDayTypeMix(trip_type=TripType.HIGHWAY, occurrences_per_year=_sourced(400))]
    with pytest.raises(ValueError, match="more than 365"):
        shuffle_day_type_schedule(mix, n_days=365, rng=np.random.default_rng(1))


def test_relax_parked_temp_k_matches_hand_computed_single_step() -> None:
    samples = relax_parked_temp_k(
        start_temp_k=310.0, tau_s=1000.0, ambient_fn=lambda _t: 290.0,
        start_t_s=0.0, duration_s=500.0, step_s=500.0,
    )
    assert len(samples) == 2
    expected = 290.0 + (310.0 - 290.0) * math.exp(-500.0 / 1000.0)
    assert samples[1][1] == pytest.approx(expected)


def test_relax_parked_temp_k_holds_constant_when_tau_is_none() -> None:
    samples = relax_parked_temp_k(
        start_temp_k=310.0, tau_s=None, ambient_fn=lambda _t: 250.0,
        start_t_s=0.0, duration_s=86400.0, step_s=3600.0,
    )
    assert all(temp == pytest.approx(310.0) for _t, temp in samples)


def test_relax_parked_temp_k_converges_toward_constant_ambient_over_many_time_constants() -> None:
    samples = relax_parked_temp_k(
        start_temp_k=330.0, tau_s=3600.0, ambient_fn=lambda _t: 290.0,
        start_t_s=0.0, duration_s=3600.0 * 20, step_s=3600.0,
    )
    assert samples[-1][1] == pytest.approx(290.0, abs=0.1)


def test_relax_parked_temp_k_zero_duration_returns_only_start_sample() -> None:
    samples = relax_parked_temp_k(
        start_temp_k=300.0, tau_s=1000.0, ambient_fn=lambda _t: 290.0,
        start_t_s=0.0, duration_s=0.0,
    )
    assert samples == [(0.0, 300.0)]


def test_relax_parked_temp_k_below_thermostat_reproduces_ambient_only_behavior() -> None:
    """duty=0 below thermostat_temp_k -- the coolant path contributes nothing,
    so this must be byte-identical to the pre-Q-0020 ambient-only call.
    """
    baseline = relax_parked_temp_k(
        start_temp_k=300.0, tau_s=1000.0, ambient_fn=lambda _t: 290.0, start_t_s=0.0, duration_s=1000.0, step_s=500.0
    )
    with_coolant = relax_parked_temp_k(
        start_temp_k=300.0,
        tau_s=1000.0,
        ambient_fn=lambda _t: 290.0,
        start_t_s=0.0,
        duration_s=1000.0,
        step_s=500.0,
        capacitance_j_per_k=1000.0,
        coolant_resistance_k_per_w=0.02,
        thermostat_temp_k=320.0,  # above start_temp_k -- duty stays 0 throughout
        thermostat_full_open_temp_k=330.0,
    )
    assert with_coolant == baseline


def test_relax_parked_temp_k_above_thermostat_relaxes_faster_with_coolant_path() -> None:
    """Above thermostat_temp_k, the coolant path adds a parallel conductance,
    so the pack must relax toward ambient strictly faster than the
    ambient-only path alone -- confirms the mechanism actually engages, not
    just that it doesn't break the no-coolant case.
    """
    ambient_only = relax_parked_temp_k(
        start_temp_k=330.0,
        tau_s=100_000.0,
        ambient_fn=lambda _t: 290.0,
        start_t_s=0.0,
        duration_s=3600.0,
        step_s=3600.0,
    )
    with_coolant = relax_parked_temp_k(
        start_temp_k=330.0,
        tau_s=100_000.0,
        ambient_fn=lambda _t: 290.0,
        start_t_s=0.0,
        duration_s=3600.0,
        step_s=3600.0,
        capacitance_j_per_k=270_000.0,
        coolant_resistance_k_per_w=0.02,
        thermostat_temp_k=308.15,
        thermostat_full_open_temp_k=313.15,  # start_temp_k=330 is well above this -- duty=1.0 throughout
    )
    assert with_coolant[-1][1] < ambient_only[-1][1]
    ambient_conductance_w_per_k = 270_000.0 / 100_000.0  # capacitance / tau_ambient == 1/R_ambient
    coolant_conductance_w_per_k = 1.0 / 0.02  # duty=1.0 / coolant_resistance
    effective_tau_s = 270_000.0 / (ambient_conductance_w_per_k + coolant_conductance_w_per_k)
    expected = 290.0 + (330.0 - 290.0) * math.exp(-3600.0 / effective_tau_s)
    assert with_coolant[-1][1] == pytest.approx(expected)


def test_relax_parked_temp_k_hand_computed_single_step_with_full_duty_coolant_path() -> None:
    """duty=1.0 (start_temp_k >= thermostat_full_open_temp_k): the effective
    tau is capacitance / (1/tau_ambient + 1/coolant_resistance) -- confirms
    the parallel-resistance combination against a hand computation, not just
    a directional check.
    """
    capacitance_j_per_k = 1000.0
    tau_ambient_s = 1000.0  # implies R_ambient = tau_ambient_s / capacitance = 1.0 K/W
    coolant_resistance_k_per_w = 0.5
    expected_effective_tau_s = capacitance_j_per_k / (1.0 / 1.0 + 1.0 / coolant_resistance_k_per_w)

    samples = relax_parked_temp_k(
        start_temp_k=310.0, tau_s=tau_ambient_s, ambient_fn=lambda _t: 290.0,
        start_t_s=0.0, duration_s=100.0, step_s=100.0,
        capacitance_j_per_k=capacitance_j_per_k,
        coolant_resistance_k_per_w=coolant_resistance_k_per_w,
        thermostat_temp_k=300.0, thermostat_full_open_temp_k=305.0,
    )
    expected = 290.0 + (310.0 - 290.0) * math.exp(-100.0 / expected_effective_tau_s)
    assert samples[1][1] == pytest.approx(expected)


# --- Q-0021: chiller-aware relax_parked_temp_k ---


def test_relax_parked_temp_k_chiller_absent_converges_to_ambient_not_below() -> None:
    samples = relax_parked_temp_k(
        start_temp_k=330.0,
        tau_s=None,
        ambient_fn=lambda _t: 313.15,
        start_t_s=0.0,
        duration_s=3600.0 * 50,
        step_s=3600.0,
        capacitance_j_per_k=400_000.0,
        coolant_resistance_k_per_w=0.05,
        thermostat_temp_k=308.15,
        thermostat_full_open_temp_k=313.15,
    )
    assert samples[-1][1] == pytest.approx(313.15, abs=0.01)


def test_relax_parked_temp_k_chiller_present_settles_below_ambient() -> None:
    """The one behavior a chiller exists to provide and a plain coolant-path
    conductance cannot: pulling the pack below ambient, not just toward it
    faster (see relax_parked_temp_k's own Q-0021 docstring section).
    """
    samples = relax_parked_temp_k(
        start_temp_k=330.0,
        tau_s=None,
        ambient_fn=lambda _t: 313.15,
        start_t_s=0.0,
        duration_s=3600.0 * 50,
        step_s=3600.0,
        capacitance_j_per_k=400_000.0,
        coolant_resistance_k_per_w=0.05,
        thermostat_temp_k=308.15,
        thermostat_full_open_temp_k=313.15,
        chiller_present=True,
        chiller_capacity_w=5000.0,
    )
    assert samples[-1][1] < 313.15 - 1.0


def test_relax_parked_temp_k_chiller_hand_computed_single_step_at_full_duty() -> None:
    """One step, start_temp_k chosen >= thermostat_full_open_temp_k +
    DUTY_RAMP_SPAN_K so BOTH the coolant path's duty and the chiller's own
    duty are unambiguously 1.0 -- checked against the exact closed form
    (target_k = ambient - Q/G), not an approximate multi-step "steady
    state" (which a piecewise-constant-duty scheme doesn't cleanly have,
    since duty itself shifts every step as temp falls -- see the directional
    "settles below ambient" test above for that longer-horizon behavior).
    """
    capacitance_j_per_k = 400_000.0
    coolant_resistance_k_per_w = 0.05
    chiller_capacity_w = 5000.0
    ambient_k = 290.0
    start_temp_k = 335.0  # thermostat_full_open_temp_k(313.15) + DUTY_RAMP_SPAN_K(15) + margin

    total_conductance_w_per_k = 1.0 / coolant_resistance_k_per_w  # tau_s=None -- ambient term is 0
    expected_target_k = ambient_k - chiller_capacity_w / total_conductance_w_per_k
    expected_tau_s = capacitance_j_per_k / total_conductance_w_per_k
    expected = expected_target_k + (start_temp_k - expected_target_k) * math.exp(-3600.0 / expected_tau_s)

    samples = relax_parked_temp_k(
        start_temp_k=start_temp_k,
        tau_s=None,
        ambient_fn=lambda _t: ambient_k,
        start_t_s=0.0,
        duration_s=3600.0,
        step_s=3600.0,
        capacitance_j_per_k=capacitance_j_per_k,
        coolant_resistance_k_per_w=coolant_resistance_k_per_w,
        thermostat_temp_k=308.15,
        thermostat_full_open_temp_k=313.15,
        chiller_present=True,
        chiller_capacity_w=chiller_capacity_w,
    )
    assert samples[1][1] == pytest.approx(expected)


def test_relax_parked_temp_k_chiller_requires_coolant_path() -> None:
    """chiller_present/chiller_capacity_w are ignored without an active
    coolant path -- a chiller sits on the coolant loop, same gating
    cooling.py's real model uses.
    """
    no_coolant_path = relax_parked_temp_k(
        start_temp_k=330.0, tau_s=1000.0, ambient_fn=lambda _t: 290.0,
        start_t_s=0.0, duration_s=3600.0, step_s=3600.0,
        chiller_present=True, chiller_capacity_w=5000.0,
    )
    ambient_only = relax_parked_temp_k(
        start_temp_k=330.0, tau_s=1000.0, ambient_fn=lambda _t: 290.0,
        start_t_s=0.0, duration_s=3600.0, step_s=3600.0,
    )
    assert no_coolant_path == ambient_only


def test_pump_soc_decrement_only_counts_intervals_after_charge_event() -> None:
    """Mirrors _parked_soc_trajectory's own reset-step convention exactly:
    an interval wholly before, or straddling, the charge-reset boundary gets
    wiped by the reset regardless of its own drain, so it must not
    contribute here either -- only intervals starting at/after the reset do.
    """
    temp_samples = [(0.0, 320.0), (3600.0, 320.0), (7200.0, 320.0), (10800.0, 320.0)]
    # charge_event_s=5000 falls inside the [3600, 7200) interval -- that interval
    # and the [0, 3600) one before it must both be excluded.
    decrement = _pump_soc_decrement(
        temp_samples, charge_event_s=5000.0,
        thermostat_temp_k=300.0, thermostat_full_open_temp_k=305.0,  # duty=1.0 at 320K
        pump_decay_per_hour_at_full_duty=0.02,
    )
    assert decrement == pytest.approx(0.02)  # exactly one full hour ([7200, 10800)) at duty=1.0


def test_pump_soc_decrement_is_zero_below_thermostat() -> None:
    temp_samples = [(0.0, 290.0), (3600.0, 290.0)]
    decrement = _pump_soc_decrement(
        temp_samples, charge_event_s=0.0,
        thermostat_temp_k=300.0, thermostat_full_open_temp_k=305.0,
        pump_decay_per_hour_at_full_duty=0.02,
    )
    assert decrement == 0.0


def test_parked_soc_trajectory_decays_linearly_with_no_charge_event_in_window() -> None:
    samples = _parked_soc_trajectory(
        start_soc=0.7, start_t_s=0.0, duration_s=7200.0, charge_event_s=1_000_000.0,
        decay_per_hour=0.01, typical_end_soc=0.8, step_s=3600.0,
    )
    assert samples[0] == (0.0, 0.7)
    assert samples[1][1] == pytest.approx(0.69)
    assert samples[2][1] == pytest.approx(0.68)


def test_parked_soc_trajectory_snaps_to_typical_end_soc_at_charge_event_boundary() -> None:
    samples = _parked_soc_trajectory(
        start_soc=0.5, start_t_s=0.0, duration_s=7200.0, charge_event_s=3600.0,
        decay_per_hour=0.01, typical_end_soc=0.8, step_s=3600.0,
    )
    assert samples[1][1] == pytest.approx(0.8)  # step boundary crosses charge_event_s exactly
    assert samples[2][1] == pytest.approx(0.79)  # decays again after the charge event


def test_parked_soc_trajectory_charge_event_outside_window_never_fires() -> None:
    no_event = _parked_soc_trajectory(
        start_soc=0.5, start_t_s=0.0, duration_s=3600.0, charge_event_s=-100.0,
        decay_per_hour=0.01, typical_end_soc=0.8, step_s=3600.0,
    )
    assert no_event[-1][1] == pytest.approx(0.49)
    no_event_future = _parked_soc_trajectory(
        start_soc=0.5, start_t_s=0.0, duration_s=3600.0, charge_event_s=1_000_000.0,
        decay_per_hour=0.01, typical_end_soc=0.8, step_s=3600.0,
    )
    assert no_event_future[-1][1] == pytest.approx(0.49)


def test_parked_soc_trajectory_never_goes_negative() -> None:
    samples = _parked_soc_trajectory(
        start_soc=0.05, start_t_s=0.0, duration_s=3600.0 * 10, charge_event_s=1_000_000.0,
        decay_per_hour=0.5, typical_end_soc=0.8, step_s=3600.0,
    )
    assert all(soc >= 0.0 for _t, soc in samples)


def test_parked_soc_trajectory_zero_duration_returns_only_start_sample() -> None:
    samples = _parked_soc_trajectory(
        start_soc=0.6, start_t_s=0.0, duration_s=0.0, charge_event_s=1000.0,
        decay_per_hour=0.01, typical_end_soc=0.8,
    )
    assert samples == [(0.0, 0.6)]


def test_parked_soc_trajectory_matches_relax_parked_temp_k_sample_timestamps() -> None:
    """`parked_calendar_aging`'s `strict=True` zip requires these two series to
    share an identical time axis -- this is the guard against that silently
    breaking."""
    temp_samples = relax_parked_temp_k(
        start_temp_k=300.0, tau_s=1000.0, ambient_fn=lambda _t: 290.0,
        start_t_s=0.0, duration_s=7500.0, step_s=3600.0,
    )
    soc_samples = _parked_soc_trajectory(
        start_soc=0.6, start_t_s=0.0, duration_s=7500.0, charge_event_s=1_000_000.0,
        decay_per_hour=0.01, typical_end_soc=0.8, step_s=3600.0,
    )
    assert [t for t, _ in soc_samples] == [t for t, _ in temp_samples]


def test_parked_calendar_aging_matches_direct_compute_aging_step_sum() -> None:
    profile = _make_long_horizon_profile()
    spec = profile.battery
    temp_samples = [(0.0, 300.0), (3600.0, 305.0), (7200.0, 302.0)]
    soc_samples = [(0.0, 0.6), (3600.0, 0.6), (7200.0, 0.6)]
    result = parked_calendar_aging(spec, soc_samples=soc_samples, temp_samples=temp_samples)
    expected = sum(
        compute_aging_step(
            spec, soh=1.0, cell_temp_k=temp0, soc=0.6, applied_current_a=0.0, dt_s=t1 - t0
        ).calendar_delta
        for (t0, temp0), (t1, _temp1) in zip(temp_samples, temp_samples[1:], strict=False)
    )
    assert result == pytest.approx(expected)


def test_parked_calendar_aging_uses_each_intervals_own_soc_not_a_constant() -> None:
    """Milestone "parked-SOC model" (Q-0012): `parked_calendar_aging` must
    actually read each interval's own start-of-interval SOC, not silently
    collapse to a single constant the way it did before this milestone.
    """
    profile = _make_long_horizon_profile()
    spec = profile.battery
    temp_samples = [(0.0, 300.0), (3600.0, 300.0), (7200.0, 300.0)]
    high_soc_samples = [(0.0, 0.8), (3600.0, 0.8), (7200.0, 0.8)]
    low_soc_samples = [(0.0, 0.3), (3600.0, 0.3), (7200.0, 0.3)]

    high_result = parked_calendar_aging(spec, soc_samples=high_soc_samples, temp_samples=temp_samples)
    low_result = parked_calendar_aging(spec, soc_samples=low_soc_samples, temp_samples=temp_samples)

    assert high_result > low_result  # calendar_aging_soc_sensitivity=1.0: higher SOC ages faster


def test_parked_calendar_aging_raises_on_length_mismatch() -> None:
    profile = _make_long_horizon_profile()
    spec = profile.battery
    temp_samples = [(0.0, 300.0), (3600.0, 300.0)]
    soc_samples = [(0.0, 0.6), (3600.0, 0.6), (7200.0, 0.6)]
    with pytest.raises(ValueError, match="shorter"):
        parked_calendar_aging(spec, soc_samples=soc_samples, temp_samples=temp_samples)


def test_apply_trip_start_offset_is_noop_at_assumed_start_temp() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile, ambient_temp_k=298.15)
    characterization = characterize_trip(profile, scenario, TripType.COLD_START_COMMUTE, 298.15)
    assumed_start = _archetype_assumed_start_temp_k(TripType.COLD_START_COMMUTE, 298.15, profile.battery)

    corrected = apply_trip_start_offset(characterization, assumed_start, profile.battery)

    assert corrected.phases[0].mean_cell_temp_k == pytest.approx(characterization.phases[0].mean_cell_temp_k)


def test_apply_trip_start_offset_shifts_phases_and_battery_final_temp() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile, ambient_temp_k=298.15)
    characterization = characterize_trip(profile, scenario, TripType.COLD_START_COMMUTE, 298.15)
    assumed_start = _archetype_assumed_start_temp_k(TripType.COLD_START_COMMUTE, 298.15, profile.battery)
    actual_start = assumed_start + 10.0

    corrected = apply_trip_start_offset(characterization, actual_start, profile.battery)

    for original_phase, corrected_phase in zip(characterization.phases, corrected.phases, strict=True):
        assert corrected_phase.mean_cell_temp_k == pytest.approx(original_phase.mean_cell_temp_k + 10.0)
        assert corrected_phase.peak_cell_temp_k == pytest.approx(original_phase.peak_cell_temp_k + 10.0)
        assert corrected_phase.mean_soc == pytest.approx(original_phase.mean_soc)
        assert corrected_phase.throughput_ah == pytest.approx(original_phase.throughput_ah)
    battery_name = profile.battery.thermal_node.name
    assert corrected.final_node_temps_k[battery_name] == pytest.approx(
        characterization.final_node_temps_k[battery_name] + 10.0
    )


def test_interpolate_characterization_matches_endpoints_at_bucket_edges() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    characterizations = [
        characterize_trip(profile, scenario, TripType.HIGHWAY, bucket) for bucket in (283.15, 313.15)
    ]
    low = _interpolate_characterization(characterizations, 283.15)
    high = _interpolate_characterization(characterizations, 313.15)
    assert low.phases[0].mean_cell_temp_k == pytest.approx(characterizations[0].phases[0].mean_cell_temp_k)
    assert high.phases[0].mean_cell_temp_k == pytest.approx(characterizations[1].phases[0].mean_cell_temp_k)


def test_interpolate_characterization_midpoint_averages_neighbors() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    characterizations = [
        characterize_trip(profile, scenario, TripType.HIGHWAY, bucket) for bucket in (283.15, 313.15)
    ]
    midpoint = _interpolate_characterization(characterizations, 298.15)
    expected = (
        characterizations[0].phases[0].mean_cell_temp_k + characterizations[1].phases[0].mean_cell_temp_k
    ) / 2.0
    assert midpoint.phases[0].mean_cell_temp_k == pytest.approx(expected)


def test_interpolate_characterization_clamps_outside_range() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    characterizations = [
        characterize_trip(profile, scenario, TripType.HIGHWAY, bucket) for bucket in (283.15, 313.15)
    ]
    below = _interpolate_characterization(characterizations, 250.0)
    above = _interpolate_characterization(characterizations, 350.0)
    assert below.phases[0].mean_cell_temp_k == pytest.approx(characterizations[0].phases[0].mean_cell_temp_k)
    assert above.phases[0].mean_cell_temp_k == pytest.approx(characterizations[1].phases[0].mean_cell_temp_k)


def test_characterize_all_trips_returns_five_types_with_ascending_buckets() -> None:
    # Milestone E2e added FAST_CHARGE_SESSION as a fifth type in this dict's
    # output, produced by characterize_fast_charge_session rather than
    # characterize_trip -- see test_characterize_all_trips_includes_fast_
    # charge_session for a test focused specifically on that addition.
    profile = _make_profile()
    scenario = _make_scenario(profile)
    result = characterize_all_trips(profile, scenario, n_buckets=4)
    assert set(result.keys()) == {
        TripType.COLD_START_COMMUTE,
        TripType.HOT_SOAK_COMMUTE,
        TripType.HIGHWAY,
        TripType.CITY_STOP_AND_GO,
        TripType.FAST_CHARGE_SESSION,
    }
    for characterizations in result.values():
        assert len(characterizations) == 4
        buckets = [c.ambient_bucket_temp_k for c in characterizations]
        assert buckets == sorted(buckets)


def test_simulate_long_horizon_checkpoint_count_matches_days() -> None:
    profile = _make_long_horizon_profile()
    scenario = _make_scenario(profile)
    characterizations = characterize_all_trips(profile, scenario)
    result = simulate_long_horizon(
        profile=profile, trip_characterizations=characterizations, day_type_mix=_make_day_type_mix(),
        climate=scenario.climate, charging_profile=scenario.charging_profile, duration_years=1, seed=1,
    )
    assert len(result.checkpoints) == 365
    assert [c.day_index for c in result.checkpoints] == list(range(365))


def test_simulate_long_horizon_soh_is_monotonically_non_increasing() -> None:
    profile = _make_long_horizon_profile()
    scenario = _make_scenario(profile)
    characterizations = characterize_all_trips(profile, scenario)
    result = simulate_long_horizon(
        profile=profile, trip_characterizations=characterizations, day_type_mix=_make_day_type_mix(),
        climate=scenario.climate, charging_profile=scenario.charging_profile, duration_years=2, seed=1,
    )
    soh_trace = [c.battery_soh for c in result.checkpoints]
    assert all(a >= b - 1e-12 for a, b in zip(soh_trace, soh_trace[1:], strict=False))
    assert soh_trace[-1] < soh_trace[0]


def test_simulate_long_horizon_odometer_only_advances_on_trip_days() -> None:
    profile = _make_long_horizon_profile()
    scenario = _make_scenario(profile)
    characterizations = characterize_all_trips(profile, scenario)
    result = simulate_long_horizon(
        profile=profile, trip_characterizations=characterizations, day_type_mix=_make_day_type_mix(),
        climate=scenario.climate, charging_profile=scenario.charging_profile, duration_years=1, seed=1,
    )
    odometer = [0.0, *(c.odometer_km for c in result.checkpoints)]
    n_moving_days = sum(1 for a, b in zip(odometer, odometer[1:], strict=False) if b > a + 1e-9)
    n_trip_days = 60 + 60 + 20 + 100
    assert n_moving_days == n_trip_days


def test_simulate_long_horizon_temp_stays_bounded_with_ambient_coupling() -> None:
    # Regression guard: _make_profile()'s own battery is adiabatic (no
    # resistance_to_ambient_k_per_w), which makes repeated per-trip self-heating
    # compound without bound over multiple years -- correct for a genuinely
    # adiabatic node, but not what a real profile does (confirmed against the
    # real Leaf profile during development). This asserts the fixture WITH
    # ambient coupling stays physically bounded over a 5-year horizon.
    profile = _make_long_horizon_profile()
    scenario = _make_scenario(profile)
    characterizations = characterize_all_trips(profile, scenario)
    result = simulate_long_horizon(
        profile=profile, trip_characterizations=characterizations, day_type_mix=_make_day_type_mix(),
        climate=scenario.climate, charging_profile=scenario.charging_profile, duration_years=5, seed=1,
    )
    temps = [c.pack_cell_temp_k for c in result.checkpoints]
    assert max(temps) < 340.0
    assert min(temps) > 260.0


def test_simulate_long_horizon_deterministic_under_fixed_seed() -> None:
    profile = _make_long_horizon_profile()
    scenario = _make_scenario(profile)
    characterizations = characterize_all_trips(profile, scenario)
    day_type_mix = _make_day_type_mix()

    def run() -> list[float]:
        result = simulate_long_horizon(
            profile=profile,
            trip_characterizations=characterizations,
            day_type_mix=day_type_mix,
            climate=scenario.climate,
            charging_profile=scenario.charging_profile,
            duration_years=1,
            seed=99,
        )
        return [c.battery_soh for c in result.checkpoints]

    assert run() == run()


# --- Milestone E2e: fast-charge characterization ---


def test_fast_charge_phases_normal_crossing() -> None:
    time_s = [0.0, 100.0, 200.0, 300.0]
    cell_temp_k = [300.0, 301.0, 302.0, 303.0]
    soc = [0.5, 0.55, 0.65, 0.8]
    current_a = [-100.0, -100.0, -80.0, -60.0]

    ramp, taper = _fast_charge_phases(time_s, cell_temp_k, soc, current_a, capacity_ah=100.0, session_duration_s=300.0)

    assert ramp.phase_name == "ramp"
    assert taper.phase_name == "taper"
    assert ramp.duration_s == pytest.approx(200.0)
    assert taper.duration_s == pytest.approx(100.0)
    assert ramp.duration_s + taper.duration_s == pytest.approx(300.0)


def test_fast_charge_phases_never_crosses_knee_gives_degenerate_taper() -> None:
    time_s = [0.0, 100.0, 200.0, 300.0]
    cell_temp_k = [300.0, 301.0, 302.0, 303.0]
    soc = [0.1, 0.2, 0.3, 0.4]  # never reaches FAST_CHARGE_SOC_KNEE
    current_a = [-100.0, -100.0, -100.0, -100.0]

    ramp, taper = _fast_charge_phases(time_s, cell_temp_k, soc, current_a, capacity_ah=100.0, session_duration_s=300.0)

    assert ramp.duration_s == pytest.approx(300.0)
    assert taper.duration_s == 0.0
    assert taper.throughput_ah == 0.0
    # degenerate taper is built from the FINAL sample
    assert taper.mean_cell_temp_k == pytest.approx(cell_temp_k[-1])
    assert taper.mean_soc == pytest.approx(soc[-1])


def test_fast_charge_phases_starts_above_knee_gives_degenerate_ramp() -> None:
    time_s = [0.0, 100.0, 200.0, 300.0]
    cell_temp_k = [300.0, 301.0, 302.0, 303.0]
    soc = [0.65, 0.7, 0.8, 0.9]  # already past FAST_CHARGE_SOC_KNEE at t=0
    current_a = [-60.0, -60.0, -60.0, -60.0]

    ramp, taper = _fast_charge_phases(time_s, cell_temp_k, soc, current_a, capacity_ah=100.0, session_duration_s=300.0)

    assert ramp.duration_s == 0.0
    assert ramp.throughput_ah == 0.0
    # degenerate ramp is built from the FIRST (t=0) sample
    assert ramp.mean_cell_temp_k == pytest.approx(cell_temp_k[0])
    assert ramp.mean_soc == pytest.approx(soc[0])
    assert taper.duration_s == pytest.approx(300.0)


def test_fast_charge_phases_soc_monotonic_means_at_most_one_crossing() -> None:
    # A charging session's SOC can only rise (see characterize_fast_charge_
    # session's own docstring for why) -- confirm the boundary detection finds
    # the FIRST crossing, not confused by a hypothetical non-monotonic trace.
    time_s = [0.0, 100.0, 200.0, 300.0, 400.0]
    cell_temp_k = [300.0] * 5
    soc = [0.5, 0.61, 0.62, 0.63, 0.64]
    current_a = [-100.0] * 5

    ramp, taper = _fast_charge_phases(time_s, cell_temp_k, soc, current_a, capacity_ah=100.0, session_duration_s=400.0)

    assert ramp.duration_s == pytest.approx(100.0)
    assert taper.duration_s == pytest.approx(300.0)


def test_characterize_fast_charge_session_has_two_phases_named_ramp_and_taper() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile, ambient_temp_k=298.15)

    characterization = characterize_fast_charge_session(profile, scenario, 298.15)

    assert characterization.trip_type is TripType.FAST_CHARGE_SESSION
    assert [p.phase_name for p in characterization.phases] == ["ramp", "taper"]
    total_duration = sum(p.duration_s for p in characterization.phases)
    assert total_duration == pytest.approx(FAST_CHARGE_SESSION_DURATION_S)


def test_characterize_fast_charge_session_soc_increases_and_current_is_charging() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile, ambient_temp_k=298.15)

    characterization = characterize_fast_charge_session(profile, scenario, 298.15)

    assert characterization.final_soc > scenario.charging_profile.typical_start_soc.value
    for phase in characterization.phases:
        assert phase.mean_c_rate >= 0.0  # stored as a magnitude


def test_characterize_all_trips_includes_fast_charge_session() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    result = characterize_all_trips(profile, scenario)

    assert TripType.FAST_CHARGE_SESSION in result
    fast_charge_characterizations = result[TripType.FAST_CHARGE_SESSION]
    assert len(fast_charge_characterizations) == 4
    buckets = [c.ambient_bucket_temp_k for c in fast_charge_characterizations]
    assert buckets == sorted(buckets)
    for c in fast_charge_characterizations:
        assert len(c.phases) == 2


def test_fast_charge_soc_knee_matches_placeholder_curve_documentation() -> None:
    # Milestone E2e's ratified plan: FAST_CHARGE_SOC_KNEE mirrors
    # BatterySpec.charge_current_limit_curve's placeholder knee (0.6) --
    # a documentation/consistency guard, not a physics test.
    assert FAST_CHARGE_SOC_KNEE == 0.6
