"""Milestone E2b: Tier-1 representative-trip characterization for long-horizon
battery aging extrapolation.

Builds a `TripCharacterization` per `TripType` by running `sim.run_scenario`
(unmodified — this module calls only its existing public interface, plus
`sim.initialize_sim_state`) under a constant-ambient, archetype-specific
driving pattern, then compresses the result into per-phase aging-relevant
summaries. Phases store the calendar/cycle-aging *inputs* (mean/peak cell
temp, mean SOC, mean C-rate, throughput) rather than the run's own computed
SOH deltas, so a later Tier-2 engine can interpolate across ambient buckets
and re-derive aging for arbitrary occurrence counts instead of being locked
to the one ambient this characterization run happened to use.

Northern-Hemisphere-only: `ambient_temp_k`'s annual-cosine phase
(`ANNUAL_PEAK_DAY_OF_YEAR`) and diurnal-cosine phase (`DIURNAL_PEAK_HOUR`)
are hardcoded module constants, not derived from `Climate`, which has no
hemisphere/timezone concept. Every real `Climate` instance in this codebase
today is implicitly Northern-Hemisphere-shaped, matching the six real
reference vehicles' actual markets. Deferred: Southern Hemisphere / inverted-
seasonal climates are unsupported until `Climate` gains a hemisphere field.

`TripType.FAST_CHARGE_SESSION` characterization is explicitly out of scope
here (see `_ARCHETYPES` below and CLAUDE.md's Milestone E2e note) — it needs
a real charge-mode addition to `sim.py`'s step loop (battery absorbing
current rather than supplying it), which is a sim-loop change deserving its
own plan review, not a bolt-on to this module.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ev_thermal.aging import compute_aging_step
from ev_thermal.schema import (
    AnnualDayTypeMix,
    BatterySpec,
    ChargingProfile,
    Climate,
    LongHorizonCheckpoint,
    LongHorizonRunResult,
    ManufacturerProfile,
    Scenario,
    TripCharacterization,
    TripPhaseAging,
    TripType,
)
from ev_thermal.sim import initialize_sim_state, run_scenario

ANNUAL_PEAK_DAY_OF_YEAR = 200.0
"""Day of year (1-365.25) of the annual ambient-temperature peak -- Northern
Hemisphere summer, ~July 19. Hardcoded; see module docstring."""

DIURNAL_PEAK_HOUR = 15.0
"""Hour of day (0-24) of the daily ambient-temperature peak -- 3pm local,
the standard lag behind solar noon. Hardcoded; see module docstring."""

N_AMBIENT_BUCKETS = 4
"""Default ambient-temperature grid size per TripType per profile+climate."""


def ambient_temp_k(climate: Climate, day_of_year: float, hour_of_day: float) -> float:
    """Ambient temperature at a point in the year, per `Climate`'s sinusoidal model:
    an annual cosine plus a diurnal cosine superposed on `mean_annual_temp_k`.

    Fed only by `mean_annual_temp_k`, `seasonal_amplitude_k`, and
    `daily_amplitude_k`. `altitude_m` and `solar_irradiance_w_per_m2` are NOT
    used by this function -- the former would matter for air-density
    correction (cooling.py already documents this as unmodeled), the latter
    is for aux_compute.py's still-stubbed cabin heat-gain model.
    """
    annual_term = climate.seasonal_amplitude_k.value * math.cos(
        2.0 * math.pi * (day_of_year - ANNUAL_PEAK_DAY_OF_YEAR) / 365.25
    )
    diurnal_term = climate.daily_amplitude_k.value * math.cos(
        2.0 * math.pi * (hour_of_day - DIURNAL_PEAK_HOUR) / 24.0
    )
    return climate.mean_annual_temp_k.value + annual_term + diurnal_term


def ambient_bucket_temps_k(climate: Climate, n_buckets: int = N_AMBIENT_BUCKETS) -> list[float]:
    """`n_buckets` ambient temperatures evenly spaced across this climate's own full
    realistically-experienced range (annual AND diurnal swing), so that any day's
    actual ambient (from `ambient_temp_k`) always falls inside this grid for its own
    climate -- no extrapolation on the ambient axis needed. A separate concern from
    aging.py's own 20-40C calibrated coefficient envelope, which this does not fix.
    """
    total_swing = climate.seasonal_amplitude_k.value + climate.daily_amplitude_k.value
    low = climate.mean_annual_temp_k.value - total_swing
    high = climate.mean_annual_temp_k.value + total_swing
    if n_buckets == 1:
        return [climate.mean_annual_temp_k.value]
    step = (high - low) / (n_buckets - 1)
    return [low + i * step for i in range(n_buckets)]


def _speed_from_waypoints(waypoints: list[tuple[float, float]]) -> Callable[[float], float]:
    """Piecewise-linear interpolation over (time_s, speed_mps) waypoints."""

    def target_speed_mps(t: float) -> float:
        if t <= waypoints[0][0]:
            return waypoints[0][1]
        for (t0, v0), (t1, v1) in zip(waypoints, waypoints[1:], strict=False):
            if t0 <= t <= t1:
                if t1 == t0:
                    return v1
                fraction = (t - t0) / (t1 - t0)
                return v0 + fraction * (v1 - v0)
        return waypoints[-1][1]

    return target_speed_mps


# --- Archetype speed traces (see CLAUDE.md's Milestone E2b plan for the reasoning
# behind each trace's shape, duration, and phase split) ---

_COMMUTE_WAYPOINTS: list[tuple[float, float]] = [
    (0.0, 0.0),
    (90.0, 11.11),
    (280.0, 11.11),
    (310.0, 0.0),
    (340.0, 0.0),
    (430.0, 11.11),
    (560.0, 11.11),
    (590.0, 0.0),
    (620.0, 0.0),
    (710.0, 11.11),
    (900.0, 11.11),
    (930.0, 0.0),
    (960.0, 0.0),
    (1050.0, 11.11),
    (1140.0, 11.11),
    (1200.0, 0.0),
]
_COMMUTE_DURATION_S = 1200.0
_COMMUTE_PHASE_BOUNDARIES_S = [0.0, 280.0, 1140.0, 1200.0]
_COMMUTE_PHASE_NAMES = ["warm_up", "cruise_and_stops", "final_decel"]

_HIGHWAY_WAYPOINTS: list[tuple[float, float]] = [
    (0.0, 0.0),
    (120.0, 27.78),
    (1680.0, 27.78),
    (1800.0, 0.0),
]
_HIGHWAY_DURATION_S = 1800.0
_HIGHWAY_PHASE_BOUNDARIES_S = [0.0, 120.0, 1680.0, 1800.0]
_HIGHWAY_PHASE_NAMES = ["accel", "cruise", "decel"]

_CITY_CYCLE_WAYPOINTS: list[tuple[float, float]] = [
    (0.0, 0.0),
    (10.0, 13.89),
    (25.0, 13.89),
    (35.0, 0.0),
    (60.0, 0.0),
]
_CITY_CYCLE_S = 60.0
_CITY_N_CYCLES = 25
_CITY_DURATION_S = _CITY_CYCLE_S * _CITY_N_CYCLES
_CITY_PHASE_BOUNDARIES_S = [0.0, _CITY_DURATION_S]
_CITY_PHASE_NAMES = ["stop_and_go"]


def _city_waypoints() -> list[tuple[float, float]]:
    return [
        (cycle_index * _CITY_CYCLE_S + t, v)
        for cycle_index in range(_CITY_N_CYCLES)
        for t, v in _CITY_CYCLE_WAYPOINTS
    ]


@dataclass(frozen=True)
class _Archetype:
    waypoints: list[tuple[float, float]]
    duration_s: float
    phase_boundaries_s: list[float]
    phase_names: list[str]
    initial_soak: str  # "cold", "hot", or "none"


_ARCHETYPES: dict[TripType, _Archetype] = {
    TripType.COLD_START_COMMUTE: _Archetype(
        waypoints=_COMMUTE_WAYPOINTS,
        duration_s=_COMMUTE_DURATION_S,
        phase_boundaries_s=_COMMUTE_PHASE_BOUNDARIES_S,
        phase_names=_COMMUTE_PHASE_NAMES,
        initial_soak="cold",
    ),
    TripType.HOT_SOAK_COMMUTE: _Archetype(
        waypoints=_COMMUTE_WAYPOINTS,
        duration_s=_COMMUTE_DURATION_S,
        phase_boundaries_s=_COMMUTE_PHASE_BOUNDARIES_S,
        phase_names=_COMMUTE_PHASE_NAMES,
        initial_soak="hot",
    ),
    TripType.HIGHWAY: _Archetype(
        waypoints=_HIGHWAY_WAYPOINTS,
        duration_s=_HIGHWAY_DURATION_S,
        phase_boundaries_s=_HIGHWAY_PHASE_BOUNDARIES_S,
        phase_names=_HIGHWAY_PHASE_NAMES,
        initial_soak="none",
    ),
    TripType.CITY_STOP_AND_GO: _Archetype(
        waypoints=_city_waypoints(),
        duration_s=_CITY_DURATION_S,
        phase_boundaries_s=_CITY_PHASE_BOUNDARIES_S,
        phase_names=_CITY_PHASE_NAMES,
        initial_soak="none",
    ),
    # TripType.FAST_CHARGE_SESSION deliberately absent -- see module docstring.
}


def _phase_aging(
    phase_name: str,
    time_s: list[float],
    cell_temp_k: list[float],
    soc: list[float],
    current_a: list[float],
    capacity_ah: float,
    start_s: float,
    end_s: float,
) -> TripPhaseAging:
    """Time-weighted (not sample-count-weighted) phase summary, since adaptive dt
    means unequal sample spacing is real physics, not a decimation artifact.
    """
    weighted_temp = 0.0
    weighted_soc = 0.0
    weighted_c_rate = 0.0
    throughput_ah = 0.0
    covered_s = 0.0
    peak_temp_k = float("-inf")
    for i in range(len(time_s) - 1):
        t0, t1 = time_s[i], time_s[i + 1]
        overlap_s = min(t1, end_s) - max(t0, start_s)
        if overlap_s <= 0.0:
            continue
        temp_k = cell_temp_k[i]
        weighted_temp += temp_k * overlap_s
        weighted_soc += soc[i] * overlap_s
        weighted_c_rate += (abs(current_a[i]) / capacity_ah) * overlap_s
        throughput_ah += abs(current_a[i]) * overlap_s / 3600.0
        covered_s += overlap_s
        peak_temp_k = max(peak_temp_k, temp_k, cell_temp_k[i + 1])
    if covered_s <= 0.0:
        raise ValueError(f"phase {phase_name!r} [{start_s}, {end_s}) has no samples in the time series")
    return TripPhaseAging(
        phase_name=phase_name,
        duration_s=covered_s,
        mean_cell_temp_k=weighted_temp / covered_s,
        peak_cell_temp_k=peak_temp_k,
        mean_soc=weighted_soc / covered_s,
        mean_c_rate=weighted_c_rate / covered_s,
        throughput_ah=throughput_ah,
    )


def characterize_trip(
    profile: ManufacturerProfile,
    scenario_template: Scenario,
    trip_type: TripType,
    ambient_bucket_temp_k: float,
) -> TripCharacterization:
    """Run one Tier-1 (full-fidelity `run_scenario`) characterization for `trip_type`
    at a constant `ambient_bucket_temp_k`, for `profile`.

    `scenario_template` supplies everything a `Scenario` needs besides the
    vehicle and climate (usage/charging profile, dt policy, random seed,
    name) -- this function overrides `manufacturer_profile` to `profile` and
    pins `climate` to a constant `ambient_bucket_temp_k` (zeroing the
    seasonal/daily amplitudes), matching `run_scenario`'s existing single-
    ambient behavior exactly.
    """
    if trip_type not in _ARCHETYPES:
        raise NotImplementedError(
            f"{trip_type} characterization is deferred to Milestone E2e (needs a real "
            "charge-mode addition to sim.py's step loop) -- see CLAUDE.md."
        )
    archetype = _ARCHETYPES[trip_type]

    climate = scenario_template.climate.model_copy(
        update={
            "mean_annual_temp_k": scenario_template.climate.mean_annual_temp_k.model_copy(
                update={"value": ambient_bucket_temp_k}
            ),
            "seasonal_amplitude_k": scenario_template.climate.seasonal_amplitude_k.model_copy(
                update={"value": 0.0}
            ),
            "daily_amplitude_k": scenario_template.climate.daily_amplitude_k.model_copy(
                update={"value": 0.0}
            ),
        }
    )
    scenario = scenario_template.model_copy(update={"manufacturer_profile": profile, "climate": climate})

    base_state = initialize_sim_state(profile, scenario, scenario.random_seed)
    battery_node_name = profile.battery.thermal_node.name
    if archetype.initial_soak == "cold":
        initial_pack_temp_k = ambient_bucket_temp_k - profile.battery.cold_soak_offset_k.value
    elif archetype.initial_soak == "hot":
        initial_pack_temp_k = ambient_bucket_temp_k + profile.battery.hot_soak_offset_k.value
    else:
        initial_pack_temp_k = ambient_bucket_temp_k
    patched_temps_k = dict(base_state.node_temps_k)
    patched_temps_k[battery_node_name] = initial_pack_temp_k
    initial_state = base_state.model_copy(update={"node_temps_k": patched_temps_k})

    result = run_scenario(
        profile,
        scenario,
        duration_s=archetype.duration_s,
        target_speed_mps=_speed_from_waypoints(archetype.waypoints),
        initial_state=initial_state,
    )

    time_s = result.time_series.time_s
    cell_temp_k = result.time_series.node_temps_k[battery_node_name]
    soc = result.time_series.soc
    current_a = result.time_series.battery_current_a
    capacity_ah = profile.battery.capacity_nominal_ah.value

    phases = [
        _phase_aging(name, time_s, cell_temp_k, soc, current_a, capacity_ah, start_s, end_s)
        for name, start_s, end_s in zip(
            archetype.phase_names,
            archetype.phase_boundaries_s,
            archetype.phase_boundaries_s[1:],
            strict=False,
        )
    ]

    return TripCharacterization(
        trip_type=trip_type,
        ambient_bucket_temp_k=ambient_bucket_temp_k,
        phases=phases,
        odometer_km_delta=result.final_state.odometer_km,
        final_soc=result.final_state.battery_soc,
        final_node_temps_k=result.final_state.node_temps_k,
    )


def characterize_all_trips(
    profile: ManufacturerProfile,
    scenario_template: Scenario,
    n_buckets: int = N_AMBIENT_BUCKETS,
) -> dict[TripType, list[TripCharacterization]]:
    """Runs `characterize_trip` once per implemented `TripType` x ambient bucket,
    for `profile` under `scenario_template`'s climate. The expensive half of the
    two-tier split -- call once, reuse the result across many
    `simulate_long_horizon` calls (different day_type_mix, duration_years, or
    seed) rather than re-running Tier-1 physics per call. Excludes
    `TripType.FAST_CHARGE_SESSION` (see `characterize_trip`).
    """
    buckets = ambient_bucket_temps_k(scenario_template.climate, n_buckets)
    return {
        trip_type: [characterize_trip(profile, scenario_template, trip_type, bucket) for bucket in buckets]
        for trip_type in _ARCHETYPES
    }


DAYS_PER_YEAR = 365

TRIP_HOUR_OF_DAY = 8.0
"""All trip occurrences use a fixed 08:00 local ambient lookup for ambient-
bucket-interpolation purposes -- morning commute is the modal trip time, and
a fixed hour keeps validation deterministic. Intra-day trip-time distribution
is not modeled (a highway trip or a fast-charge session at a different hour
gets the same 08:00 lookup as a commute). Revisit if trip-time-of-day
variance is ever wanted. See CLAUDE.md's Milestone E2c note."""

_START_OF_DAY_HOUR = 12.0
"""Hour used only for Tier-2 day-0's initial pack-temperature lookup (not for
daily trip occurrences, see TRIP_HOUR_OF_DAY) -- noon, a simple, documented
choice; the day-of-year choice (see _default_start_day_of_year) is what
actually matters for landing day-0 near the climate's annual mean."""


def _default_start_day_of_year() -> float:
    """Day of year where Climate's annual cosine crosses its mean (ambient ==
    mean_annual_temp_k, ignoring the smaller diurnal term) -- a phase-derived
    'typical day' default for a long-horizon run's day 0, per Milestone E2's
    initial-conditions decision (day-0 pack temp = ambient at this day).
    """
    return ANNUAL_PEAK_DAY_OF_YEAR + 365.25 / 4.0


def shuffle_day_type_schedule(
    day_type_mix: list[AnnualDayTypeMix], n_days: int, rng: np.random.Generator
) -> list[TripType | None]:
    """Builds the exact multiset of trip-type occurrences implied by
    `day_type_mix` (not sampled -- if a TripType has occurrences_per_year=40,
    exactly 40 days per year are that type, not ~40 in expectation), pads the
    remainder of each year with `None` (parked days), and shuffles day ORDER
    within each year via `rng`.

    Deterministic under a fixed `rng` state; the RNG stream advances
    continuously across years (not reseeded per year), matching
    `SimState.rng_state`'s existing fork-safe convention.
    """
    year_template: list[TripType | None] = []
    for entry in day_type_mix:
        year_template.extend([entry.trip_type] * entry.occurrences_per_year.value)
    if len(year_template) > DAYS_PER_YEAR:
        raise ValueError(
            f"day_type_mix occurrences_per_year sum to {len(year_template)}, "
            f"more than {DAYS_PER_YEAR} days in a year"
        )
    year_template.extend([None] * (DAYS_PER_YEAR - len(year_template)))

    schedule: list[TripType | None] = []
    n_years = (n_days + DAYS_PER_YEAR - 1) // DAYS_PER_YEAR
    for _ in range(n_years):
        year = list(year_template)
        rng.shuffle(year)
        schedule.extend(year)
    return schedule[:n_days]


def relax_parked_temp_k(
    start_temp_k: float,
    tau_s: float | None,
    ambient_fn: Callable[[float], float],
    start_t_s: float,
    duration_s: float,
    step_s: float = 3600.0,
) -> list[tuple[float, float]]:
    """Exact per-interval exponential relaxation toward ambient, treating ambient
    as piecewise-constant within each `step_s` interval -- NOT a single closed-
    form solution against the full sinusoidal forcing term, which would need an
    Arrhenius-of-sinusoid integral derived for a placeholder-coefficient model.
    `T[i+1] = Ta(t_i) + (T[i] - Ta(t_i)) * exp(-dt/tau_s)` is exact within each
    interval given that approximation.

    `tau_s=None` means the node has no ambient path
    (`BatterySpec.thermal_node.resistance_to_ambient_k_per_w` is `None`) -- pack
    temp holds constant, matching that field's documented adiabatic semantics.

    Returns `[(elapsed_t_s, pack_temp_k), ...]` at each step boundary, including
    the starting point, for calendar-aging quadrature (`parked_calendar_aging`).
    """
    samples: list[tuple[float, float]] = [(start_t_s, start_temp_k)]
    if duration_s <= 0.0:
        return samples
    temp_k = start_temp_k
    elapsed_s = 0.0
    while elapsed_s < duration_s:
        dt = min(step_s, duration_s - elapsed_s)
        if tau_s is not None:
            ambient = ambient_fn(start_t_s + elapsed_s)
            temp_k = ambient + (temp_k - ambient) * math.exp(-dt / tau_s)
        elapsed_s += dt
        samples.append((start_t_s + elapsed_s, temp_k))
    return samples


def parked_calendar_aging(spec: BatterySpec, soc: float, temp_samples: list[tuple[float, float]]) -> float:
    """Sums calendar_delta (`aging.compute_aging_step`, `applied_current_a=0.0`
    so `cycle_delta` is exactly zero) across consecutive `temp_samples`
    intervals from `relax_parked_temp_k`, using each interval's start-of-
    interval temperature (left Riemann, matching `_phase_aging`'s convention).
    `soc` is held constant across the whole parked period -- no charger model
    exists yet (see CLAUDE.md's dt-policy "or charging" gap).
    """
    total = 0.0
    for (t0, temp0), (t1, _temp1) in zip(temp_samples, temp_samples[1:], strict=False):
        result = compute_aging_step(
            spec, soh=1.0, cell_temp_k=temp0, soc=soc, applied_current_a=0.0, dt_s=t1 - t0
        )
        total += result.calendar_delta
    return total


def _archetype_assumed_start_temp_k(
    trip_type: TripType, ambient_bucket_temp_k_value: float, spec: BatterySpec
) -> float:
    if trip_type is TripType.COLD_START_COMMUTE:
        return ambient_bucket_temp_k_value - spec.cold_soak_offset_k.value
    if trip_type is TripType.HOT_SOAK_COMMUTE:
        return ambient_bucket_temp_k_value + spec.hot_soak_offset_k.value
    return ambient_bucket_temp_k_value


def apply_trip_start_offset(
    characterization: TripCharacterization, actual_start_temp_k: float, spec: BatterySpec
) -> TripCharacterization:
    """Shifts every phase's mean/peak cell temp by a constant
    `(actual_start_temp_k - archetype_assumed_start_temp_k)`, so a trip
    occurrence chains from the pack temp the preceding parked period actually
    left behind, rather than always replaying the characterization's own fixed
    starting condition.

    Approximate, not exact: `thermal.py`'s RC network is linear (explicit
    Euler), so a constant initial-condition offset propagates as a constant
    offset through it -- but R0's Arrhenius term and cooling effectiveness both
    have real temperature dependence, so error grows with offset magnitude. A
    more exact alternative (characterizing each trip type at several fixed
    pack-start offsets, interpolated like ambient buckets) exists but
    multiplies characterization cost 4x -- not adopted; revisit only if
    validation shows offset error dominates (see CLAUDE.md).

    Only the battery node's `final_node_temps_k` entry is shifted (motor/
    inverter aren't affected by the battery's own soak history, and Tier-2
    doesn't track them between trips anyway -- see CLAUDE.md).
    """
    assumed_start_temp_k = _archetype_assumed_start_temp_k(
        characterization.trip_type, characterization.ambient_bucket_temp_k, spec
    )
    offset_k = actual_start_temp_k - assumed_start_temp_k
    shifted_phases = [
        phase.model_copy(
            update={
                "mean_cell_temp_k": phase.mean_cell_temp_k + offset_k,
                "peak_cell_temp_k": phase.peak_cell_temp_k + offset_k,
            }
        )
        for phase in characterization.phases
    ]
    battery_node_name = spec.thermal_node.name
    shifted_final_temps_k = dict(characterization.final_node_temps_k)
    shifted_final_temps_k[battery_node_name] = shifted_final_temps_k[battery_node_name] + offset_k
    return characterization.model_copy(update={"phases": shifted_phases, "final_node_temps_k": shifted_final_temps_k})


def _interpolate_characterization(
    characterizations: list[TripCharacterization], ambient_temp_k_value: float
) -> TripCharacterization:
    """Linearly interpolates between the two ambient-bucketed `TripCharacterization`s
    nearest `ambient_temp_k_value` (`characterizations` must be sorted ascending
    by `ambient_bucket_temp_k`, as `ambient_bucket_temps_k` already returns them).
    Clamps to the nearest edge bucket if outside the characterized range -- a
    defensive fallback, not a silent-extrapolation policy; shouldn't trigger
    given the bucket grid spans the climate's own full annual+diurnal range.
    """
    if ambient_temp_k_value <= characterizations[0].ambient_bucket_temp_k:
        return characterizations[0]
    if ambient_temp_k_value >= characterizations[-1].ambient_bucket_temp_k:
        return characterizations[-1]
    for lo, hi in zip(characterizations, characterizations[1:], strict=False):
        if not (lo.ambient_bucket_temp_k <= ambient_temp_k_value <= hi.ambient_bucket_temp_k):
            continue
        span = hi.ambient_bucket_temp_k - lo.ambient_bucket_temp_k
        fraction = 0.0 if span == 0.0 else (ambient_temp_k_value - lo.ambient_bucket_temp_k) / span
        phases = [
            TripPhaseAging(
                phase_name=lo_phase.phase_name,
                duration_s=lo_phase.duration_s + fraction * (hi_phase.duration_s - lo_phase.duration_s),
                mean_cell_temp_k=lo_phase.mean_cell_temp_k
                + fraction * (hi_phase.mean_cell_temp_k - lo_phase.mean_cell_temp_k),
                peak_cell_temp_k=lo_phase.peak_cell_temp_k
                + fraction * (hi_phase.peak_cell_temp_k - lo_phase.peak_cell_temp_k),
                mean_soc=lo_phase.mean_soc + fraction * (hi_phase.mean_soc - lo_phase.mean_soc),
                mean_c_rate=lo_phase.mean_c_rate + fraction * (hi_phase.mean_c_rate - lo_phase.mean_c_rate),
                throughput_ah=lo_phase.throughput_ah + fraction * (hi_phase.throughput_ah - lo_phase.throughput_ah),
            )
            for lo_phase, hi_phase in zip(lo.phases, hi.phases, strict=True)
        ]
        final_node_temps_k = {
            name: temp + fraction * (hi.final_node_temps_k[name] - temp) for name, temp in lo.final_node_temps_k.items()
        }
        return TripCharacterization(
            trip_type=lo.trip_type,
            ambient_bucket_temp_k=ambient_temp_k_value,
            phases=phases,
            odometer_km_delta=lo.odometer_km_delta + fraction * (hi.odometer_km_delta - lo.odometer_km_delta),
            final_soc=lo.final_soc + fraction * (hi.final_soc - lo.final_soc),
            final_node_temps_k=final_node_temps_k,
        )
    raise AssertionError("unreachable: ambient_temp_k_value within [lo, hi] but no bracketing pair matched")


def simulate_long_horizon(
    profile: ManufacturerProfile,
    trip_characterizations: dict[TripType, list[TripCharacterization]],
    day_type_mix: list[AnnualDayTypeMix],
    climate: Climate,
    charging_profile: ChargingProfile,
    duration_years: int,
    seed: int,
    start_day_of_year: float | None = None,
) -> LongHorizonRunResult:
    """Tier-2: extrapolates `trip_characterizations` (from `characterize_all_trips`,
    called once and reused -- see that function's docstring) across a
    `duration_years` horizon via day-type weighting.

    Does not run per-second physics. Per simulated day, either replays an
    ambient-interpolated, offset-corrected `TripCharacterization` (see
    `apply_trip_start_offset`) for that day's driving, or relaxes the pack
    toward ambient for the whole day (`relax_parked_temp_k`). Battery-only:
    motor/inverter are not tracked between trips (no aging model consumes
    their trajectory, and they re-equilibrate to near-ambient within minutes
    of any real parked period -- see CLAUDE.md).

    SOC is topped up instantaneously to `charging_profile.typical_end_soc`
    after every trip day (Tier-2 doesn't model charge-session physics at
    phase level) and held constant through parked periods (no charger model
    exists yet).
    """
    spec = profile.battery
    battery_node_name = spec.thermal_node.name
    tau_s = (
        None
        if spec.thermal_node.resistance_to_ambient_k_per_w is None
        else spec.thermal_node.resistance_to_ambient_k_per_w.value * spec.thermal_node.capacitance_j_per_k.value
    )

    day0 = start_day_of_year if start_day_of_year is not None else _default_start_day_of_year()
    pack_temp_k = ambient_temp_k(climate, day0, _START_OF_DAY_HOUR)
    soc = charging_profile.typical_start_soc.value
    soh = 1.0
    odometer_km = 0.0
    calendar_loss_cumulative = 0.0
    cycle_loss_cumulative = 0.0

    def ambient_fn(elapsed_s: float) -> float:
        day_of_year = day0 + elapsed_s / 86400.0
        hour_of_day = (elapsed_s % 86400.0) / 3600.0
        return ambient_temp_k(climate, day_of_year, hour_of_day)

    rng = np.random.default_rng(seed)
    schedule = shuffle_day_type_schedule(day_type_mix, duration_years * DAYS_PER_YEAR, rng)

    checkpoints: list[LongHorizonCheckpoint] = []
    for day_index, trip_type in enumerate(schedule):
        day_start_s = day_index * 86400.0
        if trip_type is not None:
            day_ambient_k = ambient_fn(day_start_s + TRIP_HOUR_OF_DAY * 3600.0)
            characterization = _interpolate_characterization(trip_characterizations[trip_type], day_ambient_k)
            characterization = apply_trip_start_offset(characterization, pack_temp_k, spec)

            for phase in characterization.phases:
                current_a = phase.mean_c_rate * spec.capacity_nominal_ah.value
                result = compute_aging_step(
                    spec,
                    soh=soh,
                    cell_temp_k=phase.mean_cell_temp_k,
                    soc=phase.mean_soc,
                    applied_current_a=current_a,
                    dt_s=phase.duration_s,
                )
                soh = result.new_soh
                calendar_loss_cumulative += result.calendar_delta
                cycle_loss_cumulative += result.cycle_delta

            odometer_km += characterization.odometer_km_delta
            soc = charging_profile.typical_end_soc.value
            pack_temp_k = characterization.final_node_temps_k[battery_node_name]
            trip_duration_s = sum(phase.duration_s for phase in characterization.phases)
            parked_duration_s = max(0.0, 86400.0 - trip_duration_s)
            parked_start_s = day_start_s + trip_duration_s
        else:
            parked_duration_s = 86400.0
            parked_start_s = day_start_s

        temp_samples = relax_parked_temp_k(pack_temp_k, tau_s, ambient_fn, parked_start_s, parked_duration_s)
        parked_loss = parked_calendar_aging(spec, soc, temp_samples)
        calendar_loss_cumulative += parked_loss
        soh = max(0.0, soh - parked_loss)
        pack_temp_k = temp_samples[-1][1]

        checkpoints.append(
            LongHorizonCheckpoint(
                day_index=day_index,
                battery_soh=soh,
                calendar_soh_loss_cumulative=calendar_loss_cumulative,
                cycle_soh_loss_cumulative=cycle_loss_cumulative,
                battery_soc=soc,
                pack_cell_temp_k=pack_temp_k,
                odometer_km=odometer_km,
            )
        )

    all_characterizations = [c for per_type in trip_characterizations.values() for c in per_type]
    return LongHorizonRunResult(checkpoints=checkpoints, trip_characterizations=all_characterizations)
