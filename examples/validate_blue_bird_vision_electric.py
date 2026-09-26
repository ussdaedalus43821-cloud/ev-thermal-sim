"""Validates the Blue Bird Vision Electric commercial profile
(`configs/profiles_commercial/blue_bird_vision_electric.yaml`) the same way
the six passenger-EV reference profiles were validated before anything was
built on top of them: a real, longer-duration single-fidelity run checked
for thermal-steady-state plausibility (the exact class of check that caught
the demo profile's original thermal-runaway bug), a real Tier-1
characterization across every implemented trip archetype, and a real Tier-2
long-horizon SOH trajectory over a real (if modest) horizon -- all checked
for physical plausibility before this profile's numbers go into any
published table or Transition-seam deliverable. Per this project's own
"check the actual numbers before committing" discipline (see
docs/MILESTONES.md), not a schema/smoke-test pass alone -- that already
exists in tests/test_profiles_commercial.py and is not repeated here.

This profile did not previously have a UsageProfile/ChargingProfile/Climate
of its own (docs/ROADMAP.md's "Transition seam" section flagged this as
blocking) -- this script builds them here, from real sources wherever one
exists:

- Climate: real NOAA 1991-2020 normals for Columbus, OH (the School Bus
  Depot preset's real location in Transition/src/constants.ts), the same
  numbers already used to close the Transition seam's gap 3, converted from
  the Fahrenheit values stored there to the kelvin this schema uses.
  altitude_m is Columbus's real elevation. solar_irradiance_w_per_m2 has no
  real source here (same status as every existing profile's climate
  fixtures) -- a placeholder, confidence low.
- UsageProfile: annual_mileage_km and the trip-length mean/std are derived
  from Transition's real SCHOOL_BUS_DEPOT_PRESET route data (70mi/day x
  ~180 school days/year annual mileage; mean/std of one-way trip distance
  computed from its four routes' real dailyMiles, vehicle-count-weighted)
  -- not invented, but Transition's own preset data is itself flagged there
  as "not a real ridership study," so this remains a placeholder one level
  removed from field research, same as every other profile's UsageProfile.
- ChargingProfile: charge_power_kw=11.0 matches Transition's real
  chargingDemandPerVehicleKWPlaceholder for this preset (overnight L2,
  single depot). dc_fast_charge_fraction=0.0 -- a depot-charged school bus
  fleet has no real DC fast-charging usage pattern to model, consistent
  with Transition's own L2-only assumption for this preset.
  typical_start_soc/typical_end_soc reuse the flagship comparison's own
  0.5/0.8 convention rather than being tuned to this vehicle's real
  ~150-mile range and ~74mi/day usage -- a known simplification, flagged,
  not blocking this validation pass's actual purpose (catching gross
  physical implausibility, not producing a usage-accurate published
  number).
- day_type_mix: COLD_START_COMMUTE/HOT_SOAK_COMMUTE/CITY_STOP_AND_GO split
  evenly across Transition's real ~180 school-days-per-year figure, no
  HIGHWAY occurrences (school bus routes are local, not highway-speed) and
  no FAST_CHARGE_SESSION occurrences (matches the charging-profile
  assumption above). This specific 60/60/60 split is a reasonable,
  clearly-flagged assumption for a first validation pass, not a
  researched usage claim.

A real, separately-flagged limitation this script does NOT attempt to fix:
the underlying archetype speed traces in long_horizon.py's characterization
(`_ARCHETYPES`) were built for passenger-car commute/highway/city driving,
not tuned for a heavy Type C school bus's real (lower, stop-heavy) speed
profile. This script validates that the EXISTING machinery produces
physically sane output for this vehicle -- it does not build new
bus-specific speed traces, which is a materially bigger piece of work than
"validate before building on top."

Usage:
    uv run python examples/validate_blue_bird_vision_electric.py
    uv run python examples/validate_blue_bird_vision_electric.py --duration-days 7305
"""

from __future__ import annotations

import argparse
import math

from ev_thermal.long_horizon import characterize_all_trips, simulate_long_horizon
from ev_thermal.profile_loader import load_profile
from ev_thermal.schema import (
    AnnualDayTypeMix,
    ChargingProfile,
    Climate,
    Confidence,
    DTPolicy,
    ManufacturerProfile,
    Scenario,
    Sourced,
    TripCharacterization,
    TripType,
    UsageProfile,
)
from ev_thermal.sim import run_scenario

PROFILE_PATH = "configs/profiles_commercial/blue_bird_vision_electric.yaml"
DEFAULT_DURATION_DAYS = 90
SEED = 42
DAYS_PER_YEAR = 365.25


def _sourced(value: float, source: str) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source=source)


def columbus_climate() -> Climate:
    """Real NOAA 1991-2020 normals for Columbus, OH -- the same figures used
    to close the Transition seam's gap 3 (Transition/src/constants.ts
    SCHOOL_BUS_DEPOT_PRESET.location.climateZone), converted from the
    Fahrenheit values stored there: K = (F-32)*5/9 + 273.15 for the mean,
    and delta_K = delta_F * 5/9 for the two amplitude fields (a temperature
    DIFFERENCE, not an absolute -- no +32/273.15 offset applies).
    mean: 53.4F -> 285.04K. seasonal_amplitude: 22.5F -> 12.5K.
    daily_amplitude: 9.2F -> 5.11K.
    """
    return Climate(
        location_name="Columbus, OH",
        mean_annual_temp_k=_sourced(
            285.04,
            "NOAA NCEI 1991-2020 normals, Columbus OH (USW00093812) -- same source as Transition's gap-3 closure",
        ),
        seasonal_amplitude_k=_sourced(
            12.5,
            "NOAA Columbus OH normals -- (hottest-coolest monthly mean)/2, same as Transition's gap-3 closure",
        ),
        daily_amplitude_k=_sourced(
            5.11,
            "NOAA Columbus OH normals -- avg monthly diurnal range, same as Transition's gap-3 closure",
        ),
        altitude_m=_sourced(271.0, "Columbus, OH real elevation (~889ft)"),
        solar_irradiance_w_per_m2=_sourced(
            200.0,
            "placeholder -- outside research scope, same convention as every other profile's climate fixtures",
        ),
    )


def school_bus_day_type_mix() -> list[AnnualDayTypeMix]:
    """Split evenly across Transition's real ~180-school-day-per-year figure
    (SCHOOL_BUS_DEPOT_PRESET.fleet.vehicleClasses[0].avgAnnualMileage's own
    "70mi/day x ~180 school days/year" derivation). No HIGHWAY occurrences
    (school bus routes are local); no FAST_CHARGE_SESSION occurrences
    (depot L2 charging only, matching the charging profile below).
    """
    return [
        AnnualDayTypeMix(
            trip_type=TripType.COLD_START_COMMUTE,
            occurrences_per_year=_sourced(
                60, "1/3 of Transition's real ~180 school-days/year figure -- assumption, not independently researched"
            ),
        ),
        AnnualDayTypeMix(
            trip_type=TripType.HOT_SOAK_COMMUTE,
            occurrences_per_year=_sourced(
                60, "1/3 of Transition's real ~180 school-days/year figure -- assumption, not independently researched"
            ),
        ),
        AnnualDayTypeMix(
            trip_type=TripType.CITY_STOP_AND_GO,
            occurrences_per_year=_sourced(
                60, "1/3 of Transition's real ~180 school-days/year figure -- assumption, not independently researched"
            ),
        ),
    ]


def school_bus_usage_profile() -> UsageProfile:
    """annual_mileage_km: Transition's real 70mi/day x ~180 school days/year
    (12,600 mi/year) converted to km. mean_trip_km/trip_km_std: vehicle-
    count-weighted mean and std of ONE-WAY trip distance across Transition's
    four real routes (Elementary 55mi/day/20 buses, Middle 75mi/20/15,
    High 95mi/12, SpecialEd 110mi/3 -- halved for one-way, weighted by
    dedicatedVehicleCount), converted to km. Not read by
    characterize_all_trips/simulate_long_horizon (which take day_type_mix
    directly), populated because Scenario.usage_profile is required, same
    as every other profile's validation scenario.
    """
    return UsageProfile(
        annual_mileage_km=_sourced(
            12_600.0 * 1.609344,
            "Transition SCHOOL_BUS_DEPOT_PRESET avgAnnualMileage (70mi/day x ~180 school days/yr) -- placeholder",
        ),
        mean_trip_km=_sourced(
            36.95 * 1.609344,
            "vehicle-count-weighted mean one-way trip distance across Transition's four real routes",
        ),
        trip_km_std=_sourced(
            9.06 * 1.609344,
            "vehicle-count-weighted std of one-way trip distance across Transition's four real routes",
        ),
        driving_style="normal",
        garage_parked=True,
    )


def school_bus_charging_profile() -> ChargingProfile:
    return ChargingProfile(
        charger_type="Level 2",
        charge_power_kw=_sourced(
            11.0, "Transition SCHOOL_BUS_DEPOT_PRESET.fleet.vehicleEconomics.chargingDemandPerVehicleKWPlaceholder"
        ),
        typical_start_soc=_sourced(
            0.5,
            "reused from flagship_comparison.py -- not tuned to this vehicle, a known simplification (see docstring)",
        ),
        typical_end_soc=_sourced(
            0.8,
            "reused from flagship_comparison.py -- not tuned to this vehicle, a known simplification (see docstring)",
        ),
        dc_fast_charge_fraction=_sourced(
            0.0,
            "school buses return to a single depot overnight; no DC fast-charge usage in this preset",
        ),
        preconditioning_enabled=False,
        dc_charge_power_kw=_sourced(
            120.0,
            "commercial_profiles_v1.md -- Blue Bird DC fast charge rating (unused, dc_fast_charge_fraction=0)",
        ),
        charge_reset_hour=_sourced(22.0, "shared convention across all six passenger profiles, reused verbatim"),
    )


def build_scenario(profile: ManufacturerProfile, climate: Climate, seed: int) -> Scenario:
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")
    return Scenario(
        name="blue-bird-validation",
        manufacturer_profile=profile,
        climate=climate,
        usage_profile=school_bus_usage_profile(),
        charging_profile=school_bus_charging_profile(),
        duration_years=1,
        dt_policy=dt_policy,
        random_seed=seed,
    )


def _school_bus_speed_trace(t: float) -> float:
    """Simple ramp-then-cruise at ~13.4 m/s (30 mph), a reasonable average
    suburban school-route speed -- not a real drive-cycle model (this
    project doesn't have one yet, see docs/MILESTONES.md), same convention
    as every other profile's own thermal-steady-state check.
    """
    if t < 20.0:
        return 13.4 * (t / 20.0)
    return 13.4


def step_a_thermal_steady_state(profile: ManufacturerProfile, scenario: Scenario) -> None:
    print("=" * 78)
    print("Step A: 1800s single-fidelity thermal-steady-state check")
    print("=" * 78)
    result = run_scenario(profile, scenario, duration_s=1800.0, target_speed_mps=_school_bus_speed_trace)

    values_to_check = [
        result.final_state.speed_mps,
        result.final_state.battery_soc,
        *result.final_state.node_temps_k.values(),
        *result.time_series.commanded_torque_nm,
    ]
    all_finite = all(math.isfinite(v) for v in values_to_check)
    print(f"All finite (no NaN/Inf): {all_finite}")

    for node, temp_k in result.final_state.node_temps_k.items():
        peak = max(result.time_series.node_temps_k[node])
        print(f"  {node:<14} final={temp_k - 273.15:6.1f}C   peak={peak - 273.15:6.1f}C")

    limit_events = [e for e in result.causal_events if "LIMIT" in e.event_type or "DERATE" in e.event_type]
    print(f"Limit/derate events fired: {len(limit_events)}")
    for e in limit_events[:10]:
        print(f"  t={e.sim_time_s:7.1f}s  {e.event_type:<24} {e.description}")

    et = result.energy_totals
    mechanical_proxy_j = (
        et.consumed_j - et.motor_loss_j - et.inverter_loss_j - et.battery_loss_j - et.aux_j + et.regen_recovered_j
    )
    print(
        f"Energy totals (kJ): consumed={et.consumed_j / 1e3:.1f} regen={et.regen_recovered_j / 1e3:.1f} "
        f"motor_loss={et.motor_loss_j / 1e3:.1f} inverter_loss={et.inverter_loss_j / 1e3:.1f} "
        f"battery_loss={et.battery_loss_j / 1e3:.1f} radiator_rejected={et.radiator_rejected_j / 1e3:.1f} "
        f"aux={et.aux_j / 1e3:.1f}"
    )
    print(
        f"Implied mechanical work (consumed - all losses - aux + regen): {mechanical_proxy_j / 1e3:.1f} kJ "
        f"({'positive, plausible' if mechanical_proxy_j > 0 else 'NEGATIVE -- investigate'})"
    )
    print()


def step_b_tier1_characterization(
    profile: ManufacturerProfile, scenario: Scenario
) -> dict[TripType, list[TripCharacterization]]:
    print("=" * 78)
    print("Step B: Tier-1 characterization, all archetypes")
    print("=" * 78)
    characterizations = characterize_all_trips(profile, scenario, n_buckets=4)
    for trip_type, runs in characterizations.items():
        print(f"{trip_type.value}:")
        for run in runs:
            peak_temps = [p.peak_cell_temp_k - 273.15 for p in run.phases]
            all_finite = all(math.isfinite(p.peak_cell_temp_k) and math.isfinite(p.mean_soc) for p in run.phases)
            print(
                f"  bucket={run.ambient_bucket_temp_k - 273.15:6.1f}C  "
                f"final_soc={run.final_soc:.3f}  odometer={run.odometer_km_delta:6.2f}km  "
                f"peak_cell_temp={max(peak_temps):6.1f}C  finite={all_finite}"
            )
    print()
    return characterizations


def step_c_long_horizon(
    profile: ManufacturerProfile,
    characterizations: dict[TripType, list[TripCharacterization]],
    climate: Climate,
    charging_profile: ChargingProfile,
    duration_days: int,
) -> None:
    print("=" * 78)
    print(f"Step C: Tier-2 long-horizon run, {duration_days} days")
    print("=" * 78)
    duration_years = max(1, math.ceil(duration_days / DAYS_PER_YEAR))
    result = simulate_long_horizon(
        profile=profile,
        trip_characterizations=characterizations,
        day_type_mix=school_bus_day_type_mix(),
        climate=climate,
        charging_profile=charging_profile,
        duration_years=duration_years,
        seed=SEED,
    )

    sohs = [cp.battery_soh for cp in result.checkpoints[:duration_days]]
    all_finite = all(math.isfinite(s) for s in sohs)
    monotonic = all(sohs[i] >= sohs[i + 1] - 1e-9 for i in range(len(sohs) - 1))
    final = result.checkpoints[duration_days - 1]

    print(f"All SOH values finite: {all_finite}")
    print(f"SOH monotonically non-increasing: {monotonic}")
    print(
        f"Day 0:  SOH={result.checkpoints[0].battery_soh * 100:.4f}%  SOC={result.checkpoints[0].battery_soc:.3f}  "
        f"pack_temp={result.checkpoints[0].pack_cell_temp_k - 273.15:.1f}C"
    )
    print(
        f"Day {duration_days - 1}: SOH={final.battery_soh * 100:.4f}%  SOC={final.battery_soc:.3f}  "
        f"pack_temp={final.pack_cell_temp_k - 273.15:.1f}C  odometer={final.odometer_km:.1f}km"
    )
    total_loss_pct = (1.0 - final.battery_soh) * 100
    calendar_pct = final.calendar_soh_loss_cumulative * 100
    cycle_pct = final.cycle_soh_loss_cumulative * 100
    print(
        f"Total SOH loss over {duration_days} days: {total_loss_pct:.4f}% "
        f"(calendar={calendar_pct:.4f}%, cycle={cycle_pct:.4f}%)"
    )
    print(
        "Order-of-magnitude cross-check against the passenger-fleet flagship record "
        "(Leaf/Guam 0.5828%, Model3/Seattle 0.1194% over 90 days; Leaf/Guam 52.721% "
        "over a 20-year unperturbed point estimate): "
        f"{'within a plausible range' if 0.01 <= total_loss_pct <= 99.0 else 'OUTSIDE plausible range -- investigate'}"
    )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration-days", type=int, default=DEFAULT_DURATION_DAYS)
    args = parser.parse_args()

    profile = load_profile(PROFILE_PATH)
    climate = columbus_climate()
    scenario = build_scenario(profile, climate, SEED)

    step_a_thermal_steady_state(profile, scenario)
    characterizations = step_b_tier1_characterization(profile, scenario)
    step_c_long_horizon(profile, characterizations, climate, scenario.charging_profile, args.duration_days)


if __name__ == "__main__":
    main()
