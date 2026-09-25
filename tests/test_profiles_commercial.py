"""Validation tests for the commercial-vehicle profiles
(configs/profiles_commercial/*.yaml) — a separate, parallel track from
tests/test_profiles.py's six passenger-EV reference profiles, not an
extension of it. See configs/profiles_commercial/blue_bird_vision_electric
.yaml's own header comment and docs/research/commercial_profiles_v1.md for
why this is a distinct directory/test file rather than a "seventh" entry in
PROFILE_STEMS.

Mirrors test_profiles.py's checks (same rigor, same helper shapes) rather
than importing them — the two tracks are deliberately decoupled so a future
change to one doesn't silently reach into the other.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from ev_thermal.profile_loader import load_all_profiles
from ev_thermal.schema import (
    ChargingProfile,
    Climate,
    Confidence,
    DTPolicy,
    ManufacturerProfile,
    Scenario,
    Sourced,
    ThermalNode,
    UsageProfile,
)
from ev_thermal.sim import run_scenario

COMMERCIAL_PROFILES_DIR = Path(__file__).resolve().parent.parent / "configs" / "profiles_commercial"
PROFILE_STEMS = ["blue_bird_vision_electric"]


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="test-fixture")


def _make_scenario(profile: ManufacturerProfile, seed: int = 42, ambient_temp_k: float = 298.15) -> Scenario:
    """Same minimal, deliberately-generic Scenario wrapper as
    test_profiles.py's own _make_scenario — exercises run_scenario end to
    end, not this profile's own researched climate/usage assumptions (this
    profile doesn't populate those; commercial-vehicle usage/charging
    profiles are a separate, later piece of the Transition-seam work).
    """
    climate = Climate(
        location_name="Test City",
        mean_annual_temp_k=_sourced(ambient_temp_k),
        seasonal_amplitude_k=_sourced(10.0),
        daily_amplitude_k=_sourced(5.0),
        altitude_m=_sourced(100.0),
        solar_irradiance_w_per_m2=_sourced(800.0),
    )
    usage_profile = UsageProfile(
        annual_mileage_km=_sourced(15_000.0),
        mean_trip_km=_sourced(20.0),
        trip_km_std=_sourced(10.0),
        driving_style="normal",
        garage_parked=True,
    )
    charging_profile = ChargingProfile(
        charger_type="Level 2",
        charge_power_kw=_sourced(11.0),
        typical_start_soc=_sourced(0.5),
        typical_end_soc=_sourced(0.8),
        dc_fast_charge_fraction=_sourced(0.1),
        preconditioning_enabled=False,
        dc_charge_power_kw=_sourced(50.0),
        charge_reset_hour=_sourced(22.0),
    )
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")
    return Scenario(
        name=f"{profile.name}-smoke-test",
        manufacturer_profile=profile,
        climate=climate,
        usage_profile=usage_profile,
        charging_profile=charging_profile,
        duration_years=1,
        dt_policy=dt_policy,
        random_seed=seed,
    )


def _iter_sourced(obj: Any) -> Iterator[Sourced[Any]]:
    if obj is None:
        return
    if isinstance(obj, Sourced):
        yield obj
        return
    if isinstance(obj, BaseModel):
        for field_name in type(obj).model_fields:
            yield from _iter_sourced(getattr(obj, field_name))
        return
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_sourced(value)
        return
    if isinstance(obj, list | tuple):
        for value in obj:
            yield from _iter_sourced(value)
        return


def _iter_thermal_nodes(profile: ManufacturerProfile) -> Iterator[ThermalNode]:
    yield profile.battery.thermal_node
    for motor in profile.motors:
        yield motor.thermal_node
    yield profile.inverter.thermal_node


@pytest.fixture(scope="module")
def profiles() -> dict[str, ManufacturerProfile]:
    return load_all_profiles(COMMERCIAL_PROFILES_DIR)


def test_all_commercial_profile_files_exist() -> None:
    stems = {path.stem for path in COMMERCIAL_PROFILES_DIR.glob("*.yaml")}
    assert stems == set(PROFILE_STEMS)


def test_all_commercial_yamls_load_via_the_profile_loader(profiles: dict[str, ManufacturerProfile]) -> None:
    assert set(profiles.keys()) == set(PROFILE_STEMS)
    for profile in profiles.values():
        assert isinstance(profile, ManufacturerProfile)


@pytest.mark.parametrize("stem", PROFILE_STEMS)
def test_every_sourced_field_has_nonempty_source_and_confidence(
    stem: str, profiles: dict[str, ManufacturerProfile]
) -> None:
    profile = profiles[stem]
    sourced_fields = list(_iter_sourced(profile))
    assert sourced_fields, f"{stem}: expected at least one Sourced field"
    for sourced in sourced_fields:
        assert isinstance(sourced.confidence, Confidence)
        assert sourced.source.strip() != "", f"{stem}: found a Sourced field with an empty source"


@pytest.mark.parametrize("stem", PROFILE_STEMS)
def test_gross_kwh_at_least_usable_kwh(stem: str, profiles: dict[str, ManufacturerProfile]) -> None:
    battery = profiles[stem].battery
    assert battery.capacity_kwh_nominal.value >= battery.capacity_kwh_usable.value


@pytest.mark.parametrize("stem", PROFILE_STEMS)
def test_motor_peak_power_and_torque_are_positive(stem: str, profiles: dict[str, ManufacturerProfile]) -> None:
    for motor in profiles[stem].motors:
        assert motor.peak_power_kw.value > 0.0
        assert motor.peak_torque_nm.value > 0.0


@pytest.mark.parametrize("stem", PROFILE_STEMS)
def test_derate_temp_below_max_temp_for_every_thermal_node(stem: str, profiles: dict[str, ManufacturerProfile]) -> None:
    for node in _iter_thermal_nodes(profiles[stem]):
        assert node.derate_temp_k.value < node.max_temp_k.value, f"{stem}: {node.name}"


@pytest.mark.parametrize("stem", PROFILE_STEMS)
def test_every_cooling_loop_serves_entry_matches_a_real_thermal_node_name(
    stem: str, profiles: dict[str, ManufacturerProfile]
) -> None:
    profile = profiles[stem]
    valid_names = {
        profile.battery.thermal_node.name,
        profile.inverter.thermal_node.name,
        *(motor.thermal_node.name for motor in profile.motors),
    }
    for loop in profile.thermal_architecture.cooling_loops:
        for served_name in loop.serves:
            assert served_name in valid_names, (
                f"{stem}: {loop.name}.serves references {served_name!r}, "
                f"which doesn't match any ThermalNode.name ({sorted(valid_names)})"
            )


@pytest.mark.parametrize("stem", PROFILE_STEMS)
def test_battery_min_voltage_below_ocv_curve_max_and_max_voltage_above_ocv_curve_min(
    stem: str, profiles: dict[str, ManufacturerProfile]
) -> None:
    """Regression guard specific to this track: the school bus profile's OCV
    curve had to be rescaled by hand to this pack's real 583-689V range
    (see the YAML's own note) rather than reused verbatim from the shared
    passenger-EV placeholder curve (300-395V), which would have placed the
    pack's own documented min_terminal_voltage_v (583V) above the OCV
    curve's max — an internal contradiction within the same profile. This
    asserts that contradiction can't silently recur, for this profile or
    any future commercial profile with a different voltage domain.
    """
    battery = profiles[stem].battery
    ocv_values = [point[1].value for point in battery.ocv_curve_v]
    assert battery.min_terminal_voltage_v.value <= max(ocv_values), (
        f"{stem}: min_terminal_voltage_v ({battery.min_terminal_voltage_v.value}) "
        f"exceeds every point on ocv_curve_v ({ocv_values}) — pack could never reach its own minimum voltage"
    )
    assert battery.max_terminal_voltage_v.value >= min(ocv_values), (
        f"{stem}: max_terminal_voltage_v ({battery.max_terminal_voltage_v.value}) "
        f"is below every point on ocv_curve_v ({ocv_values})"
    )


@pytest.mark.parametrize("stem", PROFILE_STEMS)
def test_profile_runs_60s_default_scenario_without_raising(stem: str, profiles: dict[str, ManufacturerProfile]) -> None:
    profile = profiles[stem]
    scenario = _make_scenario(profile)

    result = run_scenario(profile, scenario, duration_s=60.0, target_speed_mps=lambda _t: 15.0)

    for value in [
        result.final_state.speed_mps,
        result.final_state.battery_soc,
        *result.final_state.node_temps_k.values(),
        *result.time_series.commanded_torque_nm,
    ]:
        assert math.isfinite(value)
