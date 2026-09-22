"""Validation tests for the six populated reference-vehicle profiles
(configs/profiles/*.yaml). Data-integrity checks on the profiles
themselves, not on ev_thermal.profile_loader's parsing mechanism (see
test_profile_loader.py for that) or on any single vehicle's specific
researched values (see docs/research/profiles_v1.md and
docs/model_reference.md for that).
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel

from ev_thermal.profile_loader import PROFILES_DIR, load_all_profiles
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

PROFILE_STEMS = ["nissan_leaf", "tesla_model3", "chevrolet_bolt", "audi_etron", "bmw_i3", "kia_niro_ev"]


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="test-fixture")


def _make_scenario(profile: ManufacturerProfile, seed: int = 42) -> Scenario:
    """A minimal, deliberately-generic Scenario wrapper around a loaded profile —
    exercises run_scenario end to end, not the profile's own researched climate/
    usage assumptions (this profile milestone doesn't populate those).
    """
    climate = Climate(
        location_name="Test City",
        mean_annual_temp_k=_sourced(298.15),
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
    """Recursively walks a ManufacturerProfile (or any nested piece of it) and
    yields every Sourced instance found, across BaseModels, lists, tuples, and
    dicts. Sourced.value is never itself Sourced-wrapped in this schema, so
    this doesn't recurse into it.
    """
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
    return load_all_profiles()


def test_all_six_profile_files_exist() -> None:
    stems = {path.stem for path in PROFILES_DIR.glob("*.yaml")}
    assert stems == set(PROFILE_STEMS)


def test_all_six_yamls_load_via_the_profile_loader(profiles: dict[str, ManufacturerProfile]) -> None:
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
