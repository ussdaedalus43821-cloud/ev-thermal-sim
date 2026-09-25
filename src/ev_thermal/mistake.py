"""Mistake library v1: runs a `MistakeCase` and produces the paired
long-horizon comparison that IS its lesson.

## Architecture: two independent long-horizon runs, not a fork

`MistakeCase`'s own docstring ("the only variable is the design decision
being taught") settles this: `failure_profile` and `counterfactual_profile`
are each run from t=0 under the SAME `case.scenario` climate/usage/charging
context and the same `day_type_mix`/`duration_years`/`seed` — this is NOT
`fork.py`'s mid-run branching mechanism (which explores what-ifs from a
live simulated moment), and it is NOT a comparison between two genuinely
different real vehicles (which would reintroduce exactly the multi-variable
confound this project has eliminated elsewhere — EFC normalization, shared
aging coefficients, shared day-type mix). `Scenario.manufacturer_profile`
is nullable specifically so `MistakeCase.scenario` can supply everything
except the vehicle, which each run substitutes in independently via a
single `model_copy`.

`run_mistake_case` is pure orchestration: it calls `long_horizon.
characterize_all_trips`/`simulate_long_horizon` unmodified, once per
profile — the same functions `examples/flagship_comparison.py` already
calls, now formalized into a reusable core function instead of ad hoc
script code, per this project's "physics lives in core, not UI" rule.
"""

from __future__ import annotations

from ev_thermal.long_horizon import characterize_all_trips, simulate_long_horizon
from ev_thermal.schema import AnnualDayTypeMix, MistakeCase, MistakeCaseResult


def run_mistake_case(
    case: MistakeCase,
    day_type_mix: list[AnnualDayTypeMix],
    duration_years: int,
    seed: int,
) -> MistakeCaseResult:
    """Runs `case.failure_profile` and `case.counterfactual_profile` under
    `case.scenario`'s shared climate/usage/charging context, and returns
    both full results plus the computed SOH delta between them.
    """
    failure_scenario = case.scenario.model_copy(update={"manufacturer_profile": case.failure_profile})
    failure_characterizations = characterize_all_trips(case.failure_profile, failure_scenario)
    failure_result = simulate_long_horizon(
        profile=case.failure_profile,
        trip_characterizations=failure_characterizations,
        day_type_mix=day_type_mix,
        climate=case.scenario.climate,
        charging_profile=case.scenario.charging_profile,
        duration_years=duration_years,
        seed=seed,
    )

    counterfactual_scenario = case.scenario.model_copy(update={"manufacturer_profile": case.counterfactual_profile})
    counterfactual_characterizations = characterize_all_trips(case.counterfactual_profile, counterfactual_scenario)
    counterfactual_result = simulate_long_horizon(
        profile=case.counterfactual_profile,
        trip_characterizations=counterfactual_characterizations,
        day_type_mix=day_type_mix,
        climate=case.scenario.climate,
        charging_profile=case.scenario.charging_profile,
        duration_years=duration_years,
        seed=seed,
    )

    final_soh_failure = failure_result.checkpoints[-1].battery_soh
    final_soh_counterfactual = counterfactual_result.checkpoints[-1].battery_soh

    return MistakeCaseResult(
        case_name=case.name,
        failure_result=failure_result,
        counterfactual_result=counterfactual_result,
        final_soh_failure=final_soh_failure,
        final_soh_counterfactual=final_soh_counterfactual,
        soh_delta=final_soh_counterfactual - final_soh_failure,
    )
