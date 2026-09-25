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
from ev_thermal.sim import advance_one_step, coolant_node_name, initialize_sim_state, run_scenario

PROFILE_STEMS = ["nissan_leaf", "tesla_model3", "chevrolet_bolt", "audi_etron", "bmw_i3", "kia_niro_ev"]


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="test-fixture")


def _make_scenario(profile: ManufacturerProfile, seed: int = 42, ambient_temp_k: float = 298.15) -> Scenario:
    """A minimal, deliberately-generic Scenario wrapper around a loaded profile —
    exercises run_scenario end to end, not the profile's own researched climate/
    usage assumptions (this profile milestone doesn't populate those).
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
def test_every_cooling_loop_serves_entry_matches_a_real_thermal_node_name(
    stem: str, profiles: dict[str, ManufacturerProfile]
) -> None:
    """Regression guard for the battery/battery_pack naming mismatch found
    during the topology-wiring milestone's planning: every profile's
    CoolingLoop.serves listed "battery" while the actual battery
    ThermalNode.name is "battery_pack", which would have silently made
    every profile's battery thermally isolated once CoolingLoop.serves
    became authoritative -- including the four profiles whose batteries
    are genuinely actively liquid-cooled. This asserts the two vocabularies
    can never drift apart again undetected.
    """
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


def test_all_six_profiles_produce_different_peak_temps_under_identical_scenario(
    profiles: dict[str, ManufacturerProfile],
) -> None:
    """Regression guard for the spec-integration milestone: if every profile
    produced the same trace under the same scenario, that would mean profile
    data isn't actually threading through to the physics layer (e.g. sim.py
    silently falling back to some shared default) rather than being read from
    each profile's own fields.
    """
    peak_motor_temps_k: dict[str, float] = {}
    peak_battery_temps_k: dict[str, float] = {}
    for stem, profile in profiles.items():
        scenario = _make_scenario(profile)
        result = run_scenario(profile, scenario, duration_s=60.0, target_speed_mps=lambda _t: 15.0)
        motor_name = profile.motors[0].thermal_node.name
        battery_name = profile.battery.thermal_node.name
        peak_motor_temps_k[stem] = max(result.time_series.node_temps_k[motor_name])
        peak_battery_temps_k[stem] = max(result.time_series.node_temps_k[battery_name])

    assert len(set(peak_motor_temps_k.values())) > 1, (
        f"all six profiles produced identical peak motor temps: {peak_motor_temps_k}"
    )
    assert len(set(peak_battery_temps_k.values())) > 1, (
        f"all six profiles produced identical peak battery temps: {peak_battery_temps_k}"
    )


def test_two_loop_profile_commands_genuinely_different_duty_per_loop(
    profiles: dict[str, ManufacturerProfile],
) -> None:
    """The actual point of the per-loop-duty milestone: on a real two-loop
    profile (Model 3 -- battery_loop serves only the battery, motor_oil_loop
    serves only motor+inverter), a hot battery with a cool motor/inverter
    must command a high duty on the battery loop while the motor loop stays
    at its idle minimum -- something the prior milestone's single-broadcast-
    duty compromise could not produce (it would have raised both loops'
    duty together, since only the max across ALL components mattered then).
    """
    profile = profiles["tesla_model3"]
    scenario = _make_scenario(profile)
    battery_name = profile.battery.thermal_node.name
    battery_loop, motor_loop = profile.thermal_architecture.cooling_loops
    assert battery_loop.serves == [battery_name]  # sanity: this test's premise depends on the real topology

    state = initialize_sim_state(profile, scenario, scenario.random_seed)
    hot_battery_temps_k = {
        **state.node_temps_k,
        battery_name: battery_loop.thermostat_temp_k.value + 10.0,
        # the battery loop's own coolant is hot too (a hot pack would heat its
        # own loop's coolant in reality) -- the motor loop's coolant stays at
        # ambient, isolating this to a per-loop effect, not a global one
        coolant_node_name(battery_loop.name): battery_loop.thermostat_temp_k.value + 10.0,
    }
    state = state.model_copy(update={"node_temps_k": hot_battery_temps_k})

    record = advance_one_step(profile, scenario, state, target_speed_mps=20.0)

    assert record.pump_duty[battery_loop.name] > record.pump_duty[motor_loop.name]
    assert record.fan_duty[battery_loop.name] > record.fan_duty[motor_loop.name]
    # the motor loop should be at (or near) its idle floor, not dragged up by the hot battery
    assert record.pump_duty[motor_loop.name] < 0.3


def test_leaf_battery_soaks_toward_ambient_over_hours_not_seconds(
    profiles: dict[str, ManufacturerProfile],
) -> None:
    """Ambient-coupling milestone regression guard: with no torque demand (a
    parked, no-load soak) and a cold ambient, the Leaf's passively-air-cooled
    battery (resistance_to_ambient_k_per_w targets a multi-hour time constant,
    see nissan_leaf.yaml) should drift toward ambient gradually -- 'a pack
    soaks over a day in the sun,' not a temperature swing per simulation step.
    This exercises the full run_scenario stack, not thermal.py's isolated-node
    unit tests (see test_node_level_ambient_resistance_cools_with_time_
    constant_tau_equals_rc in test_thermal.py for the analytical version).
    Since the topology-wiring milestone (CoolingLoop.serves is now
    authoritative — the Leaf's powertrain_loop correctly excludes the
    battery), the battery's only path here really is the ambient one, so
    this is now close to a pure RC decay, not just order-of-magnitude —
    though still not asserted to the same 5% tolerance as the isolated-node
    unit test, since aux-load battery current draw is a small but real
    second heat source this integration-level test doesn't null out.
    """
    profile = profiles["nissan_leaf"]
    battery_name = profile.battery.thermal_node.name
    initial_temp_k = profile.battery.thermal_node.initial_temp_k
    cold_ambient_k = 273.15  # 0C, well below the 25C initial battery temp

    scenario = _make_scenario(profile, ambient_temp_k=cold_ambient_k)
    result = run_scenario(profile, scenario, duration_s=86_400.0, target_speed_mps=lambda _t: 0.0)

    battery_temps_k = result.time_series.node_temps_k[battery_name]
    battery_times_s = result.time_series.time_s

    one_hour_temp_k = next(temp for t, temp in zip(battery_times_s, battery_temps_k, strict=True) if t >= 3600.0)
    one_hour_drop_k = initial_temp_k - one_hour_temp_k
    assert 0.0 < one_hour_drop_k < 5.0, f"expected a gentle first-hour drift, got {one_hour_drop_k:.2f}K"

    final_temp_k = battery_temps_k[-1]
    total_drop_k = initial_temp_k - final_temp_k
    assert total_drop_k > 5.0, f"expected meaningful drift toward ambient over 24h, got {total_drop_k:.2f}K"
    assert final_temp_k > cold_ambient_k, "should not overshoot past ambient"

    # monotonically cooling toward ambient, no oscillation
    assert all(later <= earlier for earlier, later in zip(battery_temps_k, battery_temps_k[1:], strict=False))
