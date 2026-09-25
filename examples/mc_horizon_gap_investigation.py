"""Reproducible runner for the investigation into why `MCResult.soh_band`'s
`p50` diverges from an unperturbed point-estimate run's SOH at the same
checkpoint, and why that gap grows with horizon.

This investigation was triggered by the first genuine 20-year Monte Carlo
run of the Nissan Leaf / Guam scenario (see docs/MILESTONES.md's "Multi-year Monte
Carlo v1" entry): its p50 at year 20 sat ~6.3% (relative) below the
already-reviewed 20-year point estimate (47.279% SOH, see
`docs/DESIGN_QUESTIONS.md` Q-0017), while the existing 90-day test
(`tests/test_mc.py::test_mc_p50_approximates_flagship_point_estimate`)
still passed its 5%-relative tolerance. Two candidate explanations were
distinguished by evidence, not reasoning:

1. **Horizon-dependent non-linearity** (Jensen's-inequality effect of
   compounding a convex Arrhenius-exponential aging term over an
   increasing number of years under log-normal coefficient sampling): the
   gap should grow smoothly and monotonically with horizon.
2. **Parked-SOC-model interaction artifact** (the point estimate was
   measured post-parked-SOC-fix, but the 90-day p50 test predates that
   fix's own validation at this specific comparison): the gap should
   appear abruptly at long horizons, not grow smoothly from a small base.

This script reproduces the evidence that distinguished them: it runs both
an unperturbed point estimate and a full Monte Carlo (same seeds, same
`n_runs`) at each of several horizons and reports the point-estimate-vs-p50
relative gap at each. The result was smooth, monotonic growth (~0.03% at
90 days -> ~6.3% at 20 years) with no discontinuity, confirming
explanation 1 -- see `ev_thermal.mc`'s module docstring ("p50 vs. the
point estimate") for the full mechanism this pointed to, and
`docs/model_reference.md`'s "Monte Carlo p50 vs. the point estimate"
section for the reader-facing summary.

Reuses `examples/flagship_comparison.py`'s scenario builders and
`examples/monte_carlo_leaf_guam.py`'s aging-parameter list directly (not
re-declared), so this script's seeds and scenario are guaranteed identical
to both of those scripts' own records.

Usage (run as a module, not a bare script path -- same reason as
`examples/monte_carlo_leaf_guam.py`, it imports `examples.
flagship_comparison`, which needs the repo root on `sys.path`):
    uv run python -m examples.mc_horizon_gap_investigation
    uv run python -m examples.mc_horizon_gap_investigation --duration-years 1,5,10,20 --n-runs 500

At N=500 across the default four `--duration-years` values plus the
built-in 90-day checkpoint (extracted from the 1-year run, no extra run
needed), this takes roughly 3-4 minutes -- run it in the foreground with a
bounded timeout, not backgrounded.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ev_thermal.long_horizon import characterize_all_trips, simulate_long_horizon
from ev_thermal.mc import run_monte_carlo
from ev_thermal.profile_loader import load_profile
from ev_thermal.schema import MCConfig
from examples.flagship_comparison import DEFAULT_PROFILE_A, build_scenario, default_day_type_mix, guam_climate
from examples.monte_carlo_leaf_guam import AGING_PARAMETER_PATHS, DEFAULT_MC_SEED, DEFAULT_N_RUNS, DEFAULT_SCHEDULE_SEED

NINETY_DAY_INDEX = 89


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", type=Path, default=Path(DEFAULT_PROFILE_A))
    parser.add_argument("--n-runs", type=int, default=DEFAULT_N_RUNS)
    parser.add_argument("--mc-seed", type=int, default=DEFAULT_MC_SEED, help="Seeds coefficient sampling only.")
    parser.add_argument(
        "--schedule-seed",
        type=int,
        default=DEFAULT_SCHEDULE_SEED,
        help="Seeds the day-type schedule, held fixed across every sample and every horizon below.",
    )
    parser.add_argument(
        "--duration-years",
        type=str,
        default="1,5,10,20",
        help="Comma-separated list of duration_years to check the final checkpoint of. "
        "The 90-day checkpoint is always additionally reported, extracted from the "
        "duration_years=1 run at no extra cost, if 1 is included in this list.",
    )
    args = parser.parse_args()

    duration_years_list = [int(v) for v in args.duration_years.split(",")]

    profile = load_profile(args.profile)
    climate = guam_climate()
    scenario = build_scenario(profile, climate, args.schedule_seed)
    day_type_mix = default_day_type_mix()

    print(f"{profile.name} / {climate.location_name}")
    print(f"n_runs={args.n_runs}  mc_seed={args.mc_seed}  schedule_seed={args.schedule_seed}")
    print()
    print(f"{'horizon':>10}{'point_soh':>12}{'mc_p50':>12}{'gap_rel%':>10}{'mc_p05':>10}{'mc_p95':>10}")

    rows: list[tuple[str, int, int | None]] = []
    for duration_years in duration_years_list:
        if duration_years == 1:
            rows.append(("90-day", 1, NINETY_DAY_INDEX))
        rows.append((f"{duration_years}-year", duration_years, None))

    for label, duration_years, day_index in rows:
        characterizations = characterize_all_trips(profile, scenario, n_buckets=4)
        point_result = simulate_long_horizon(
            profile=profile,
            trip_characterizations=characterizations,
            day_type_mix=day_type_mix,
            climate=climate,
            charging_profile=scenario.charging_profile,
            duration_years=duration_years,
            seed=args.schedule_seed,
        )
        idx = day_index if day_index is not None else len(point_result.checkpoints) - 1
        point_soh = point_result.checkpoints[idx].battery_soh

        config = MCConfig(
            n_runs=args.n_runs,
            parameters_to_vary=AGING_PARAMETER_PATHS,
            random_seed=args.mc_seed,
            parallel=False,
            output_metrics=["final_soh"],
        )
        mc_result = run_monte_carlo(profile, scenario, day_type_mix, config, duration_years=duration_years)
        mc_idx = day_index if day_index is not None else len(mc_result.soh_band) - 1
        band = mc_result.soh_band[mc_idx]

        gap_rel = (point_soh - band.p50) / point_soh
        print(
            f"{label:>10}{point_soh * 100:>11.4f}%{band.p50 * 100:>11.4f}%{gap_rel * 100:>9.3f}%"
            f"{band.p05 * 100:>9.4f}%{band.p95 * 100:>9.4f}%"
        )


if __name__ == "__main__":
    main()
