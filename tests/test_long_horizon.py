"""Tests for Milestone E2b's Tier-1 representative-trip characterization
(`ev_thermal.long_horizon`): the ambient sinusoid, the ambient-bucket grid,
waypoint interpolation, phase-aging aggregation, and `characterize_trip`
itself against the shared `tests.test_sim` fixture profile.
"""

from __future__ import annotations

import pytest

from ev_thermal.long_horizon import (
    ANNUAL_PEAK_DAY_OF_YEAR,
    DIURNAL_PEAK_HOUR,
    _phase_aging,
    _speed_from_waypoints,
    ambient_bucket_temps_k,
    ambient_temp_k,
    characterize_trip,
)
from ev_thermal.schema import TripType
from tests.test_sim import _make_profile, _make_scenario

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
