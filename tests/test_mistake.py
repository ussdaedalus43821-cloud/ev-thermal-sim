"""Behavioral tests for `ev_thermal.mistake`'s v1 case runner."""

from __future__ import annotations

from examples.flagship_comparison import default_day_type_mix
from examples.mistake_library_leaf_battery_cooling import build_case

from ev_thermal.mistake import run_mistake_case


def test_run_mistake_case_produces_both_results_and_a_consistent_delta() -> None:
    case = build_case()
    result = run_mistake_case(case, default_day_type_mix(), duration_years=1, seed=42)

    assert result.case_name == case.name
    assert len(result.failure_result.checkpoints) == len(result.counterfactual_result.checkpoints)
    assert result.final_soh_failure == result.failure_result.checkpoints[-1].battery_soh
    assert result.final_soh_counterfactual == result.counterfactual_result.checkpoints[-1].battery_soh
    assert result.soh_delta == result.final_soh_counterfactual - result.final_soh_failure


def test_run_mistake_case_counterfactual_never_ages_worse_than_failure() -> None:
    """The whole point of this MistakeCase is that active battery cooling
    doesn't hurt -- soh_delta should never be negative, even though its
    magnitude is small under this scenario's specific conditions (see
    docs/DESIGN_QUESTIONS.md Q-0020's radiator-vs-ambient finding).
    """
    case = build_case()
    result = run_mistake_case(case, default_day_type_mix(), duration_years=1, seed=42)
    assert result.soh_delta >= 0.0
