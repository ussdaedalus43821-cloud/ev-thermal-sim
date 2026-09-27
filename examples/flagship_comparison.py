"""Reproducible runner for this project's "flagship" comparative aging
demonstration: two vehicle profiles, each under its own climate, compared
over the same simulated horizon via Milestone E2's Tier-1/Tier-2 long-horizon
pipeline. Defaults to the Nissan Leaf in a hot climate (Guam-like) vs. the
Tesla Model 3 in a mild climate (Seattle-like), the comparison referenced in
docs/MILESTONES.md.

Exists because an earlier one-off version of this comparison was run ad hoc
and its exact script was not preserved, which meant a later re-run (after
fixing the cycle-aging capacity-normalization gap) could not be checked
against the original numbers under identical conditions -- only a
reconstruction. See docs/MILESTONES.md for the full story. This
script is the fix: every scenario parameter that defines a specific
comparison run is an explicit, named argument here (CLI flag or a
documented module-level builder function), not an inline literal buried in
a throwaway script, so any future re-run (e.g. once preconditioning or a
multi-year horizon lands) can be reproduced exactly by re-running this file
with the same flags, and any future reviewer can regenerate the numbers on
demand rather than trusting a copy-pasted table.

Usage:
    uv run python examples/flagship_comparison.py
    uv run python examples/flagship_comparison.py --duration-days 30
    uv run python examples/flagship_comparison.py \
        --profile-a configs/profiles/bmw_i3.yaml --profile-b configs/profiles/audi_etron.yaml
    uv run python examples/flagship_comparison.py \
        --fast-charge-occurrences-a 0 --fast-charge-occurrences-b 0  # disable fast-charge days entirely

Fast-charge occurrence rates (`--fast-charge-occurrences-a`/`-b`, Q-0029)
default to `DEFAULT_FAST_CHARGE_OCCURRENCES_A`/`_B`, which match `DEFAULT_
PROFILE_A`/`_B` specifically -- override them (or pass 0 to disable) when
comparing a different profile pair.

**This script produces a POINT ESTIMATE, not a median.** Every number this
script prints is a single unperturbed run at each aging coefficient's
central value — it does not itself run Monte Carlo. If a Monte Carlo band
(`ev_thermal.mc.run_monte_carlo`) is ever shown alongside this script's
table, do not assume `MCResult.soh_band[i].p50` will match the
corresponding row here, especially at a long duration: the two are
different objects by construction, and the gap between them grows with
horizon (negligible at 90 days, ~6% relative by 20 years for the reference
Leaf/Guam scenario). See `ev_thermal.mc`'s module docstring ("p50 vs. the
point estimate") for why, and `examples/mc_horizon_gap_investigation.py`
for the reproducible evidence.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

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
    TripType,
    UsageProfile,
)

DEFAULT_PROFILE_A = "configs/profiles/nissan_leaf.yaml"
DEFAULT_PROFILE_B = "configs/profiles/tesla_model3.yaml"
DEFAULT_DURATION_DAYS = 90
DEFAULT_SEED = 42
DAYS_PER_YEAR = 365.25
# Milestone: fast-charge occurrence rates (Q-0029). Defaults match DEFAULT_
# PROFILE_A/B specifically (this project's 24kWh Gen1 Leaf, 73mi EPA range,
# vs. its 80.5kWh/75kWh-usable Model 3, matching the Long Range AWD trim's
# ~358mi EPA range) -- override via --fast-charge-occurrences-a/-b for a
# different --profile-a/--profile-b pair. See Q-0029 for the reasoning.
DEFAULT_FAST_CHARGE_OCCURRENCES_A = 48
DEFAULT_FAST_CHARGE_OCCURRENCES_B = 12


def _sourced(value: float, source: str = "flagship-comparison placeholder") -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source=source)


def guam_climate() -> Climate:
    """Hot, low-seasonal-variation tropical climate -- stresses a pack with a
    weak or absent active cooling path, per this comparison's own teaching
    point (Leaf: passive air-cooled battery).
    """
    return Climate(
        location_name="Guam",
        mean_annual_temp_k=_sourced(303.15),  # 30C
        seasonal_amplitude_k=_sourced(2.0),
        daily_amplitude_k=_sourced(4.0),
        altitude_m=_sourced(0.0),
        solar_irradiance_w_per_m2=_sourced(250.0),
    )


def seattle_climate() -> Climate:
    """Mild, moderate-seasonal-variation climate -- the comparison's benign
    baseline (Model 3: actively liquid-cooled battery).
    """
    return Climate(
        location_name="Seattle",
        mean_annual_temp_k=_sourced(285.15),  # 12C
        seasonal_amplitude_k=_sourced(8.0),
        daily_amplitude_k=_sourced(6.0),
        altitude_m=_sourced(50.0),
        solar_irradiance_w_per_m2=_sourced(150.0),
    )


def default_day_type_mix(fast_charge_occurrences_per_year: int = 0) -> list[AnnualDayTypeMix]:
    """Annual trip-occurrence counts -- an explicit, named usage assumption,
    not something implied by the profile or climate. Same counts for both
    profiles in a comparison, so usage pattern is a controlled variable,
    same reasoning as aging.py's shared-coefficients design.

    `fast_charge_occurrences_per_year` (Q-0029) is the one EXCEPTION to
    "same counts for both profiles" -- deliberately, not an oversight: how
    often a real driver needs a fast charge is a function of the vehicle's
    own range, not a controlled variable the way driving-archetype mix is.
    Defaults to 0 (no `FAST_CHARGE_SESSION` entry at all), preserving every
    existing caller's behavior unchanged (mistake-library scripts, Monte
    Carlo examples/tests) -- only `run_one`'s flagship-comparison callers
    pass a nonzero value.
    """
    mix = [
        AnnualDayTypeMix(trip_type=TripType.COLD_START_COMMUTE, occurrences_per_year=_sourced(60)),
        AnnualDayTypeMix(trip_type=TripType.HOT_SOAK_COMMUTE, occurrences_per_year=_sourced(60)),
        AnnualDayTypeMix(trip_type=TripType.HIGHWAY, occurrences_per_year=_sourced(20)),
        AnnualDayTypeMix(trip_type=TripType.CITY_STOP_AND_GO, occurrences_per_year=_sourced(100)),
    ]
    if fast_charge_occurrences_per_year > 0:
        mix.append(
            AnnualDayTypeMix(
                trip_type=TripType.FAST_CHARGE_SESSION,
                occurrences_per_year=_sourced(
                    fast_charge_occurrences_per_year,
                    source="docs/DESIGN_QUESTIONS.md Q-0029 -- range-based usage assumption, not research",
                ),
            )
        )
    return mix


def default_usage_profile() -> UsageProfile:
    """Not read by `characterize_all_trips`/`simulate_long_horizon` (which take
    `day_type_mix` as its own explicit argument, not via this profile's own
    `day_type_mix` field) -- populated anyway since `Scenario.usage_profile`
    is required, same fields/values as `tests/test_sim.py`'s `_make_scenario`.
    """
    return UsageProfile(
        annual_mileage_km=_sourced(15_000.0),
        mean_trip_km=_sourced(20.0),
        trip_km_std=_sourced(10.0),
        driving_style="normal",
        garage_parked=True,
    )


def default_charging_profile() -> ChargingProfile:
    """`preconditioning_enabled=True` (Q-0029, flipped from the prior False):
    a real driver who navigates to a charger uses preconditioning whenever
    the vehicle supports it -- there's no realistic reason to model a
    scenario where it's deliberately disabled. Safe to flip for every
    existing caller of this function: the mistake library's three vehicles
    (Leaf, e-tron, i3) all have `ThermalArchitecture.battery_preconditioning
    = False`, so the capability x scenario gate stays closed for them
    regardless -- confirmed unchanged by their own 0.0000% cold-charge
    columns.
    """
    return ChargingProfile(
        charger_type="Level 2",
        charge_power_kw=_sourced(11.0),
        typical_start_soc=_sourced(0.5),
        typical_end_soc=_sourced(0.8),
        dc_fast_charge_fraction=_sourced(0.1),
        preconditioning_enabled=True,
        dc_charge_power_kw=_sourced(50.0),
        charge_reset_hour=_sourced(22.0),
    )


def build_scenario(profile: ManufacturerProfile, climate: Climate, seed: int) -> Scenario:
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")
    return Scenario(
        name="flagship-comparison",
        manufacturer_profile=profile,
        climate=climate,
        usage_profile=default_usage_profile(),
        charging_profile=default_charging_profile(),
        duration_years=1,
        dt_policy=dt_policy,
        random_seed=seed,
    )


class ComparisonResult:
    def __init__(
        self,
        label: str,
        total_loss: float,
        calendar_loss: float,
        cycle_loss: float,
        cold_charge_plating_loss: float,
    ) -> None:
        self.label = label
        self.total_loss = total_loss
        self.calendar_loss = calendar_loss
        self.cycle_loss = cycle_loss
        self.cold_charge_plating_loss = cold_charge_plating_loss


def run_one(
    profile_path: Path,
    climate: Climate,
    duration_days: int,
    seed: int,
    label: str,
    fast_charge_occurrences_per_year: int = 0,
) -> ComparisonResult:
    profile = load_profile(profile_path)
    scenario = build_scenario(profile, climate, seed)
    day_type_mix = default_day_type_mix(fast_charge_occurrences_per_year)
    characterizations = characterize_all_trips(profile, scenario, n_buckets=4)
    duration_years = max(1, math.ceil(duration_days / DAYS_PER_YEAR))
    result = simulate_long_horizon(
        profile=profile,
        trip_characterizations=characterizations,
        day_type_mix=day_type_mix,
        climate=climate,
        charging_profile=scenario.charging_profile,
        duration_years=duration_years,
        seed=seed,
    )
    checkpoint = result.checkpoints[duration_days - 1]
    return ComparisonResult(
        label=label,
        total_loss=1.0 - checkpoint.battery_soh,
        calendar_loss=checkpoint.calendar_soh_loss_cumulative,
        cycle_loss=checkpoint.cycle_soh_loss_cumulative,
        cold_charge_plating_loss=checkpoint.cold_charge_plating_soh_loss_cumulative,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile-a", type=Path, default=Path(DEFAULT_PROFILE_A))
    parser.add_argument("--profile-b", type=Path, default=Path(DEFAULT_PROFILE_B))
    parser.add_argument("--duration-days", type=int, default=DEFAULT_DURATION_DAYS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--fast-charge-occurrences-a", type=int, default=DEFAULT_FAST_CHARGE_OCCURRENCES_A)
    parser.add_argument("--fast-charge-occurrences-b", type=int, default=DEFAULT_FAST_CHARGE_OCCURRENCES_B)
    args = parser.parse_args()

    result_a = run_one(
        args.profile_a,
        guam_climate(),
        args.duration_days,
        args.seed,
        f"{args.profile_a.stem}/Guam",
        args.fast_charge_occurrences_a,
    )
    result_b = run_one(
        args.profile_b,
        seattle_climate(),
        args.duration_days,
        args.seed,
        f"{args.profile_b.stem}/Seattle",
        args.fast_charge_occurrences_b,
    )

    print(f"Comparison over {args.duration_days} days, seed={args.seed}")
    print()
    header = f"{'':<22}{'total':>10}{'calendar':>10}{'cycle':>10}{'cold-charge':>13}"
    print(header)
    for r in (result_a, result_b):
        print(
            f"{r.label:<22}{r.total_loss * 100:>9.4f}%{r.calendar_loss * 100:>9.4f}%"
            f"{r.cycle_loss * 100:>9.4f}%{r.cold_charge_plating_loss * 100:>12.4f}%"
        )
    print()
    print(f"total ratio (A/B):    {result_a.total_loss / result_b.total_loss:.4f}")
    print(f"calendar ratio (A/B): {result_a.calendar_loss / result_b.calendar_loss:.4f}")
    print(f"cycle ratio (A/B):    {result_a.cycle_loss / result_b.cycle_loss:.4f}")


if __name__ == "__main__":
    main()
