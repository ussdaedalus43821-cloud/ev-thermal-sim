"""Behavioral tests for `ev_thermal.fork`'s v1 (full-fidelity) branching.

Reuses `tests/test_sim.py`'s `_make_profile`/`_make_scenario` fixtures,
matching this project's established cross-module fixture-reuse pattern.
"""

from __future__ import annotations

import copy

import pytest

from ev_thermal.fork import apply_override, apply_overrides, branch, create_fork_point
from ev_thermal.sim import run_scenario
from tests.test_sim import _make_profile, _make_scenario


def test_apply_override_targets_profile_leaf() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    forked = apply_override(scenario, "profile.battery.capacity_nominal_ah", 999.0)

    assert forked.manufacturer_profile is not None
    assert scenario.manufacturer_profile is not None
    assert forked.manufacturer_profile.battery.capacity_nominal_ah.value == 999.0
    assert scenario.manufacturer_profile.battery.capacity_nominal_ah.value != 999.0


def test_apply_override_targets_climate_leaf() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile, ambient_temp_k=298.15)

    forked = apply_override(scenario, "climate.mean_annual_temp_k", 320.0)

    assert forked.climate.mean_annual_temp_k.value == 320.0
    assert scenario.climate.mean_annual_temp_k.value == 298.15


def test_apply_override_targets_usage_profile_leaf() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    forked = apply_override(scenario, "usage_profile.annual_mileage_km", 30_000.0)

    assert forked.usage_profile.annual_mileage_km.value == 30_000.0
    assert scenario.usage_profile.annual_mileage_km.value != 30_000.0


def test_apply_override_targets_charging_profile_leaf() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    forked = apply_override(scenario, "charging_profile.typical_end_soc", 0.95)

    assert forked.charging_profile.typical_end_soc.value == 0.95
    assert scenario.charging_profile.typical_end_soc.value != 0.95


def test_apply_override_rejects_paths_deeper_than_one_level_below_top() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    # 4 segments -- exceeds the ratified 2-or-3-segment shape (Q-0018).
    with pytest.raises(ValueError, match="2 or 3 dotted segments"):
        apply_override(scenario, "profile.thermal_architecture.cooling_loops.0", 1.0)

    # 3 segments, but the resolved sub-object field is a list, not a leaf.
    with pytest.raises(ValueError, match="not a leaf value"):
        apply_override(scenario, "profile.thermal_architecture.cooling_loops", [])

    # Unknown top-level prefix.
    with pytest.raises(ValueError, match="must start with one of"):
        apply_override(scenario, "vehicle_dynamics.mass_kg", 1000.0)


def test_apply_overrides_applies_every_entry() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)

    forked = apply_overrides(
        scenario,
        {
            "profile.battery.capacity_nominal_ah": 999.0,
            "climate.mean_annual_temp_k": 320.0,
        },
    )

    assert forked.manufacturer_profile is not None
    assert forked.manufacturer_profile.battery.capacity_nominal_ah.value == 999.0
    assert forked.climate.mean_annual_temp_k.value == 320.0


def test_fork_produces_genuine_divergence() -> None:
    """Mirrors `test_hot_ambient_produces_higher_peak_component_temps_than_cool`'s
    already-validated hot-vs-cool mechanism (radiator heat rejection weakens
    at higher ambient), but reaches it via a real fork instead of two
    independently-constructed scenarios -- proving the fork mechanism itself
    produces genuine physical divergence, not just a code path that runs.
    """
    profile = _make_profile()
    scenario = _make_scenario(profile, ambient_temp_k=293.15)  # 20C

    first_leg = run_scenario(profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 20.0)
    fork_point = create_fork_point(
        first_leg.final_state, overrides={"climate.mean_annual_temp_k": 313.15}, description="what if it got hot"
    )
    hot_scenario, hot_state = branch(scenario, first_leg.final_state, fork_point)

    unforked_continuation = run_scenario(
        profile, scenario, duration_s=120.0, target_speed_mps=lambda _t: 20.0, initial_state=first_leg.final_state
    )
    forked_continuation = run_scenario(
        profile.model_copy(), hot_scenario, duration_s=120.0, target_speed_mps=lambda _t: 20.0, initial_state=hot_state
    )

    unforked_peaks = {n: max(v) for n, v in unforked_continuation.time_series.node_temps_k.items()}
    forked_peaks = {n: max(v) for n, v in forked_continuation.time_series.node_temps_k.items()}
    for node_name, unforked_peak in unforked_peaks.items():
        assert forked_peaks[node_name] >= unforked_peak
    assert any(forked_peaks[name] > unforked_peaks[name] for name in unforked_peaks)


def test_fork_does_not_mutate_parent() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    first_leg = run_scenario(profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0)
    parent_state_snapshot = copy.deepcopy(first_leg.final_state)
    parent_scenario_snapshot = copy.deepcopy(scenario)

    fork_point = create_fork_point(
        first_leg.final_state, overrides={"climate.mean_annual_temp_k": 330.0}, description="hot"
    )
    child_scenario, child_state = branch(scenario, first_leg.final_state, fork_point)
    run_scenario(profile, child_scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0, initial_state=child_state)

    assert first_leg.final_state == parent_state_snapshot
    assert scenario == parent_scenario_snapshot


def test_fork_of_a_fork_accumulates_lineage_and_diverges_state_ids() -> None:
    profile = _make_profile()
    scenario = _make_scenario(profile)
    first_leg = run_scenario(profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0)

    fork_point_1 = create_fork_point(
        first_leg.final_state, overrides={"climate.mean_annual_temp_k": 310.0}, description="warmer"
    )
    scenario_1, state_1 = branch(scenario, first_leg.final_state, fork_point_1)
    leg_1 = run_scenario(profile, scenario_1, duration_s=30.0, target_speed_mps=lambda _t: 15.0, initial_state=state_1)

    fork_point_2 = create_fork_point(
        leg_1.final_state, overrides={"climate.mean_annual_temp_k": 330.0}, description="even warmer"
    )
    scenario_2, state_2 = branch(scenario_1, leg_1.final_state, fork_point_2)
    leg_2 = run_scenario(profile, scenario_2, duration_s=30.0, target_speed_mps=lambda _t: 15.0, initial_state=state_2)

    unforked_continuation = run_scenario(
        profile, scenario, duration_s=30.0, target_speed_mps=lambda _t: 15.0, initial_state=first_leg.final_state
    )

    assert state_2.fork_lineage == [fork_point_1.fork_id, fork_point_2.fork_id]
    state_ids = {
        unforked_continuation.final_state.state_id,
        leg_1.final_state.state_id,
        leg_2.final_state.state_id,
    }
    assert len(state_ids) == 3
