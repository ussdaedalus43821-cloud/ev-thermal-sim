"""Milestones E2b/E2c/E2e: Tier-1 representative-trip characterization and
Tier-2 long-horizon extrapolation for battery aging.

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

`TripType.FAST_CHARGE_SESSION` characterization (Milestone E2e) is NOT
handled by `characterize_trip`/`_ARCHETYPES` — it needs `sim.
run_charging_session`'s charging-mode pipeline, not `run_scenario`'s driving
one, so it has its own `characterize_fast_charge_session` function, which
produces the same `TripCharacterization` shape and is merged into
`characterize_all_trips`'s output alongside the four driving archetypes.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from ev_thermal.aging import compute_aging_step
from ev_thermal.battery import ocv_v as battery_ocv_v
from ev_thermal.controller import DUTY_RAMP_SPAN_K
from ev_thermal.schema import (
    AnnualDayTypeMix,
    BatterySpec,
    ChargingProfile,
    Climate,
    CoolingLoop,
    LongHorizonCheckpoint,
    LongHorizonRunResult,
    ManufacturerProfile,
    Scenario,
    TripCharacterization,
    TripPhaseAging,
    TripType,
)
from ev_thermal.sim import coolant_node_name, initialize_sim_state, run_charging_session, run_scenario

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


# --- Archetype speed traces (see docs/MILESTONES.md's Milestone E2b plan for the reasoning
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


def _degenerate_phase(
    phase_name: str, time_s: list[float], cell_temp_k: list[float], soc: list[float], current_a: list[float],
    capacity_ah: float, at_s: float,
) -> TripPhaseAging:
    """A zero-duration `TripPhaseAging` built directly from the sample nearest
    `at_s`, for when a real phase boundary doesn't exist (see
    `characterize_fast_charge_session`'s docstring). Deliberately NOT routed
    through `_phase_aging`, which correctly raises on an empty time window --
    that check should keep catching a genuinely empty time series elsewhere,
    not be loosened to silently accept this specific, legitimate degenerate
    case.
    """
    index = min(range(len(time_s)), key=lambda i: abs(time_s[i] - at_s))
    return TripPhaseAging(
        phase_name=phase_name,
        duration_s=0.0,
        mean_cell_temp_k=cell_temp_k[index],
        peak_cell_temp_k=cell_temp_k[index],
        mean_soc=soc[index],
        mean_c_rate=abs(current_a[index]) / capacity_ah,
        throughput_ah=0.0,
    )


def _pinned_climate(base_climate: Climate, ambient_bucket_temp_k: float) -> Climate:
    """A copy of `base_climate` pinned to a constant `ambient_bucket_temp_k`
    (seasonal/daily amplitudes zeroed) -- matches `run_scenario`'s existing
    single-ambient behavior exactly. Shared by `characterize_trip` and
    `characterize_fast_charge_session` so the pinning logic can't diverge
    between the two.
    """
    return base_climate.model_copy(
        update={
            "mean_annual_temp_k": base_climate.mean_annual_temp_k.model_copy(update={"value": ambient_bucket_temp_k}),
            "seasonal_amplitude_k": base_climate.seasonal_amplitude_k.model_copy(update={"value": 0.0}),
            "daily_amplitude_k": base_climate.daily_amplitude_k.model_copy(update={"value": 0.0}),
        }
    )


def _soak_patched_temps_k(
    base_temps_k: dict[str, float],
    battery_node_name: str,
    initial_pack_temp_k: float,
    cooling_loops: list[CoolingLoop],
) -> dict[str, float]:
    """Applies a hot/cold-soak initial pack temperature to the battery node
    AND to any coolant node(s) serving it -- fixed alongside Q-0021 (see
    `characterize_trip`'s own docstring for the full reasoning: a hot-soaked
    vehicle's coolant loop has been sitting in the same heat, not just the
    pack, and using anything but the pack's own temperature would invent a
    new offset this project has no basis for). Motor/inverter are
    deliberately untouched, matching `apply_trip_start_offset`'s own
    precedent (Tier-2 doesn't track them between trips at all).
    """
    patched = dict(base_temps_k)
    patched[battery_node_name] = initial_pack_temp_k
    for loop in cooling_loops:
        if battery_node_name in loop.serves:
            patched[coolant_node_name(loop.name)] = initial_pack_temp_k
    return patched


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

    The hot/cold-soak offset is applied to the battery's serving coolant
    node(s) too, not just the battery cell itself -- see `_soak_patched_
    temps_k`'s own docstring for why. Before this fix (Q-0021's own
    follow-up), a HOT_SOAK_COMMUTE trip started with a pre-heated cell but
    coolant still at the pinned climate's own ambient, so a coolant-gated
    mechanism (the chiller's second-stage duty, or the radiator/fan ramp)
    couldn't see the soak condition at all during a short trip's duration.
    """
    if trip_type not in _ARCHETYPES:
        raise NotImplementedError(
            f"{trip_type} is not a driving archetype -- use characterize_fast_charge_session "
            "for TripType.FAST_CHARGE_SESSION, which needs its own charging-mode pipeline."
        )
    archetype = _ARCHETYPES[trip_type]

    climate = _pinned_climate(scenario_template.climate, ambient_bucket_temp_k)
    scenario = scenario_template.model_copy(update={"manufacturer_profile": profile, "climate": climate})

    base_state = initialize_sim_state(profile, scenario, scenario.random_seed)
    battery_node_name = profile.battery.thermal_node.name
    if archetype.initial_soak == "cold":
        initial_pack_temp_k = ambient_bucket_temp_k - profile.battery.cold_soak_offset_k.value
    elif archetype.initial_soak == "hot":
        initial_pack_temp_k = ambient_bucket_temp_k + profile.battery.hot_soak_offset_k.value
    else:
        initial_pack_temp_k = ambient_bucket_temp_k
    patched_temps_k = _soak_patched_temps_k(
        base_state.node_temps_k,
        battery_node_name,
        initial_pack_temp_k,
        profile.thermal_architecture.cooling_loops,
    )
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


FAST_CHARGE_SESSION_DURATION_S = 1800.0
"""TODO(spec-sourcing): placeholder DC fast-charge session length, same
order of magnitude as the HIGHWAY archetype's duration -- a typical real
fast-charge stop."""

FAST_CHARGE_SOC_KNEE = 0.6
"""Matches BatterySpec.charge_current_limit_curve's placeholder knee -- the
SOC at which characterize_fast_charge_session draws the ramp/taper phase
boundary. Kept as its own named constant (not read from the curve itself)
since a characterization function shouldn't reach into a specific profile's
curve shape to decide its own phase structure -- the boundary is a modeling
choice about where "ramp" ends and "taper" begins, not a per-profile value."""


def _fast_charge_phases(
    time_s: list[float],
    cell_temp_k: list[float],
    soc: list[float],
    current_a: list[float],
    capacity_ah: float,
    session_duration_s: float,
) -> tuple[TripPhaseAging, TripPhaseAging]:
    """Splits a charging session's time series into (`ramp`, `taper`) at the
    first sample where `soc >= FAST_CHARGE_SOC_KNEE`, with explicit handling
    for the two edge cases where no real crossing exists -- see
    `characterize_fast_charge_session`'s docstring for the full reasoning.
    Factored out from that function so these edge cases are directly unit-
    testable against hand-crafted SOC traces, not only incidentally
    exercised by whichever ambient buckets a real physics run happens to
    produce.
    """
    boundary_index = next((i for i, s in enumerate(soc) if s >= FAST_CHARGE_SOC_KNEE), None)

    if boundary_index is None:
        # never crossed the knee: "ramp" covers the whole session, "taper" degenerate at the end
        ramp = _phase_aging("ramp", time_s, cell_temp_k, soc, current_a, capacity_ah, 0.0, session_duration_s)
        taper = _degenerate_phase("taper", time_s, cell_temp_k, soc, current_a, capacity_ah, session_duration_s)
    elif boundary_index == 0:
        # already past the knee at t=0: "ramp" degenerate at the start, "taper" covers the whole session
        ramp = _degenerate_phase("ramp", time_s, cell_temp_k, soc, current_a, capacity_ah, 0.0)
        taper = _phase_aging("taper", time_s, cell_temp_k, soc, current_a, capacity_ah, 0.0, session_duration_s)
    else:
        boundary_s = time_s[boundary_index]
        ramp = _phase_aging("ramp", time_s, cell_temp_k, soc, current_a, capacity_ah, 0.0, boundary_s)
        taper = _phase_aging(
            "taper", time_s, cell_temp_k, soc, current_a, capacity_ah, boundary_s, session_duration_s
        )
    return ramp, taper


def characterize_fast_charge_session(
    profile: ManufacturerProfile,
    scenario_template: Scenario,
    ambient_bucket_temp_k: float,
) -> TripCharacterization:
    """Run one Tier-1 (full-fidelity `sim.run_charging_session`) characterization
    for a DC fast-charge session at a constant `ambient_bucket_temp_k`, for
    `profile`. Peer to `characterize_trip`, NOT a variant of it -- charging
    needs `run_charging_session`, not `run_scenario` -- but produces the SAME
    `TripCharacterization` shape, so `simulate_long_horizon`/`apply_trip_
    start_offset` need zero changes to consume it. NOT registered in
    `_ARCHETYPES`/dispatched via `characterize_trip`, since it needs its own
    charging-mode pipeline.

    Session start SOC comes from `charging_profile.typical_start_soc` via
    `initialize_sim_state` (unchanged, no separate patching needed here --
    that function already reads it). `typical_end_soc` is NOT used as a
    stopping condition: the session runs for the fixed `FAST_CHARGE_SESSION_
    DURATION_S` regardless of what SOC it reaches (matching `run_charging_
    session`'s own fixed-duration convention) -- if the placeholder taper
    curve means a session doesn't reach a realistic end SOC in that fixed
    duration, that's diagnostic information for the caller, not something
    silently papered over here.

    **Arrival-temp approximation, stated explicitly, not claimed as
    "correct":** a real fast-charge session arrives with the pack elevated
    by the preceding drive, not at ambient. This characterization assumes
    ambient-bucket start temp for consistency with every other archetype's
    linear-offset replay scheme; the real arrival-temp effect is absorbed by
    `apply_trip_start_offset` at replay time and is therefore approximate.
    Revisit if offset error is shown to dominate the fast-charge thermal
    signature specifically.

    **Phases (`ramp`, `taper`) are SOC-triggered at `FAST_CHARGE_SOC_KNEE`,
    not time-fixed like every other archetype.** SOC is monotonically
    non-decreasing across a charging session (`advance_charging_step` always
    requests negative/charging current; the applied magnitude only ever
    shrinks, never flips sign, since every ceiling in `available_charge_
    current_a` is non-negative by construction), so a knee-crossing, if it
    exists, exists exactly once. `phases` ALWAYS has exactly 2 entries
    regardless of whether a real crossing occurs -- `_interpolate_
    characterization` zips two characterizations' phase lists with
    `strict=True`, so a variable phase count per ambient bucket would crash
    bucket interpolation the moment two adjacent buckets disagreed on
    whether a session crosses the knee (plausible: colder buckets charge
    more slowly), not just break a single session. When the session starts
    at/above the knee, `ramp` is a `_degenerate_phase` built from the `t=0`
    sample. When the session never reaches the knee in `duration_s`, `taper`
    is a `_degenerate_phase` built from the `t=duration_s` (final) sample.
    """
    climate = _pinned_climate(scenario_template.climate, ambient_bucket_temp_k)
    scenario = scenario_template.model_copy(update={"manufacturer_profile": profile, "climate": climate})

    base_state = initialize_sim_state(profile, scenario, scenario.random_seed)
    battery_node_name = profile.battery.thermal_node.name
    patched_temps_k = dict(base_state.node_temps_k)
    patched_temps_k[battery_node_name] = ambient_bucket_temp_k
    initial_state = base_state.model_copy(update={"node_temps_k": patched_temps_k})

    result = run_charging_session(
        profile,
        scenario,
        duration_s=FAST_CHARGE_SESSION_DURATION_S,
        charge_power_request_w=scenario.charging_profile.dc_charge_power_kw.value * 1000.0,
        initial_state=initial_state,
    )

    time_s = result.time_series.time_s
    cell_temp_k = result.time_series.node_temps_k[battery_node_name]
    soc = result.time_series.soc
    current_a = result.time_series.battery_current_a
    capacity_ah = profile.battery.capacity_nominal_ah.value

    ramp, taper = _fast_charge_phases(
        time_s, cell_temp_k, soc, current_a, capacity_ah, FAST_CHARGE_SESSION_DURATION_S
    )

    return TripCharacterization(
        trip_type=TripType.FAST_CHARGE_SESSION,
        ambient_bucket_temp_k=ambient_bucket_temp_k,
        phases=[ramp, taper],
        odometer_km_delta=0.0,
        final_soc=result.final_state.battery_soc,
        final_node_temps_k=result.final_state.node_temps_k,
    )


def characterize_all_trips(
    profile: ManufacturerProfile,
    scenario_template: Scenario,
    n_buckets: int = N_AMBIENT_BUCKETS,
) -> dict[TripType, list[TripCharacterization]]:
    """Runs `characterize_trip` once per implemented `TripType` x ambient bucket,
    plus `characterize_fast_charge_session` once per ambient bucket, for
    `profile` under `scenario_template`'s climate. The expensive half of the
    two-tier split -- call once, reuse the result across many
    `simulate_long_horizon` calls (different day_type_mix, duration_years, or
    seed) rather than re-running Tier-1 physics per call.
    """
    buckets = ambient_bucket_temps_k(scenario_template.climate, n_buckets)
    result = {
        trip_type: [characterize_trip(profile, scenario_template, trip_type, bucket) for bucket in buckets]
        for trip_type in _ARCHETYPES
    }
    result[TripType.FAST_CHARGE_SESSION] = [
        characterize_fast_charge_session(profile, scenario_template, bucket) for bucket in buckets
    ]
    return result


DAYS_PER_YEAR = 365

TRIP_HOUR_OF_DAY = 8.0
"""All trip occurrences use a fixed 08:00 local ambient lookup for ambient-
bucket-interpolation purposes -- morning commute is the modal trip time, and
a fixed hour keeps validation deterministic. Intra-day trip-time distribution
is not modeled (a highway trip or a fast-charge session at a different hour
gets the same 08:00 lookup as a commute). Revisit if trip-time-of-day
variance is ever wanted. See docs/MILESTONES.md's Milestone E2c note."""

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


def _chiller_duty(temp_k: float, thermostat_full_open_temp_k: float) -> float:
    """Second-stage ramp, mirroring controller.py's commanded_chiller_duty
    exactly (same threshold, thermostat_full_open_temp_k, and the same
    DUTY_RAMP_SPAN_K span) -- see that module's docstring for why this is a
    genuine second stage, not a scaled copy of the coolant-path duty above.
    """
    return min(1.0, max(0.0, (temp_k - thermostat_full_open_temp_k) / DUTY_RAMP_SPAN_K))


def _refrigerant_duty(temp_k: float, activation_temp_k: float, full_duty_temp_k: float) -> float:
    """Mirrors controller.py's commanded_refrigerant_duty exactly (Milestone
    D) -- unlike `_chiller_duty` above, the span comes from the loop's own
    two real setpoints, not the shared DUTY_RAMP_SPAN_K constant (a
    RefrigerantLoop's control span is defined by its own hardware, not the
    CoolingLoop tuning knob -- see controller.py's own docstring).
    """
    return min(1.0, max(0.0, (temp_k - activation_temp_k) / (full_duty_temp_k - activation_temp_k)))


def relax_parked_temp_k(
    start_temp_k: float,
    tau_s: float | None,
    ambient_fn: Callable[[float], float],
    start_t_s: float,
    duration_s: float,
    step_s: float = 3600.0,
    capacitance_j_per_k: float | None = None,
    coolant_resistance_k_per_w: float | None = None,
    thermostat_temp_k: float | None = None,
    thermostat_full_open_temp_k: float | None = None,
    chiller_present: bool = False,
    chiller_capacity_w: float | None = None,
    refrigerant_present: bool = False,
    refrigerant_resistance_k_per_w: float | None = None,
    refrigerant_activation_temp_k: float | None = None,
    refrigerant_full_duty_temp_k: float | None = None,
) -> list[tuple[float, float]]:
    """Exact per-interval exponential relaxation toward ambient, treating ambient
    as piecewise-constant within each `step_s` interval -- NOT a single closed-
    form solution against the full sinusoidal forcing term, which would need an
    Arrhenius-of-sinusoid integral derived for a placeholder-coefficient model.
    `T[i+1] = Ta(t_i) + (T[i] - Ta(t_i)) * exp(-dt/tau_s)` is exact within each
    interval given that approximation.

    `tau_s=None` means the node has no direct ambient path
    (`BatterySpec.thermal_node.resistance_to_ambient_k_per_w` is `None`) -- pack
    temp holds constant via that path, matching that field's documented
    adiabatic semantics (unless a coolant path is also given, below).

    ## Coolant-loop coupling while parked (see docs/DESIGN_QUESTIONS.md Q-0020)

    When `coolant_resistance_k_per_w`, `thermostat_temp_k`, and
    `thermostat_full_open_temp_k` are ALL given (along with
    `capacitance_j_per_k`, needed to recombine resistances into an effective
    tau each step), the battery's pack temperature also relaxes through a
    coolant-mediated path in PARALLEL with the direct ambient path -- reusing
    the SAME linear thermostat ramp `cooling.py`'s real per-second model
    already uses (0% engaged at `thermostat_temp_k`, 100% at
    `thermostat_full_open_temp_k`), not a hard on/off switch or a new
    placeholder. `coolant_resistance_k_per_w` is the SERIES battery-to-
    coolant-to-radiator-to-ambient resistance (`resistance_to_coolant_k_per_w
    + 1/radiator_ua_w_per_k`) -- the coolant node itself is treated as
    quasi-steady (no separate thermal mass tracked), a coarser approximation
    than the full per-second model, consistent with this function's existing
    piecewise-constant-ambient simplification. Recomputed every step (not
    once for the whole call), since `duty` depends on the CURRENT pack temp.
    If any of the four coolant-path arguments is omitted, behavior is
    byte-identical to the pre-Q-0020 ambient-only function -- every existing
    call site that doesn't pass them is unaffected.

    ## Chiller (see docs/DESIGN_QUESTIONS.md Q-0021)

    `chiller_present`/`chiller_capacity_w` require `coolant_path_active`
    (a chiller sits on the coolant loop, same as `cooling.py`'s real model)
    -- ignored otherwise. Deliberately NOT another parallel conductance term
    like the coolant path above: a conductance can only ever speed
    convergence TOWARD ambient, never past it, which is exactly the
    limitation a real chiller exists to overcome (see cooling.py's module
    docstring and Q-0021's own context). Modeled instead as a constant
    (duty-scaled) heat-extraction rate Q, which shifts the interval's
    exponential-relaxation TARGET itself: for an RC system with ambient
    coupling conductance G and a constant applied sink Q, the steady state
    is `ambient - Q/G`, not `ambient` -- allowing the pack to settle below
    ambient when the chiller is engaged, matching the constant-heat-input
    treatment cooling.py's real per-second model already uses (chiller_
    heat_removed_w applied as a heat_inputs_w sink, not folded into any
    conductance there either). Duty is `_chiller_duty` (second-stage ramp
    against `thermostat_full_open_temp_k`, same threshold and span as
    controller.py's `commanded_chiller_duty` -- see that function). Since
    `thermostat_full_open_temp_k > thermostat_temp_k` always, the coolant
    conductance term is already saturated (duty=1.0, G>0) by the time the
    chiller's own duty becomes positive -- G is never zero while the
    chiller is actually contributing, so no separate G=0 case is needed
    here (unlike the ambient-only branch below, which already handles
    tau_s=None on its own).

    ## Refrigerant loop (Milestone D)

    `refrigerant_present`/`refrigerant_resistance_k_per_w`/`refrigerant_
    activation_temp_k`/`refrigerant_full_duty_temp_k` model a DX
    `RefrigerantLoop` as a conductance to a FIXED virtual sink held at
    `refrigerant_activation_temp_k` (not ambient), gated by duty --
    `refrigerant_resistance_k_per_w / duty` is that conductance's
    resistance, reusing `BatterySpec.thermal_node.resistance_to_coolant_k_
    per_w` (the SAME field a `CoolingLoop`-served component's coolant edge
    uses in the real per-second model -- see refrigerant.step_refrigerant_
    loop's own docstring for why this reuse, not a new field, is correct).
    This combines with the ambient path exactly like two parallel resistors
    to a weighted-average target: `target_k = (G_ambient*ambient +
    G_refrigerant*activation_temp_k) / (G_ambient+G_refrigerant)`. An
    earlier version of this branch instead borrowed G from the (deliberately
    weak) ambient path itself, matching the chiller's own coolant-path-
    conductance trick -- found wrong while validating Milestone D, not a
    stylistic preference: a DX loop's real resistance-to-component coupling
    has nothing to do with how weak its passive ambient leak is, and
    reusing the ambient path's G produced an absurd implied target (the
    ambient path's G is orders of magnitude smaller than what a real
    compressor's resistance-limited transfer implies), overshooting the
    pack far below its own activation_temp_k before duty could react. This
    conductance-based version has no such failure mode: with a realistic
    resistance_to_coolant_k_per_w, `refrigerant_capacity_w`'s own ceiling
    essentially never binds (confirmed: a modest 20K differential over a
    real ~0.3 K/W resistance implies ~67W, far below a real compressor's
    multi-kW rating), so this Tier-2 approximation deliberately does NOT
    also cap by capacity_w -- a documented simplification, not an
    oversight, consistent with this function's existing coarser-than-Tier-1
    treatment elsewhere. Only engages in the branch that would otherwise be
    plain ambient-only relaxation (`coolant_path_active` is False) -- a
    profile is never expected to have both a `CoolingLoop`-based chiller
    AND a `RefrigerantLoop` serving the same battery. Duty is
    `_refrigerant_duty` (mirrors `_chiller_duty`'s shape but against the
    loop's own two setpoints, not `DUTY_RAMP_SPAN_K` -- see that function
    and controller.py's own docstring).

    Returns `[(elapsed_t_s, pack_temp_k), ...]` at each step boundary, including
    the starting point, for calendar-aging quadrature (`parked_calendar_aging`).
    """
    coolant_path_active = (
        coolant_resistance_k_per_w is not None
        and thermostat_temp_k is not None
        and thermostat_full_open_temp_k is not None
        and capacitance_j_per_k is not None
    )
    chiller_active = chiller_present and chiller_capacity_w is not None and coolant_path_active
    refrigerant_active = (
        refrigerant_present
        and refrigerant_resistance_k_per_w is not None
        and refrigerant_activation_temp_k is not None
        and refrigerant_full_duty_temp_k is not None
        and capacitance_j_per_k is not None
        and not coolant_path_active
    )
    samples: list[tuple[float, float]] = [(start_t_s, start_temp_k)]
    if duration_s <= 0.0:
        return samples
    temp_k = start_temp_k
    elapsed_s = 0.0
    while elapsed_s < duration_s:
        dt = min(step_s, duration_s - elapsed_s)
        ambient = ambient_fn(start_t_s + elapsed_s)
        if coolant_path_active:
            assert capacitance_j_per_k is not None  # narrows for mypy; coolant_path_active already guarantees this
            assert coolant_resistance_k_per_w is not None
            assert thermostat_temp_k is not None
            assert thermostat_full_open_temp_k is not None
            duty = min(1.0, max(0.0, (temp_k - thermostat_temp_k) / (thermostat_full_open_temp_k - thermostat_temp_k)))
            ambient_conductance_w_per_k = 0.0 if tau_s is None else capacitance_j_per_k / tau_s
            coolant_conductance_w_per_k = duty / coolant_resistance_k_per_w
            total_conductance_w_per_k = ambient_conductance_w_per_k + coolant_conductance_w_per_k
            if total_conductance_w_per_k > 0.0:
                effective_tau_s = capacitance_j_per_k / total_conductance_w_per_k
                target_k = ambient
                if chiller_active:
                    assert chiller_capacity_w is not None  # narrows for mypy; chiller_active already guarantees this
                    chiller_heat_removed_w = chiller_capacity_w * _chiller_duty(temp_k, thermostat_full_open_temp_k)
                    target_k = ambient - chiller_heat_removed_w / total_conductance_w_per_k
                temp_k = target_k + (temp_k - target_k) * math.exp(-dt / effective_tau_s)
        elif tau_s is not None:
            if refrigerant_active:
                assert capacitance_j_per_k is not None  # narrows for mypy; refrigerant_active already guarantees this
                assert refrigerant_resistance_k_per_w is not None
                assert refrigerant_activation_temp_k is not None
                assert refrigerant_full_duty_temp_k is not None
                duty = _refrigerant_duty(temp_k, refrigerant_activation_temp_k, refrigerant_full_duty_temp_k)
                ambient_conductance_w_per_k = capacitance_j_per_k / tau_s
                refrigerant_conductance_w_per_k = 0.0 if duty <= 0.0 else duty / refrigerant_resistance_k_per_w
                total_conductance_w_per_k = ambient_conductance_w_per_k + refrigerant_conductance_w_per_k
                if total_conductance_w_per_k > 0.0:
                    effective_tau_s = capacitance_j_per_k / total_conductance_w_per_k
                    target_k = (
                        ambient_conductance_w_per_k * ambient
                        + refrigerant_conductance_w_per_k * refrigerant_activation_temp_k
                    ) / total_conductance_w_per_k
                    temp_k = target_k + (temp_k - target_k) * math.exp(-dt / effective_tau_s)
            else:
                temp_k = ambient + (temp_k - ambient) * math.exp(-dt / tau_s)
        elapsed_s += dt
        samples.append((start_t_s + elapsed_s, temp_k))
    return samples


def parked_calendar_aging(
    spec: BatterySpec,
    soc_samples: list[tuple[float, float]],
    temp_samples: list[tuple[float, float]],
) -> float:
    """Sums calendar_delta (`aging.compute_aging_step`, `applied_current_a=0.0`
    so `cycle_delta` is exactly zero) across consecutive intervals, using each
    interval's start-of-interval SOC and temperature (left Riemann, matching
    `_phase_aging`'s convention). `soc_samples` and `temp_samples` MUST share
    the same time axis (same `start_t_s`/`duration_s`/`step_s`, see
    `_parked_soc_trajectory`/`relax_parked_temp_k`) -- `strict=True` below
    catches a length mismatch immediately rather than silently misaligning
    SOC and temperature within an interval.

    Milestone "parked-SOC model" (see docs/DESIGN_QUESTIONS.md Q-0012):
    `soc` is no longer held constant across the whole parked period -- the
    caller now passes a real per-hour trajectory from
    `_parked_soc_trajectory`, replacing the earlier single-scalar `soc`
    parameter that produced the SOC-snapping severity documented in
    docs/MILESTONES.md.
    """
    total = 0.0
    for (soc_t0, soc0), (soc_t1, _soc1), (_temp_t0, temp0) in zip(
        soc_samples[:-1], soc_samples[1:], temp_samples[:-1], strict=True
    ):
        result = compute_aging_step(
            spec, soh=1.0, cell_temp_k=temp0, soc=soc0, applied_current_a=0.0, dt_s=soc_t1 - soc_t0
        )
        total += result.calendar_delta
    return total


def _pump_soc_decrement(
    temp_samples: list[tuple[float, float]],
    charge_event_s: float,
    thermostat_temp_k: float,
    thermostat_full_open_temp_k: float,
    pump_decay_per_hour_at_full_duty: float,
) -> float:
    """Sums the coolant pump's SOC draw (per Q-0020's energy-cost-consistency
    requirement -- modeling the parked-cooling-loop's thermal benefit without
    charging its power draw would repeat the exact shortcut Q-0004 already
    rejected for preconditioning) over intervals AFTER the day's charge-reset
    event only. Intervals at or before the reset get wiped by
    `_parked_soc_trajectory`'s own hard reset to `typical_end_soc` regardless
    of how much they drained, so counting their pump draw here would double-
    subtract drain the reset already erases -- `t0 >= charge_event_s` mirrors
    that function's own `t_before < charge_event_s <= t_after` reset-step
    condition exactly (the reset step and everything before it excluded).
    Left-Riemann (interval-start temp for duty), matching
    `parked_calendar_aging`'s convention.
    """
    total = 0.0
    for (t0, temp0), (t1, _temp1) in zip(temp_samples[:-1], temp_samples[1:], strict=True):
        if t0 < charge_event_s:
            continue
        duty = min(1.0, max(0.0, (temp0 - thermostat_temp_k) / (thermostat_full_open_temp_k - thermostat_temp_k)))
        total += duty * pump_decay_per_hour_at_full_duty * ((t1 - t0) / 3600.0)
    return total


def _chiller_soc_decrement(
    temp_samples: list[tuple[float, float]],
    charge_event_s: float,
    thermostat_full_open_temp_k: float,
    chiller_decay_per_hour_at_full_duty: float,
) -> float:
    """Chiller's own electrical draw during a parked period (Q-0021), same
    treatment and same reset-step exclusion as `_pump_soc_decrement` (see
    that function's docstring) -- the two are separate sums, not merged,
    since they use different duty ramps (`_chiller_duty`, not the pump's).
    """
    total = 0.0
    for (t0, temp0), (t1, _temp1) in zip(temp_samples[:-1], temp_samples[1:], strict=True):
        if t0 < charge_event_s:
            continue
        duty = _chiller_duty(temp0, thermostat_full_open_temp_k)
        total += duty * chiller_decay_per_hour_at_full_duty * ((t1 - t0) / 3600.0)
    return total


def _refrigerant_soc_decrement(
    temp_samples: list[tuple[float, float]],
    charge_event_s: float,
    activation_temp_k: float,
    full_duty_temp_k: float,
    refrigerant_decay_per_hour_at_full_duty: float,
) -> float:
    """RefrigerantLoop compressor's own electrical draw during a parked period
    (Milestone D), same treatment/reset-step exclusion as
    `_chiller_soc_decrement` -- a separate sum since it uses `_refrigerant_
    duty`, not `_chiller_duty`.
    """
    total = 0.0
    for (t0, temp0), (t1, _temp1) in zip(temp_samples[:-1], temp_samples[1:], strict=True):
        if t0 < charge_event_s:
            continue
        duty = _refrigerant_duty(temp0, activation_temp_k, full_duty_temp_k)
        total += duty * refrigerant_decay_per_hour_at_full_duty * ((t1 - t0) / 3600.0)
    return total


def _parked_soc_trajectory(
    start_soc: float,
    start_t_s: float,
    duration_s: float,
    charge_event_s: float,
    decay_per_hour: float,
    typical_end_soc: float,
    step_s: float = 3600.0,
) -> list[tuple[float, float]]:
    """Piecewise-linear SOC decay through a parked period, with exactly one
    charge event (an instantaneous reset to `typical_end_soc`) applied at the
    step boundary that crosses `charge_event_s` (an absolute sim-time, always
    within this call's `[start_t_s, start_t_s + duration_s)` window in
    practice -- `simulate_long_horizon` never calls this across more than one
    calendar day). No charge-session physics (power caps, taper, thermal
    coupling) -- only the SHAPE of the daily SOC trajectory matters for
    calendar aging's SOC-sensitivity term. See docs/DESIGN_QUESTIONS.md
    Q-0012.

    Same step_s/sampling convention as `relax_parked_temp_k` (matching
    timestamps, required by `parked_calendar_aging`'s `strict=True` zip) --
    the charge event is snapped to the nearest step boundary at or after
    `charge_event_s`, not sampled at its exact instant, trading a sub-hour
    timing error (negligible next to every other approximation already in
    this model) for guaranteed alignment with `temp_samples`.

    Returns `[(elapsed_t_s, soc), ...]` at each step boundary, including the
    starting point, mirroring `relax_parked_temp_k`'s own return shape.
    """
    samples: list[tuple[float, float]] = [(start_t_s, start_soc)]
    if duration_s <= 0.0:
        return samples
    soc = start_soc
    elapsed_s = 0.0
    while elapsed_s < duration_s:
        dt = min(step_s, duration_s - elapsed_s)
        t_before = start_t_s + elapsed_s
        t_after = t_before + dt
        soc = max(0.0, soc - decay_per_hour * (dt / 3600.0))
        if t_before < charge_event_s <= t_after:
            soc = typical_end_soc
        elapsed_s += dt
        samples.append((t_after, soc))
    return samples


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
    validation shows offset error dominates (see docs/MILESTONES.md).

    Only the battery node's `final_node_temps_k` entry is shifted (motor/
    inverter aren't affected by the battery's own soak history, and Tier-2
    doesn't track them between trips anyway -- see docs/MILESTONES.md).
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
    of any real parked period -- see docs/MILESTONES.md).

    SOC is a real per-hour trajectory, not a snapped constant (Milestone
    "parked-SOC model" -- see docs/DESIGN_QUESTIONS.md Q-0012, and
    docs/MILESTONES.md for why the previous instantaneous-snap-
    to-typical_end_soc behavior had to be replaced): a trip applies its own
    actual SOC delta (`characterization.final_soc - charging_profile.
    typical_start_soc`, i.e. `typical_start_soc` is the reference point
    Tier-1 characterization used, not an enforced actual value each day --
    see Q-0012's own note on this), the pack then decays through the parked
    period at a rate driven by `AuxLoads.accessory_load_w` ONLY (the
    schema's own "baseline 12V/accessory" load -- NOT `hvac_power_w`, which
    is a driving/occupied-cabin load and produces an unrealistic ~5%/hour
    drain if used here), and charges back to `typical_end_soc` exactly once
    per day at `charging_profile.charge_reset_hour`.
    """
    spec = profile.battery
    battery_node_name = spec.thermal_node.name
    tau_s = (
        None
        if spec.thermal_node.resistance_to_ambient_k_per_w is None
        else spec.thermal_node.resistance_to_ambient_k_per_w.value * spec.thermal_node.capacitance_j_per_k.value
    )
    battery_loop = next(
        (loop for loop in profile.thermal_architecture.cooling_loops if battery_node_name in loop.serves), None
    )
    # Milestone D: a DX RefrigerantLoop is mutually exclusive with battery_loop
    # in every profile so far (the BMW i3 has no CoolingLoop serving its
    # battery at all) -- see relax_parked_temp_k's own docstring for why
    # refrigerant_active is itself gated on `not coolant_path_active`.
    refrigerant_loop = next(
        (loop for loop in profile.thermal_architecture.refrigerant_loops if battery_node_name in loop.serves), None
    )
    accessory_power_w = profile.aux_loads.accessory_load_w.value / profile.aux_loads.dc_dc_converter_efficiency.value
    pack_voltage_v = battery_ocv_v(spec, charging_profile.typical_end_soc.value, spec.reference_temp_k.value)
    decay_per_hour = (accessory_power_w / pack_voltage_v) / spec.capacity_nominal_ah.value
    pump_decay_per_hour_at_full_duty = (
        0.0
        if battery_loop is None
        else (battery_loop.pump_power_w.value / pack_voltage_v) / spec.capacity_nominal_ah.value
    )
    chiller_decay_per_hour_at_full_duty = (
        0.0
        if battery_loop is None or not battery_loop.chiller_present or battery_loop.chiller_power_w is None
        else (battery_loop.chiller_power_w.value / pack_voltage_v) / spec.capacity_nominal_ah.value
    )
    refrigerant_decay_per_hour_at_full_duty = (
        0.0
        if refrigerant_loop is None
        else (refrigerant_loop.power_w.value / pack_voltage_v) / spec.capacity_nominal_ah.value
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
            trip_delta_soc = characterization.final_soc - charging_profile.typical_start_soc.value
            soc = max(0.0, soc + trip_delta_soc)
            pack_temp_k = characterization.final_node_temps_k[battery_node_name]
            trip_duration_s = sum(phase.duration_s for phase in characterization.phases)
            parked_duration_s = max(0.0, 86400.0 - trip_duration_s)
            parked_start_s = day_start_s + trip_duration_s
        else:
            parked_duration_s = 86400.0
            parked_start_s = day_start_s

        charge_event_s = day_start_s + charging_profile.charge_reset_hour.value * 3600.0
        refrigerant_loss_soc = 0.0
        if battery_loop is None and refrigerant_loop is not None:
            temp_samples = relax_parked_temp_k(
                pack_temp_k,
                tau_s,
                ambient_fn,
                parked_start_s,
                parked_duration_s,
                capacitance_j_per_k=spec.thermal_node.capacitance_j_per_k.value,
                refrigerant_present=True,
                refrigerant_resistance_k_per_w=spec.thermal_node.resistance_to_coolant_k_per_w.value,
                refrigerant_activation_temp_k=refrigerant_loop.activation_temp_k.value,
                refrigerant_full_duty_temp_k=refrigerant_loop.full_duty_temp_k.value,
            )
            pump_loss_soc = 0.0
            chiller_loss_soc = 0.0
            refrigerant_loss_soc = _refrigerant_soc_decrement(
                temp_samples,
                charge_event_s,
                refrigerant_loop.activation_temp_k.value,
                refrigerant_loop.full_duty_temp_k.value,
                refrigerant_decay_per_hour_at_full_duty,
            )
        elif battery_loop is None:
            temp_samples = relax_parked_temp_k(pack_temp_k, tau_s, ambient_fn, parked_start_s, parked_duration_s)
            pump_loss_soc = 0.0
            chiller_loss_soc = 0.0
        else:
            coolant_resistance_k_per_w = (
                spec.thermal_node.resistance_to_coolant_k_per_w.value + 1.0 / battery_loop.radiator_ua_w_per_k.value
            )
            temp_samples = relax_parked_temp_k(
                pack_temp_k,
                tau_s,
                ambient_fn,
                parked_start_s,
                parked_duration_s,
                capacitance_j_per_k=spec.thermal_node.capacitance_j_per_k.value,
                coolant_resistance_k_per_w=coolant_resistance_k_per_w,
                thermostat_temp_k=battery_loop.thermostat_temp_k.value,
                thermostat_full_open_temp_k=battery_loop.thermostat_full_open_temp_k.value,
                chiller_present=battery_loop.chiller_present,
                chiller_capacity_w=(
                    battery_loop.chiller_capacity_w.value if battery_loop.chiller_capacity_w is not None else None
                ),
            )
            pump_loss_soc = _pump_soc_decrement(
                temp_samples,
                charge_event_s,
                battery_loop.thermostat_temp_k.value,
                battery_loop.thermostat_full_open_temp_k.value,
                pump_decay_per_hour_at_full_duty,
            )
            chiller_loss_soc = _chiller_soc_decrement(
                temp_samples,
                charge_event_s,
                battery_loop.thermostat_full_open_temp_k.value,
                chiller_decay_per_hour_at_full_duty,
            )
        soc_samples = _parked_soc_trajectory(
            soc,
            parked_start_s,
            parked_duration_s,
            charge_event_s,
            decay_per_hour,
            charging_profile.typical_end_soc.value,
        )
        parked_loss = parked_calendar_aging(spec, soc_samples, temp_samples)
        calendar_loss_cumulative += parked_loss
        soh = max(0.0, soh - parked_loss)
        pack_temp_k = temp_samples[-1][1]
        soc = max(0.0, soc_samples[-1][1] - pump_loss_soc - chiller_loss_soc - refrigerant_loss_soc)

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
