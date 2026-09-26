"""Behavioral tests for `ev_thermal.cooling`'s effectiveness-NTU radiator model.

Fixture coefficients (radiator UA, frontal area, fan max air velocity) are
physically-plausible placeholders, not sourced hardware curves — see the
TODO(spec-sourcing) markers on the corresponding `CoolingLoop` fields in
schema.py.
"""

from __future__ import annotations

from scipy.optimize import brentq

from ev_thermal.cooling import step_cooling
from ev_thermal.schema import Confidence, CoolingLoop, Sourced

_AMBIENT_TEMP_K = 298.15
_HOT_COOLANT_TEMP_K = 333.15


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="placeholder")


def _make_cooling_loop() -> CoolingLoop:
    """A single shared loop serving all three components — matches the topology
    tests/test_sim.py's _make_profile() (which reuses this fixture) assumes.
    """
    return CoolingLoop(
        name="shared_loop",
        coolant_type="50/50 water-glycol",
        loop_volume_l=_sourced(6.0),
        pump_max_flow_l_per_min=_sourced(20.0),
        pump_power_w=_sourced(80.0),
        radiator_ua_w_per_k=_sourced(400.0),
        fan_power_w=_sourced(150.0),
        chiller_present=False,
        serves=["battery_pack", "motor", "inverter"],
        coolant_specific_heat_j_per_kg_k=_sourced(3600.0),
        coolant_density_kg_per_m3=_sourced(1050.0),
        thermostat_temp_k=_sourced(308.15),
        thermostat_full_open_temp_k=_sourced(313.15),
        radiator_frontal_area_m2=_sourced(0.25),
        fan_max_air_velocity_mps=_sourced(6.0),
    )


def _make_chiller_loop() -> CoolingLoop:
    return _make_cooling_loop().model_copy(
        update={
            "chiller_present": True,
            "chiller_capacity_w": _sourced(5000.0),
            "chiller_power_w": _sourced(2000.0),
        }
    )


def _step(
    spec: CoolingLoop,
    coolant_inlet_temp_k: float = _HOT_COOLANT_TEMP_K,
    ambient_temp_k: float = _AMBIENT_TEMP_K,
    vehicle_speed_mps: float = 0.0,
    pump_duty: float = 1.0,
    fan_duty: float = 1.0,
    chiller_duty: float = 0.0,
):
    return step_cooling(
        spec, coolant_inlet_temp_k, ambient_temp_k, vehicle_speed_mps, pump_duty, fan_duty, chiller_duty
    )


def test_zero_pump_duty_means_no_flow_no_rejection_no_pump_power() -> None:
    spec = _make_cooling_loop()
    result = _step(spec, pump_duty=0.0)

    assert result.coolant_mass_flow_kg_per_s == 0.0
    assert result.heat_rejected_w == 0.0
    assert result.pump_power_w == 0.0
    assert result.coolant_outlet_temp_k == _HOT_COOLANT_TEMP_K


def test_full_pump_hot_coolant_cold_ambient_rejects_heat_and_cools() -> None:
    spec = _make_cooling_loop()
    result = _step(spec, pump_duty=1.0, fan_duty=1.0)

    assert result.heat_rejected_w > 0.0
    assert result.coolant_outlet_temp_k < _HOT_COOLANT_TEMP_K


def test_higher_pump_duty_increases_rejection_with_diminishing_returns() -> None:
    spec = _make_cooling_loop()
    duties = [0.1, 0.3, 0.5, 0.7, 0.9, 1.0]
    rejections = [_step(spec, pump_duty=d).heat_rejected_w for d in duties]
    deltas = [b - a for a, b in zip(rejections, rejections[1:], strict=False)]

    assert all(d > 0.0 for d in deltas)
    assert all(deltas[i] > deltas[i + 1] for i in range(len(deltas) - 1))


def test_fan_duty_raises_rejection_when_stationary() -> None:
    spec = _make_cooling_loop()
    no_fan = _step(spec, vehicle_speed_mps=0.0, fan_duty=0.0)
    with_fan = _step(spec, vehicle_speed_mps=0.0, fan_duty=1.0)

    assert with_fan.heat_rejected_w > no_fan.heat_rejected_w


def test_fan_duty_has_less_effect_at_highway_speed() -> None:
    spec = _make_cooling_loop()
    highway_speed_mps = 30.0

    stationary_delta = _step(spec, vehicle_speed_mps=0.0, fan_duty=1.0).heat_rejected_w - _step(
        spec, vehicle_speed_mps=0.0, fan_duty=0.0
    ).heat_rejected_w
    highway_delta = _step(spec, vehicle_speed_mps=highway_speed_mps, fan_duty=1.0).heat_rejected_w - _step(
        spec, vehicle_speed_mps=highway_speed_mps, fan_duty=0.0
    ).heat_rejected_w

    assert highway_delta < stationary_delta


def test_parasitic_power_scales_monotonically_with_duty() -> None:
    spec = _make_cooling_loop()
    duties = [0.0, 0.25, 0.5, 0.75, 1.0]
    pump_powers = [_step(spec, pump_duty=d, fan_duty=0.0).pump_power_w for d in duties]
    fan_powers = [_step(spec, pump_duty=0.0, fan_duty=d).fan_power_w for d in duties]

    assert all(a < b for a, b in zip(pump_powers, pump_powers[1:], strict=False))
    assert all(a < b for a, b in zip(fan_powers, fan_powers[1:], strict=False))


def test_thermostat_closed_means_no_rejection_and_recirculation() -> None:
    spec = _make_cooling_loop()
    closed_temp_k = spec.thermostat_temp_k.value - 5.0
    result = _step(spec, coolant_inlet_temp_k=closed_temp_k)

    assert result.thermostat_open_fraction == 0.0
    assert result.heat_rejected_w == 0.0
    assert result.coolant_outlet_temp_k == closed_temp_k
    assert result.coolant_mass_flow_kg_per_s > 0.0


def test_energy_balance_at_steady_state() -> None:
    spec = _make_cooling_loop()
    heat_load_w = 5000.0
    pump_duty, fan_duty, vehicle_speed_mps = 1.0, 1.0, 0.0

    def residual(coolant_inlet_temp_k: float) -> float:
        result = _step(
            spec,
            coolant_inlet_temp_k=coolant_inlet_temp_k,
            vehicle_speed_mps=vehicle_speed_mps,
            pump_duty=pump_duty,
            fan_duty=fan_duty,
        )
        return result.heat_rejected_w - heat_load_w

    steady_state_inlet_temp_k = brentq(residual, _AMBIENT_TEMP_K + 0.01, _AMBIENT_TEMP_K + 200.0)
    result = _step(
        spec,
        coolant_inlet_temp_k=steady_state_inlet_temp_k,
        vehicle_speed_mps=vehicle_speed_mps,
        pump_duty=pump_duty,
        fan_duty=fan_duty,
    )

    assert abs(result.heat_rejected_w - heat_load_w) / heat_load_w < 0.01


# --- Q-0021: chiller ---


def test_chiller_absent_produces_zero_chiller_heat_and_power_regardless_of_duty() -> None:
    spec = _make_cooling_loop()
    assert spec.chiller_present is False
    result = _step(spec, chiller_duty=1.0)

    assert result.chiller_heat_removed_w == 0.0
    assert result.chiller_power_w == 0.0


def test_chiller_zero_duty_produces_zero_chiller_heat_and_power() -> None:
    spec = _make_chiller_loop()
    result = _step(spec, chiller_duty=0.0)

    assert result.chiller_heat_removed_w == 0.0
    assert result.chiller_power_w == 0.0


def test_chiller_removes_heat_at_full_duty_even_when_coolant_at_ambient() -> None:
    """The radiator branch alone rejects ~0W with no temperature differential
    -- proving the chiller term is genuinely independent of the ambient-
    limited radiator, not folded into or bounded by it (see cooling.py's
    module docstring, Q-0021).
    """
    spec = _make_chiller_loop()
    assert spec.chiller_capacity_w is not None
    assert spec.chiller_power_w is not None
    result = _step(spec, coolant_inlet_temp_k=_AMBIENT_TEMP_K, chiller_duty=1.0)

    assert result.heat_rejected_w == 0.0
    assert result.chiller_heat_removed_w == spec.chiller_capacity_w.value
    assert result.chiller_power_w == spec.chiller_power_w.value


def test_chiller_duty_scales_heat_removed_and_power_linearly() -> None:
    spec = _make_chiller_loop()
    half = _step(spec, coolant_inlet_temp_k=_AMBIENT_TEMP_K, chiller_duty=0.5)
    full = _step(spec, coolant_inlet_temp_k=_AMBIENT_TEMP_K, chiller_duty=1.0)

    assert half.chiller_heat_removed_w == full.chiller_heat_removed_w / 2.0
    assert half.chiller_power_w == full.chiller_power_w / 2.0


def test_chiller_produces_nothing_when_coolant_is_not_flowing() -> None:
    """A stopped loop has nothing for the chiller to extract heat from -- same
    gating the radiator branch already uses.
    """
    spec = _make_chiller_loop()
    result = _step(spec, pump_duty=0.0, chiller_duty=1.0)

    assert result.coolant_mass_flow_kg_per_s == 0.0
    assert result.chiller_heat_removed_w == 0.0
    assert result.chiller_power_w == 0.0
