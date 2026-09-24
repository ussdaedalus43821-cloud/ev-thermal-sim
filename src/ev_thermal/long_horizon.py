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

from ev_thermal.schema import (
    Climate,
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
