"""Smoke test for the Streamlit dashboard: the UI itself isn't exercised (Streamlit
apps need a running server/session to test meaningfully), but importing app.py
must not raise, and run_scenario's signature must stay compatible with how the
dashboard calls it. `_dedupe_consecutive_events` is plain Python logic (no
Streamlit dependency), so it's tested directly.
"""

from __future__ import annotations

import importlib
import inspect

from ev_thermal.dashboard.app import _dedupe_consecutive_events
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
