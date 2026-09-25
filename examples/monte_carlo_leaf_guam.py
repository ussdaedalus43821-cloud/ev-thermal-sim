"""Reproducible runner for this project's first Monte Carlo run: the Nissan
Leaf in a Guam-like climate, with battery aging coefficients sampled per
Milestone "Monte Carlo v1" (see docs/DESIGN_QUESTIONS.md Q-0008 through
Q-0011, and `ev_thermal.mc`'s module docstring for the sampling methodology
and its two explicit caveats).

Reuses `examples/flagship_comparison.py`'s scenario builders directly
(climate, day-type mix, charging profile, scenario) rather than
re-declaring them, so this run's schedule seed is guaranteed identical to
the flagship comparison's -- both a reproducibility convenience and a
correctness requirement (see `mc.py`'s docstring: the MC band's day-type
schedule must be held fixed, not resampled per draw).

Usage (run as a module, not a bare script path -- it imports
examples.flagship_comparison, which needs the repo root on sys.path;
`python examples/monte_carlo_leaf_guam.py` puts examples/ itself there
instead and fails with ModuleNotFoundError):
    uv run python -m examples.monte_carlo_leaf_guam
    uv run python -m examples.monte_carlo_leaf_guam --n-runs 2000 --duration-days 30
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ev_thermal.mc import run_monte_carlo
from ev_thermal.profile_loader import load_profile
from ev_thermal.schema import MCConfig
from examples.flagship_comparison import (
    DEFAULT_PROFILE_A,
    build_scenario,
    default_day_type_mix,
    guam_climate,
)

DEFAULT_N_RUNS = 500
DEFAULT_MC_SEED = 999
DEFAULT_SCHEDULE_SEED = 42
DEFAULT_DURATION_DAYS = 90

AGING_PARAMETER_PATHS = [
    "battery.calendar_aging_rate_per_s",
    "battery.calendar_aging_activation_temp_k",
    "battery.calendar_aging_soc_sensitivity",
    "battery.cycle_aging_rate_per_efc",
    "battery.cycle_aging_activation_temp_k",
    "battery.cycle_aging_c_rate_sensitivity",
    "battery.resistance_growth_per_soh_loss",
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", type=Path, default=Path(DEFAULT_PROFILE_A))
    parser.add_argument("--n-runs", type=int, default=DEFAULT_N_RUNS)
    parser.add_argument("--mc-seed", type=int, default=DEFAULT_MC_SEED, help="Seeds coefficient sampling only.")
    parser.add_argument(
        "--schedule-seed",
        type=int,
        default=DEFAULT_SCHEDULE_SEED,
        help="Seeds the day-type schedule, held fixed across every sample.",
    )
    parser.add_argument("--duration-days", type=int, default=DEFAULT_DURATION_DAYS)
    args = parser.parse_args()

    profile = load_profile(args.profile)
    climate = guam_climate()
    scenario = build_scenario(profile, climate, args.schedule_seed)
    day_type_mix = default_day_type_mix()

    config = MCConfig(
        n_runs=args.n_runs,
        parameters_to_vary=AGING_PARAMETER_PATHS,
        random_seed=args.mc_seed,
        parallel=False,
        output_metrics=["final_soh", "final_calendar_loss", "final_cycle_loss"],
    )

    result = run_monte_carlo(profile, scenario, day_type_mix, config, duration_years=1)

    print(f"{profile.name} / {climate.location_name}")
    print(f"n_runs={args.n_runs}  mc_seed={args.mc_seed}  schedule_seed={args.schedule_seed}")
    if args.n_runs < 500:
        print("NOTE: n_runs < 500 -- p05/p95 sampling error is larger than this project's own N=500 target.")
    print()
    print(f"{'day':>5}{'p05':>10}{'p50':>10}{'p95':>10}")
    checkpoint_days = sorted({0, args.duration_days // 3, 2 * args.duration_days // 3, args.duration_days - 1})
    for day_index in checkpoint_days:
        band = result.soh_band[day_index]
        print(f"{day_index:>5}{band.p05 * 100:>9.4f}%{band.p50 * 100:>9.4f}%{band.p95 * 100:>9.4f}%")

    final_metrics = [s.endpoint_metrics["final_soh"] for s in result.samples]
    print()
    print(
        f"final_soh across {len(final_metrics)} samples: min={min(final_metrics) * 100:.4f}%  "
        f"max={max(final_metrics) * 100:.4f}%"
    )


if __name__ == "__main__":
    main()
