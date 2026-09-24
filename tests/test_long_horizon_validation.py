"""Slow validation harness for Milestone E2c (excluded from the default `pytest`
run -- see pyproject.toml's `addopts`; run explicitly with `pytest -m slow`).

Proves Tier-2 (`simulate_long_horizon`) approximates Tier-1 (continuous
full-fidelity `run_scenario` physics) within tolerance over a real 30-90 day
window, using the IDENTICAL day-type schedule and ambient lookups for both --
the comparison isolates Tier-2's phase-averaging/chaining/relaxation
approximations, not whether two independent random usage realizations happen
to age similarly. Made affordable by Milestone E2c-0's O(n^2) fix: before
that fix, a 90-day continuous reference run exceeded a 120s bounded timeout;
this harness now completes in well under a minute.
"""

from __future__ import annotations

import numpy as np
import pytest

from ev_thermal.long_horizon import (
    _ARCHETYPES,
    TRIP_HOUR_OF_DAY,
    _speed_from_waypoints,
    ambient_temp_k,
    characterize_all_trips,
    shuffle_day_type_schedule,
    simulate_long_horizon,
)
from ev_thermal.schema import ManufacturerProfile, Scenario, SimState, TripType
from ev_thermal.sim import run_scenario
from tests.test_long_horizon import _make_day_type_mix, _make_long_horizon_profile
from tests.test_sim import _make_scenario


def _generate_tier1_reference_soh(
    profile: ManufacturerProfile,
    scenario_template: Scenario,
    schedule: list[TripType | None],
    start_day_of_year: float,
) -> list[float]:
    """Full-fidelity ground truth: one `run_scenario` call per day (86400s),
    chained via `initial_state`, each day's ambient pinned constant to
    `ambient_temp_k(climate, day_of_year, TRIP_HOUR_OF_DAY)` -- the same
    per-day-constant-ambient approximation `characterize_trip` already uses,
    extended day-by-day instead of once per archetype+bucket.

    Applies the SAME instantaneous SOC top-up `simulate_long_horizon` uses
    after every trip day (patching `state.battery_soc` directly, the same
    `SimState`-patching pattern `characterize_trip` already uses for initial
    pack temp). `sim.py` has no charger model at all, so without this, the
    reference's SOC would simply deplete toward zero with no recharging,
    while Tier-2 holds it near `typical_end_soc` -- a real discrepancy
    between the two, but about charging modeling, not about the aging-
    extrapolation math this validation exists to check. Leaving it in would
    contaminate the comparison with a confound unrelated to what's tested.

    Returns the per-day `battery_soh` trace, one value per schedule entry.
    """
    state: SimState | None = None
    soh_trace: list[float] = []
    for day_index, trip_type in enumerate(schedule):
        day_of_year = start_day_of_year + day_index + TRIP_HOUR_OF_DAY / 24.0
        day_ambient_k = ambient_temp_k(scenario_template.climate, day_of_year, TRIP_HOUR_OF_DAY)
        climate = scenario_template.climate.model_copy(
            update={
                "mean_annual_temp_k": scenario_template.climate.mean_annual_temp_k.model_copy(
                    update={"value": day_ambient_k}
                ),
                "seasonal_amplitude_k": scenario_template.climate.seasonal_amplitude_k.model_copy(
                    update={"value": 0.0}
                ),
                "daily_amplitude_k": scenario_template.climate.daily_amplitude_k.model_copy(update={"value": 0.0}),
            }
        )
        scenario = scenario_template.model_copy(update={"manufacturer_profile": profile, "climate": climate})
        speed_fn = (
            _speed_from_waypoints(_ARCHETYPES[trip_type].waypoints) if trip_type is not None else (lambda _t: 0.0)
        )
        result = run_scenario(
            profile,
            scenario,
            duration_s=86400.0,
            target_speed_mps=speed_fn,
            initial_state=state,
            record_interval_s=3600.0,
        )
        state = result.final_state
        if trip_type is not None:
            state = state.model_copy(
                update={"battery_soc": scenario_template.charging_profile.typical_end_soc.value}
            )
        soh_trace.append(state.battery_soh)
    return soh_trace


@pytest.mark.slow
def test_tier2_matches_tier1_reference_within_tolerance() -> None:
    n_days = 60
    profile = _make_long_horizon_profile()
    scenario = _make_scenario(profile)
    day_type_mix = _make_day_type_mix()
    seed = 123
    start_day = 100.0

    schedule = shuffle_day_type_schedule(day_type_mix, n_days, np.random.default_rng(seed))

    tier1_soh = _generate_tier1_reference_soh(profile, scenario, schedule, start_day)
    tier1_loss = 1.0 - tier1_soh[-1]

    characterizations = characterize_all_trips(profile, scenario)
    tier2_result = simulate_long_horizon(
        profile=profile,
        trip_characterizations=characterizations,
        day_type_mix=day_type_mix,
        climate=scenario.climate,
        charging_profile=scenario.charging_profile,
        duration_years=1,
        seed=seed,
        start_day_of_year=start_day,
    )
    tier2_loss = 1.0 - tier2_result.checkpoints[n_days - 1].battery_soh

    # 5% relative alone is degenerate at short/low-magnitude horizons: at this
    # window's cumulative loss, 5% of it can sit below the resolution of the
    # placeholder aging coefficients themselves, so a relative-only tolerance
    # would either pass on noise or fail spuriously once real coefficients are
    # sourced and shift slightly. The 0.1% absolute floor keeps the test
    # meaningful at any horizon length; 5% relative takes over once losses are
    # large enough for it to be the binding constraint.
    tolerance = max(0.05 * tier1_loss, 0.001)
    assert abs(tier2_loss - tier1_loss) <= tolerance, (
        f"tier1_loss={tier1_loss:.6f} tier2_loss={tier2_loss:.6f} tolerance={tolerance:.6f}"
    )
