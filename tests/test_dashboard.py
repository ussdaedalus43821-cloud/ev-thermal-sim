"""Smoke test for the Streamlit dashboard: the UI itself isn't exercised (Streamlit
apps need a running server/session to test meaningfully), but importing app.py
must not raise, and run_scenario's signature must stay compatible with how the
dashboard calls it. `_dedupe_consecutive_events` is plain Python logic (no
Streamlit dependency), so it's tested directly.
"""

from __future__ import annotations

import importlib
import inspect

from ev_thermal.dashboard.app import _dedupe_consecutive_events, soaked_to_ambient, summarize_run
from ev_thermal.profile_loader import PROFILES_DIR, load_profile
from ev_thermal.schema import CausalEvent
from ev_thermal.sim import run_scenario


def _event(sim_time_s: float, event_type: str = "MOTOR_LIMIT_TRIGGERED", description: str = "same") -> CausalEvent:
    return CausalEvent(
        event_id=f"evt-{sim_time_s}",
        sim_time_s=sim_time_s,
        event_type=event_type,
        description=description,
        affected_component="motor",
        severity="warning",
    )


def test_app_imports_without_error() -> None:
    importlib.import_module("ev_thermal.dashboard.app")


def test_run_scenario_signature_has_dashboard_parameters() -> None:
    params = inspect.signature(run_scenario).parameters
    expected = (
        "profile",
        "scenario",
        "duration_s",
        "target_speed_mps",
        "driver_aggression",
        "grade_rad",
        "pump_aggressiveness",
    )
    for name in expected:
        assert name in params


def test_dedupe_merges_identical_consecutive_events_with_count_and_span() -> None:
    events = [_event(10.0), _event(11.0), _event(12.0)]

    groups = _dedupe_consecutive_events(events)

    assert len(groups) == 1
    assert groups[0].fired_count == 3
    assert groups[0].first_sim_time_s == 10.0
    assert groups[0].last_sim_time_s == 12.0


def test_dedupe_keeps_differing_descriptions_separate() -> None:
    events = [_event(10.0, description="a"), _event(11.0, description="b"), _event(12.0, description="a")]

    groups = _dedupe_consecutive_events(events)

    assert len(groups) == 3
    assert all(group.fired_count == 1 for group in groups)


def test_dedupe_does_not_merge_non_consecutive_matching_events() -> None:
    events = [_event(10.0), _event(11.0, event_type="BATTERY_LIMIT_TRIGGERED"), _event(12.0)]

    groups = _dedupe_consecutive_events(events)

    assert len(groups) == 3
    assert all(group.fired_count == 1 for group in groups)


def test_soaked_to_ambient_patches_battery_motor_inverter_initial_temp() -> None:
    """Q-0030 regression guard: run_scenario's thermal-node initial_temp_k
    previously ignored the dashboard's own ambient slider entirely, so every
    run silently started ~20C off from what the user actually set.
    """
    profile = load_profile(PROFILES_DIR / "nissan_leaf.yaml")
    ambient_k = 318.15  # 45C

    patched = soaked_to_ambient(profile, ambient_temp_k=ambient_k)

    assert patched.battery.thermal_node.initial_temp_k == ambient_k
    assert patched.inverter.thermal_node.initial_temp_k == ambient_k
    assert all(motor.thermal_node.initial_temp_k == ambient_k for motor in patched.motors)
    # Untouched: this isn't a blanket profile mutation.
    assert patched.battery.capacity_nominal_ah == profile.battery.capacity_nominal_ah
    assert patched.name == profile.name


def test_soaked_to_ambient_does_not_mutate_the_original_profile() -> None:
    profile = load_profile(PROFILES_DIR / "nissan_leaf.yaml")
    original_initial_temp_k = profile.battery.thermal_node.initial_temp_k

    soaked_to_ambient(profile, ambient_temp_k=318.15)

    assert profile.battery.thermal_node.initial_temp_k == original_initial_temp_k


def test_summarize_run_flags_a_derate_crossing() -> None:
    profile = load_profile(PROFILES_DIR / "nissan_leaf.yaml")
    profile = soaked_to_ambient(profile, ambient_temp_k=318.15)  # 45C, at the battery's own derate limit
    scenario_module = importlib.import_module("ev_thermal.dashboard.app")
    scenario = scenario_module.build_scenario(profile, ambient_temp_k=318.15)
    result = run_scenario(
        profile,
        scenario,
        duration_s=1800.0,
        target_speed_mps=scenario_module.target_speed_mps,
        driver_aggression=1.3,
        grade_rad=0.0,
        pump_aggressiveness=1.0,
    )
    battery_name = profile.battery.thermal_node.name
    motor_name = profile.motors[0].thermal_node.name
    inverter_name = profile.inverter.thermal_node.name

    summary = summarize_run(result, profile, motor_name, inverter_name, battery_name)

    assert "battery" in summary
    assert "crossed its overheating limit" in summary
    # The formatting bug this test also guards against: each crossed
    # component's own bold markers must not be double-wrapped.
    assert "****" not in summary
