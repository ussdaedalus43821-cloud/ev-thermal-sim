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
from ev_thermal.profile_loader import PROFILES_DIR, load_profile
from ev_thermal.schema import Climate, ManufacturerProfile, Scenario, SimState, TripType
from ev_thermal.sim import run_scenario
from tests.test_long_horizon import _make_day_type_mix
from tests.test_sim import _make_scenario

_PARKED_STEP_S = 3600.0
"""Reference-generator sub-stepping granularity during parked stretches --
matches relax_parked_temp_k's own step_s exactly, for the reasons in
_generate_tier1_reference_soh's docstring."""


def _pinned_climate(base_climate: Climate, ambient_k: float) -> Climate:
    return base_climate.model_copy(
        update={
            "mean_annual_temp_k": base_climate.mean_annual_temp_k.model_copy(update={"value": ambient_k}),
            "seasonal_amplitude_k": base_climate.seasonal_amplitude_k.model_copy(update={"value": 0.0}),
            "daily_amplitude_k": base_climate.daily_amplitude_k.model_copy(update={"value": 0.0}),
        }
    )


def _generate_tier1_reference_soh(
    profile: ManufacturerProfile,
    scenario_template: Scenario,
    schedule: list[TripType | None],
    start_day_of_year: float,
) -> list[float]:
    """Full-fidelity ground truth, chained via `initial_state` across many
    `run_scenario` calls. Two granularity choices below were NOT arbitrary --
    both were found necessary by direct empirical testing (a naive once-per-
    day version passed with the synthetic test fixture but failed against
    real profiles by a wide margin; each fix was verified in isolation before
    being combined here):

    1. **Ambient is sub-stepped hourly during parked stretches**, not pinned
       constant for the whole day. A trip's own portion still uses ONE
       constant ambient at `TRIP_HOUR_OF_DAY` (matching `characterize_trip`'s
       convention, appropriate for a 20-60 minute call) -- but extending that
       same pin across a full 24h parked day is a much coarser approximation,
       and it turned out to matter: pinning parked-day ambient at its 08:00
       value undersamples the daily cycle (08:00 sits well below the daily
       mean, given `DIURNAL_PEAK_HOUR=15.0`), producing a systematic ~2K
       reference-vs-Tier-2 temperature bias -- confirmed by direct comparison
       against the real Nissan Leaf profile (no coolant path to the battery
       at all, so nothing damps this out) before this fix, and confirmed
       collapsing to <0.5K after it. `relax_parked_temp_k` already varies
       ambient hourly during parked periods; the reference needs to match or
       exceed that granularity to be a fair ground truth, not the reverse.

    2. **SOC is re-patched to `typical_end_soc` every `_PARKED_STEP_S`
       interval, not once per day.** `sim.py` has no charger model, but
       `aux_current_a` draws unconditionally every step regardless of driving
       state -- so between SOC patches, the reference's SOC genuinely
       declines from continuous aux draw, and `compute_aging_step`'s calendar
       term (calculated live, every step, inside `advance_one_step`) sees
       that declining trajectory. Tier-2's `parked_calendar_aging` uses a
       single CONSTANT soc for an entire parked stretch (Tier-2 does not
       model parked-period SOC depletion at all -- see CLAUDE.md's SOC-
       snapping deferred-work note). A once-per-day patch only fixes the
       END-of-day snapshot; the within-day decline still biases the live
       calendar-aging integral low, since `calendar_aging_soc_sensitivity`
       makes lower SOC age slower. Patching hourly instead keeps the
       reference's SOC close to constant throughout, matching what Tier-2
       already assumes -- this is not a new approximation, it's applying the
       SAME already-accepted SOC-snapping simplification (see CLAUDE.md) at
       the granularity needed for it to actually hold throughout a parked
       stretch, not just at its boundary. Without this, the reference
       exhibited a real ~60-70% calendar-aging-rate excess relative to
       Tier-2 -- matching CLAUDE.md's own independent estimate of this
       approximation's effect almost exactly, which is itself a form of
       cross-validation for that estimate.

    Returns the per-day `battery_soh` trace, one value per schedule entry.
    """
    typical_end_soc = scenario_template.charging_profile.typical_end_soc.value
    state: SimState | None = None
    soh_trace: list[float] = []
    for day_index, trip_type in enumerate(schedule):
        elapsed_s = 0.0
        if trip_type is not None:
            trip_day_of_year = start_day_of_year + day_index + TRIP_HOUR_OF_DAY / 24.0
            trip_ambient_k = ambient_temp_k(scenario_template.climate, trip_day_of_year, TRIP_HOUR_OF_DAY)
            climate = _pinned_climate(scenario_template.climate, trip_ambient_k)
            scenario = scenario_template.model_copy(update={"manufacturer_profile": profile, "climate": climate})
            trip_duration_s = _ARCHETYPES[trip_type].duration_s
            result = run_scenario(
                profile,
                scenario,
                duration_s=trip_duration_s,
                target_speed_mps=_speed_from_waypoints(_ARCHETYPES[trip_type].waypoints),
                initial_state=state,
                record_interval_s=3600.0,
            )
            state = result.final_state.model_copy(update={"battery_soc": typical_end_soc})
            elapsed_s = trip_duration_s

        while elapsed_s < 86400.0:
            step_dt_s = min(_PARKED_STEP_S, 86400.0 - elapsed_s)
            hour_of_day = (elapsed_s % 86400.0) / 3600.0
            day_of_year = start_day_of_year + day_index + elapsed_s / 86400.0
            step_ambient_k = ambient_temp_k(scenario_template.climate, day_of_year, hour_of_day)
            climate = _pinned_climate(scenario_template.climate, step_ambient_k)
            scenario = scenario_template.model_copy(update={"manufacturer_profile": profile, "climate": climate})
            result = run_scenario(
                profile,
                scenario,
                duration_s=step_dt_s,
                target_speed_mps=lambda _t: 0.0,
                initial_state=state,
                record_interval_s=3600.0,
            )
            state = result.final_state.model_copy(update={"battery_soc": typical_end_soc})
            elapsed_s += step_dt_s

        assert state is not None  # every day runs at least one parked sub-step (elapsed_s starts < 86400)
        soh_trace.append(state.battery_soh)
    return soh_trace


@pytest.mark.slow
@pytest.mark.parametrize(
    "profile_yaml",
    ["nissan_leaf.yaml", "tesla_model3.yaml"],
    ids=["leaf", "model3"],
)
def test_tier2_matches_tier1_reference_within_tolerance(profile_yaml: str) -> None:
    """Parameterized over two real profiles, not the synthetic test fixture --
    Leaf (no battery coolant loop, ambient-only coupling) and Model 3 (battery
    in a multi-loop topology) chosen because they bracket the architectural
    extremes Tier-2 could plausibly be sensitive to. If a real-profile-specific
    bug exists, it's most likely to surface at one of these two ends rather
    than in the four intermediate topologies. Do not remove either case as
    "redundant" without re-reading this reasoning.

    Both YAMLs currently carry the same shared placeholder aging coefficients
    (per aging.py's "chemistry is a controlled variable" decision, so any SOH
    divergence between vehicles is attributable to thermal architecture, not
    chemistry) -- that's expected and correct here, not an oversight. This
    test validates Tier-2's fidelity to Tier-1's math for each vehicle's own
    thermal behavior, not a claim that the two profiles should age
    identically or differently from each other.

    Expect residual, nonzero error from four sources even after both
    reference-generator fixes above: phase averaging (a trip's mean/peak
    temp per phase vs. its full per-step trace -- see `_phase_aging`'s own
    Jensen's-inequality note), `apply_trip_start_offset`'s linear-offset
    approximation (exact only for a perfectly linear RC network; error
    grows with offset magnitude, and cold/hot-soak offsets here are
    +-3/+8K), `_interpolate_characterization`'s linear interpolation
    between ambient buckets (vs. the true nonlinear Arrhenius temperature
    dependence), and `relax_parked_temp_k`'s hourly (not continuous)
    quadrature. None of these are eliminated by this test passing -- they're
    why the tolerance has a floor at all. The Leaf's margin (this seed:
    0.80% relative error, well inside the 5% band) is not a fragile
    near-zero coincidence: re-run across seeds 7/55/999 (different day-type
    schedules, same profile) gives 2.96%/4.14%/1.95% relative error --
    consistently small and well-behaved, no sign flips or blowups, which is
    what you'd expect from several independent small approximation errors
    rather than one dominant term being cancelled by coincidence.
    """
    n_days = 60
    profile = load_profile(PROFILES_DIR / profile_yaml)
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
