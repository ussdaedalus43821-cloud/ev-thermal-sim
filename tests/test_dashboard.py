"""Smoke test for the Streamlit dashboard: the UI itself isn't exercised (Streamlit
apps need a running server/session to test meaningfully), but importing app.py
must not raise, and run_scenario's signature must stay compatible with how the
dashboard calls it.
"""

from __future__ import annotations

import importlib
import inspect

from ev_thermal.sim import run_scenario


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
