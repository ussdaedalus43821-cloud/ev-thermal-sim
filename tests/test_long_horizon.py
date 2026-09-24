"""Tests for Milestone E2b/E2c (`ev_thermal.long_horizon`): Tier-1
representative-trip characterization (the ambient sinusoid, the ambient-bucket
grid, waypoint interpolation, phase-aging aggregation, `characterize_trip`) and
Tier-2 long-horizon extrapolation (day-type shuffling, parked-period
relaxation, trip-start chaining, ambient-bucket interpolation,
`simulate_long_horizon`) -- all against the shared `tests.test_sim` fixture
profile, except where noted.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from ev_thermal.aging import compute_aging_step
from ev_thermal.long_horizon import (
    ANNUAL_PEAK_DAY_OF_YEAR,
    DIURNAL_PEAK_HOUR,
    _archetype_assumed_start_temp_k,
    _interpolate_characterization,
    _phase_aging,
    _speed_from_waypoints,
    ambient_bucket_temps_k,
    ambient_temp_k,
    apply_trip_start_offset,
    characterize_all_trips,
    characterize_trip,
    parked_calendar_aging,
    relax_parked_temp_k,
    shuffle_day_type_schedule,
    simulate_long_horizon,
)
from ev_thermal.schema import AnnualDayTypeMix, Confidence, ManufacturerProfile, Sourced, TripType
from tests.test_sim import _make_profile, _make_scenario


def _sourced(value):
    return Sourced[type(value)](value=value, confidence=Confidence.LOW, source="test-fixture")


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
    profile = _make_profile()
    scenario = _make_scenario(profile)
    with pytest.raises(NotImplementedError, match="Milestone E2e"):
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


def test_parked_calendar_aging_matches_direct_compute_aging_step_sum() -> None:
    profile = _make_long_horizon_profile()
    spec = profile.battery
    samples = [(0.0, 300.0), (3600.0, 305.0), (7200.0, 302.0)]
    result = parked_calendar_aging(spec, soc=0.6, temp_samples=samples)
    expected = sum(
        compute_aging_step(
            spec, soh=1.0, cell_temp_k=temp0, soc=0.6, applied_current_a=0.0, dt_s=t1 - t0
        ).calendar_delta
        for (t0, temp0), (t1, _temp1) in zip(samples, samples[1:], strict=False)
    )
    assert result == pytest.approx(expected)
    assert result > 0.0


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


def test_characterize_all_trips_returns_four_archetypes_with_ascending_buckets() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    result = characterize_all_trips(profile, scenario, n_buckets=4)
    assert set(result.keys()) == {
        TripType.COLD_START_COMMUTE,
        TripType.HOT_SOAK_COMMUTE,
        TripType.HIGHWAY,
        TripType.CITY_STOP_AND_GO,
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
    kwargs = dict(
        profile=profile, trip_characterizations=characterizations, day_type_mix=_make_day_type_mix(),
        climate=scenario.climate, charging_profile=scenario.charging_profile, duration_years=1, seed=99,
    )
    a = simulate_long_horizon(**kwargs)
    b = simulate_long_horizon(**kwargs)
    assert [c.battery_soh for c in a.checkpoints] == [c.battery_soh for c in b.checkpoints]
