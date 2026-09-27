"""Generates a first version of the Transition-seam SOH-delta table
(docs/seam_reconnaissance.md §5) -- step 3 of the seam-table plan, after
step 1 (Blue Bird's UsageProfile/ChargingProfile promoted to canonical,
Q-0022) and step 2 (Transition's ClimateZone gained the two missing
Climate fields it needed).

Deliberately ONE ROW, not a comprehensive matrix, per explicit scope: Blue
Bird Vision Electric, Columbus OH (its real, already-validated climate),
one fixed `duty_cycle_archetype` ("default" -- Transition's Route has no
per-route duty-cycle detail yet, seam_reconnaissance.md gap 2, still
deliberately deferred). Proving the table CAN be produced end-to-end from
already-canonical data matters more right now than populating a matrix no
consumer can look up yet (Transition-side wiring is its own next
milestone, not done here).

Reuses examples/validate_blue_bird_vision_electric.py's own canonical
builders directly (profile, climate, usage/charging profile, day_type_mix)
-- not re-declared, so this table is guaranteed to reflect the SAME
canonical data that profile's own validation record describes, not a
silently-drifted copy.

Methodology: a single unperturbed `simulate_long_horizon` point estimate
(SeamTableRow.methodology="point_estimate"), NOT a Monte Carlo distribution
-- seam_reconnaissance.md §5 originally proposed "median + spread, from
MCResult," but running MC for a first proof-of-shape row adds real cost
(500 samples) for a distinction this row doesn't need yet. Do not read
monthly_soh_delta_pct as a median -- mc.py's own "p50 vs. the point
estimate" note documents why a point estimate and an MC median are
different claims about the same model, with the gap growing at longer
horizons. Upgrading a row to real methodology="monte_carlo" is future
work, not attempted here.

Tier-2's checkpoints are daily; Transition ticks monthly. Each
`monthly_soh_delta_pct` entry is the INCREMENTAL loss in that calendar
month (not cumulative), aggregated by sampling the daily checkpoint
closest to each month's end (round(month * 365/12) days in) -- a real
down-sampling, not a 1:1 field mapping, per seam_reconnaissance.md §2's
own note that this conversion would be needed.

Usage:
    uv run python -m examples.generate_seam_table
    uv run python -m examples.generate_seam_table --duration-years 20 --output examples/seam_table.json
"""

from __future__ import annotations

import argparse
import datetime
from pathlib import Path

from ev_thermal.long_horizon import characterize_all_trips, simulate_long_horizon
from ev_thermal.profile_loader import load_profile
from ev_thermal.schema import LongHorizonCheckpoint, SeamTable, SeamTableRow
from examples.validate_blue_bird_vision_electric import (
    PROFILE_PATH,
    SEED,
    build_scenario,
    columbus_climate,
    school_bus_day_type_mix,
)

DEFAULT_DURATION_YEARS = 20
DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "seam_table.json"
DAYS_PER_MONTH = 365.0 / 12.0

VEHICLE_CLASS = "blue_bird_vision_electric"
CLIMATE_ZONE = "columbus_oh"
DUTY_CYCLE_ARCHETYPE = "default"

CAVEATS = [
    "methodology=point_estimate is NOT a median -- see mc.py's own 'p50 vs. the point estimate' "
    "note; an unperturbed point estimate and an MC median diverge, more so at longer horizons.",
    "typical_end_soc=1.0 (this profile's real depot-charging convention, Q-0022) sits outside "
    "aging.py's calendar-aging calibration range (the two fleet anchors were derived under "
    "constant 80%/60% SOC, never 100%).",
    "Calendar aging's linear-in-time formulation (no saturation term) is a known extrapolation "
    "limit for a 20-year horizon, 2-4x past the calibration anchors' own 5-10 year window (Q-0017).",
    "This profile's seven aging-rate coefficients, shared with the six passenger-EV profiles, have "
    "never been validated against any commercial/heavy-duty vehicle's chemistry or duty cycle.",
    "cooling.py's chiller path is real and validated (Q-0021) but Columbus's climate doesn't get "
    "hot enough to meaningfully engage it -- these numbers carry the same radiator-dominated "
    "thermal behavior as a non-chiller-equipped profile would show under this specific climate.",
    "cycle_aging_rate_per_efc's 75kWh reference-pack-capacity assumption (Q-0006/Q-0007) scales "
    "this profile's own cycle-aging output EXACTLY linearly (confirmed empirically: cycle loss at "
    "50/100/200kWh is 0.66x/1.33x/2.67x the 75kWh value) -- currently a modest effect on the total "
    "(cycle is 1.21pp of 73.74pp total loss at 20yr under this profile's own school-route duty "
    "cycle, calendar-dominated), but would become load-bearing for any future scenario with "
    "heavier cycling/fast-charge exposure than this one has.",
]


def _monthly_soh_deltas_pct(checkpoints: list[LongHorizonCheckpoint], duration_years: int) -> list[float]:
    """Aggregates Tier-2's daily checkpoints into INCREMENTAL monthly SOH
    loss (percentage points), one entry per calendar month since
    conversion. See this module's own docstring for why this is a real
    down-sampling, not a 1:1 mapping.
    """
    n_months = duration_years * 12
    cumulative_loss_pct = [0.0]
    for month in range(1, n_months + 1):
        day_index = min(round(month * DAYS_PER_MONTH) - 1, len(checkpoints) - 1)
        cumulative_loss_pct.append((1.0 - checkpoints[day_index].battery_soh) * 100.0)
    return [cumulative_loss_pct[m] - cumulative_loss_pct[m - 1] for m in range(1, n_months + 1)]


def generate_blue_bird_columbus_row(duration_years: int) -> SeamTableRow:
    profile = load_profile(PROFILE_PATH)
    climate = columbus_climate()
    scenario = build_scenario(profile, climate, SEED)
    day_type_mix = school_bus_day_type_mix()

    characterizations = characterize_all_trips(profile, scenario, n_buckets=4)
    result = simulate_long_horizon(
        profile=profile,
        trip_characterizations=characterizations,
        day_type_mix=day_type_mix,
        climate=climate,
        charging_profile=scenario.charging_profile,
        duration_years=duration_years,
        seed=SEED,
    )

    return SeamTableRow(
        vehicle_class=VEHICLE_CLASS,
        climate_zone=CLIMATE_ZONE,
        duty_cycle_archetype=DUTY_CYCLE_ARCHETYPE,
        methodology="point_estimate",
        monthly_soh_delta_pct=_monthly_soh_deltas_pct(result.checkpoints, duration_years),
        source_seed=SEED,
        source_duration_years=duration_years,
        caveats=CAVEATS,
        generated_at=datetime.date.today().isoformat(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration-years", type=int, default=DEFAULT_DURATION_YEARS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    row = generate_blue_bird_columbus_row(args.duration_years)
    table = SeamTable(table_version="1", rows=[row])

    args.output.write_text(table.model_dump_json(indent=2))

    print(f"Row: {row.vehicle_class} / {row.climate_zone} / {row.duty_cycle_archetype}")
    print(f"Methodology: {row.methodology}  duration_years={row.source_duration_years}  seed={row.source_seed}")
    cumulative_pct = sum(row.monthly_soh_delta_pct)
    print(f"Total months: {len(row.monthly_soh_delta_pct)}  Cumulative SOH loss: {cumulative_pct:.4f}%")
    print(f"First 3 monthly deltas (pp): {[round(v, 5) for v in row.monthly_soh_delta_pct[:3]]}")
    print(f"Last 3 monthly deltas (pp):  {[round(v, 5) for v in row.monthly_soh_delta_pct[-3:]]}")
    print(f"Caveats attached: {len(row.caveats)}")
    print(f"Written to: {args.output}")


if __name__ == "__main__":
    main()
