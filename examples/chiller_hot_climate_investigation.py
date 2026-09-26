"""Reproducible runner for the investigation into whether a genuinely hotter
test climate makes Blue Bird Vision Electric's chiller (Q-0021,
docs/DESIGN_QUESTIONS.md) visibly change its own published SOH numbers.

Both the chiller-path milestone and its hot-soak-coolant-initialization
follow-up (docs/MILESTONES.md) left this as the one candidate cause neither
had tried: Guam's own mild seasonal amplitude (2K) never pushed the coolant
loop's temperature anywhere near the chiller's engagement threshold
(thermostat_full_open_temp_k, 40C), even after the hot-soak fix let the
coolant reflect the battery's own pre-heated soak condition.

This script uses REAL NOAA NCEI 1991-2020 normals for Phoenix Sky Harbor
Intl AP, AZ (station USW00023183) -- not an invented or scaled-up
placeholder -- per this project's "don't invent parameters" rule:
  - Annual mean: 75.6F -> mean_annual_temp_k = (75.6-32)*5/9+273.15 = 297.37K
  - Seasonal amplitude: (hottest month mean [Jul, 95.5F] - coolest month
    mean [Jan, 56.8F]) / 2 = 19.35F -> 10.75K, same derivation convention
    already used for Blue Bird's own Columbus, OH climate.
  - Daily amplitude: average of each month's (Daily Max - Daily Min) normal,
    22.95F -> 12.75K.
Phoenix's ANNUAL MEAN (24.2C) is actually lower than the Guam-like test
climate's (30C) -- Phoenix has real winters. What differs is amplitude:
Phoenix's implied peak ambient (mean + seasonal + daily = 47.7C) is far
above Guam's (36C) and the 40C chiller threshold, where Guam's peak never
got close.

RESULT (see docs/MILESTONES.md's "Hot climate chiller investigation"
milestone for the full record): the hotter climate DOES make the chiller
engage, dramatically -- commanded_chiller_duty reaches 1.0 (full 5000W
draw) during HOT_SOAK_COMMUTE at the hottest ambient bucket, confirmed by
direct time-series inspection. But even running at full capacity for an
entire ~1200s trip, it only shaves 0.03-0.35K off the battery's own mean
cell temperature (measured directly, phase by phase) -- because the
placeholder chiller_capacity_w (5000W, already flagged in Blue Bird's own
YAML as "likely undersized for a bus-size battery pack") is small relative
to the pack's own thermal capacitance (990,000 J/K). The resulting 1-year
SOH effect (this script's own default `--duration-years 1` output) is real
but negligible: 0.00027818 percentage points absolute (3.51090196% with
the chiller vs. 3.51118014% without), 0.007923% relative -- three to four
orders of magnitude smaller than what would be needed to change any
published comparison. This closes the "hotter test
climate" candidate cause with a real answer, not a null result: the
remaining blocker is chiller_capacity_w's magnitude, not engagement
timing, threshold placement, or climate choice -- a materially different,
more specific finding than "not tested hot enough."

Usage:
    uv run python -m examples.chiller_hot_climate_investigation
    uv run python -m examples.chiller_hot_climate_investigation --duration-years 5
"""

from __future__ import annotations

import argparse

from ev_thermal.long_horizon import (
    ambient_bucket_temps_k,
    characterize_all_trips,
    characterize_trip,
    simulate_long_horizon,
)
from ev_thermal.profile_loader import load_profile
from ev_thermal.schema import Climate, Confidence, ManufacturerProfile, Sourced, TripType
from examples.validate_blue_bird_vision_electric import PROFILE_PATH, SEED, build_scenario, school_bus_day_type_mix

PHOENIX_SOURCE = "NOAA NCEI 1991-2020 normals, Phoenix Sky Harbor Intl AP AZ (USW00023183)"


def _sourced(value: float, note: str) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.HIGH, source=note)


def phoenix_climate() -> Climate:
    """Real NOAA NCEI 1991-2020 normals, Phoenix Sky Harbor Intl AP, AZ --
    see this module's own docstring for the full derivation and citation.
    """
    return Climate(
        location_name="Phoenix, AZ",
        mean_annual_temp_k=_sourced((75.6 - 32) * 5.0 / 9.0 + 273.15, f"{PHOENIX_SOURCE} -- annual mean 75.6F"),
        seasonal_amplitude_k=_sourced(
            (95.5 - 56.8) / 2.0 * 5.0 / 9.0,
            f"{PHOENIX_SOURCE} -- (hottest [Jul 95.5F] - coolest [Jan 56.8F] monthly mean)/2",
        ),
        daily_amplitude_k=_sourced(22.95 * 5.0 / 9.0, f"{PHOENIX_SOURCE} -- avg monthly (Daily Max - Daily Min)"),
        altitude_m=_sourced(1107.0 * 0.3048, "Phoenix Sky Harbor Intl AP real elevation (1107ft)"),
        solar_irradiance_w_per_m2=_sourced(
            250.0, "placeholder -- outside research scope, same convention as every other profile"
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration-years", type=int, default=1)
    args = parser.parse_args()

    climate = phoenix_climate()
    buckets_c = [t - 273.15 for t in ambient_bucket_temps_k(climate, n_buckets=4)]
    print(f"Climate: {climate.location_name} ({PHOENIX_SOURCE})")
    print(
        f"mean={climate.mean_annual_temp_k.value - 273.15:.2f}C  "
        f"seasonal_amplitude={climate.seasonal_amplitude_k.value:.2f}K  "
        f"daily_amplitude={climate.daily_amplitude_k.value:.2f}K  "
        f"implied_peak={max(buckets_c):.2f}C"
    )
    print(f"ambient buckets (C): {[round(b, 1) for b in buckets_c]}")
    print()

    profile = load_profile(PROFILE_PATH)
    scenario = build_scenario(profile, climate, SEED)
    day_type_mix = school_bus_day_type_mix()

    no_chiller_loop = profile.thermal_architecture.cooling_loops[0].model_copy(update={"chiller_present": False})
    no_chiller_thermal_architecture = profile.thermal_architecture.model_copy(
        update={"cooling_loops": [no_chiller_loop]}
    )
    no_chiller_profile = profile.model_copy(update={"thermal_architecture": no_chiller_thermal_architecture})

    print("=" * 78)
    print("Step 1: does the chiller engage at the hottest bucket's HOT_SOAK_COMMUTE?")
    print("=" * 78)
    hottest_bucket_k = max(ambient_bucket_temps_k(climate, n_buckets=4))
    with_c = characterize_trip(profile, scenario, TripType.HOT_SOAK_COMMUTE, hottest_bucket_k)
    without_c = characterize_trip(no_chiller_profile, scenario, TripType.HOT_SOAK_COMMUTE, hottest_bucket_k)
    for phase_with, phase_without in zip(with_c.phases, without_c.phases, strict=True):
        diff_k = phase_without.mean_cell_temp_k - phase_with.mean_cell_temp_k
        print(
            f"  {phase_with.phase_name:<16} WITH mean={phase_with.mean_cell_temp_k - 273.15:7.3f}C  "
            f"NO mean={phase_without.mean_cell_temp_k - 273.15:7.3f}C  chiller_effect={diff_k:.4f}K"
        )
    print()

    print("=" * 78)
    print(f"Step 2: {args.duration_years}-year Tier-2 aggregate effect")
    print("=" * 78)

    def run(p: ManufacturerProfile) -> float:
        chars = characterize_all_trips(p, scenario, n_buckets=4)
        result = simulate_long_horizon(
            profile=p,
            trip_characterizations=chars,
            day_type_mix=day_type_mix,
            climate=climate,
            charging_profile=scenario.charging_profile,
            duration_years=args.duration_years,
            seed=SEED,
        )
        return 1.0 - result.checkpoints[-1].battery_soh

    with_loss = run(profile)
    without_loss = run(no_chiller_profile)
    abs_diff_pp = (without_loss - with_loss) * 100.0
    rel_diff_pct = (1.0 - with_loss / without_loss) * 100.0 if without_loss > 0 else 0.0
    print(f"WITH chiller total loss: {with_loss * 100:.8f}%")
    print(f"NO   chiller total loss: {without_loss * 100:.8f}%")
    print(f"absolute difference (pp): {abs_diff_pp:.8f}")
    print(f"relative reduction: {rel_diff_pct:.6f}%")


if __name__ == "__main__":
    main()
