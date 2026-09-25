"""Tests for `ev_thermal.mc` (Monte Carlo v1: battery aging-coefficient
sampling, see docs/DESIGN_QUESTIONS.md Q-0008 through Q-0011).
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from examples.flagship_comparison import (
    DEFAULT_DURATION_DAYS,
    DEFAULT_PROFILE_A,
    DEFAULT_SEED,
    build_scenario,
    default_day_type_mix,
    guam_climate,
)

from ev_thermal.long_horizon import characterize_all_trips, simulate_long_horizon
from ev_thermal.mc import (
    CV_BY_CONFIDENCE,
    _compute_percentile_band,
    _resolve_battery_field_name,
    run_monte_carlo,
    sample_battery_spec,
    sample_lognormal,
)
from ev_thermal.profile_loader import load_profile
from ev_thermal.schema import Confidence, MCConfig, MCSampleResult
from tests.test_battery import _make_battery_spec

_ALL_AGING_PATHS = [
    "battery.calendar_aging_rate_per_s",
    "battery.calendar_aging_activation_temp_k",
    "battery.calendar_aging_soc_sensitivity",
    "battery.cycle_aging_rate_per_efc",
    "battery.cycle_aging_activation_temp_k",
    "battery.cycle_aging_c_rate_sensitivity",
    "battery.resistance_growth_per_soh_loss",
]


def test_resolve_battery_field_name_accepts_battery_prefixed_path() -> None:
    assert _resolve_battery_field_name("battery.calendar_aging_rate_per_s") == "calendar_aging_rate_per_s"


def test_resolve_battery_field_name_rejects_non_battery_path() -> None:
    with pytest.raises(ValueError, match="only supports 'battery"):
        _resolve_battery_field_name("climate.mean_annual_temp_k")


def test_sample_battery_spec_raises_on_non_sourced_field() -> None:
    spec = _make_battery_spec()
    with pytest.raises(ValueError, match="does not resolve to a Sourced field"):
        sample_battery_spec(spec, ["battery.chemistry"], np.random.default_rng(1))


def test_sample_lognormal_mean_and_cv_match_target_over_many_draws() -> None:
    rng = np.random.default_rng(0)
    target_mean = 10.0
    for confidence in Confidence:
        draws = [sample_lognormal(target_mean, confidence, rng) for _ in range(20_000)]
        empirical_median = float(np.median(draws))
        empirical_cv = float(np.std(draws) / np.mean(draws))
        target_cv = CV_BY_CONFIDENCE[confidence]
        assert empirical_median == pytest.approx(target_mean, rel=0.03)
        assert empirical_cv == pytest.approx(target_cv, rel=0.1)


def test_sample_lognormal_rejects_non_positive_mean() -> None:
    with pytest.raises(ValueError, match="strictly positive"):
        sample_lognormal(0.0, Confidence.LOW, np.random.default_rng(0))


def test_sample_battery_spec_leaves_unsampled_fields_untouched() -> None:
    spec = _make_battery_spec()
    perturbed, sampled_values = sample_battery_spec(
        spec, ["battery.calendar_aging_rate_per_s"], np.random.default_rng(1)
    )
    assert perturbed.calendar_aging_rate_per_s.value != spec.calendar_aging_rate_per_s.value
    assert perturbed.cycle_aging_rate_per_efc.value == spec.cycle_aging_rate_per_efc.value
    assert set(sampled_values) == {"battery.calendar_aging_rate_per_s"}
    assert sampled_values["battery.calendar_aging_rate_per_s"] == perturbed.calendar_aging_rate_per_s.value


def test_compute_percentile_band_matches_hand_computed_small_n() -> None:
    samples = [
        MCSampleResult(sample_index=0, sampled_values={}, soh_trajectory=[1.0, 0.9], endpoint_metrics={}),
        MCSampleResult(sample_index=1, sampled_values={}, soh_trajectory=[1.0, 0.8], endpoint_metrics={}),
        MCSampleResult(sample_index=2, sampled_values={}, soh_trajectory=[1.0, 0.7], endpoint_metrics={}),
    ]
    band = _compute_percentile_band(samples)
    assert band[0].day_index == 0
    assert band[0].p05 == pytest.approx(1.0)
    assert band[0].p50 == pytest.approx(1.0)
    assert band[0].p95 == pytest.approx(1.0)
    expected_day1 = np.percentile([0.9, 0.8, 0.7], [5.0, 50.0, 95.0])
    assert band[1].p05 == pytest.approx(float(expected_day1[0]))
    assert band[1].p50 == pytest.approx(float(expected_day1[1]))
    assert band[1].p95 == pytest.approx(float(expected_day1[2]))


def test_run_monte_carlo_band_is_monotonic_every_day() -> None:
    profile = load_profile(DEFAULT_PROFILE_A)
    climate = guam_climate()
    scenario = build_scenario(profile, climate, DEFAULT_SEED)
    day_type_mix = default_day_type_mix()
    config = MCConfig(
        n_runs=30,
        parameters_to_vary=_ALL_AGING_PATHS,
        random_seed=7,
        parallel=False,
        output_metrics=["final_soh", "final_calendar_loss", "final_cycle_loss"],
    )

    result = run_monte_carlo(profile, scenario, day_type_mix, config, duration_years=1)

    assert len(result.samples) == 30
    for band_point in result.soh_band:
        assert band_point.p05 <= band_point.p50 <= band_point.p95
    for sample in result.samples:
        assert set(sample.sampled_values) == set(_ALL_AGING_PATHS)
        assert math.isfinite(sample.endpoint_metrics["final_soh"])


def test_run_monte_carlo_raises_on_unrecognized_output_metric() -> None:
    profile = load_profile(DEFAULT_PROFILE_A)
    climate = guam_climate()
    scenario = build_scenario(profile, climate, DEFAULT_SEED)
    day_type_mix = default_day_type_mix()
    config = MCConfig(
        n_runs=2,
        parameters_to_vary=["battery.calendar_aging_rate_per_s"],
        random_seed=1,
        parallel=False,
        output_metrics=["not_a_real_metric"],
    )
    with pytest.raises(ValueError, match="unrecognized MCConfig.output_metrics"):
        run_monte_carlo(profile, scenario, day_type_mix, config, duration_years=1)


def test_mc_p50_approximates_flagship_point_estimate() -> None:
    """The log-normal sampler is centered at each field's `value` (a
    log-normal's median equals its target mean), and `run_monte_carlo`
    holds the day-type schedule seed fixed at `scenario.random_seed` --
    the same seed a direct, unperturbed `simulate_long_horizon` call uses.
    So the MC median trajectory should match that direct run's trajectory
    up to Monte Carlo sampling error. A mismatch here means either the
    schedule seeds have drifted apart, or the sampler is biased -- both
    real bugs this test exists to catch before they get rationalized away
    as noise.
    """
    profile = load_profile(DEFAULT_PROFILE_A)
    climate = guam_climate()
    scenario = build_scenario(profile, climate, DEFAULT_SEED)
    day_type_mix = default_day_type_mix()

    characterizations = characterize_all_trips(profile, scenario, n_buckets=4)
    point_estimate = simulate_long_horizon(
        profile=profile,
        trip_characterizations=characterizations,
        day_type_mix=day_type_mix,
        climate=scenario.climate,
        charging_profile=scenario.charging_profile,
        duration_years=1,
        seed=scenario.random_seed,
    )

    config = MCConfig(
        n_runs=200,
        parameters_to_vary=_ALL_AGING_PATHS,
        random_seed=999,
        parallel=False,
        output_metrics=["final_soh"],
    )
    mc_result = run_monte_carlo(profile, scenario, day_type_mix, config, duration_years=1)

    for day_index in range(DEFAULT_DURATION_DAYS):
        flagship_soh = point_estimate.checkpoints[day_index].battery_soh
        mc_p50 = mc_result.soh_band[day_index].p50
        assert mc_p50 == pytest.approx(flagship_soh, rel=0.05)
