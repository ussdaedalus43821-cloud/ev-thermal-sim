"""Behavioral tests for `ev_thermal.refrigerant`'s DX-refrigerant-loop physics
(Milestone D).
"""

from __future__ import annotations

from ev_thermal.refrigerant import step_refrigerant_loop
from ev_thermal.schema import Confidence, RefrigerantLoop, Sourced

_RESISTANCE_K_PER_W = 0.3
_ACTIVATION_TEMP_K = 293.15


def _sourced(value: float) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source="placeholder")


def _make_refrigerant_loop() -> RefrigerantLoop:
    return RefrigerantLoop(
        name="battery_refrigerant_loop",
        refrigerant_type="R134a",
        capacity_w=_sourced(3500.0),
        power_w=_sourced(1140.0),
        activation_temp_k=_sourced(_ACTIVATION_TEMP_K),
        full_duty_temp_k=_sourced(303.15),
        serves=["battery_pack"],
    )


def test_zero_duty_produces_zero_heat_and_power() -> None:
    spec = _make_refrigerant_loop()
    heat_removed_w, power_w = step_refrigerant_loop(
        spec, duty=0.0, component_temp_k=313.15, resistance_to_coolant_k_per_w=_RESISTANCE_K_PER_W
    )

    assert heat_removed_w == 0.0
    assert power_w == 0.0


def test_negative_duty_produces_zero_heat_and_power() -> None:
    """A commanded duty should never go negative in practice (_duty_ramp's
    min_duty=0.0 floor) -- defensive, matching cooling.py's chiller_duty<=0.0 gate.
    """
    spec = _make_refrigerant_loop()
    heat_removed_w, power_w = step_refrigerant_loop(
        spec, duty=-0.1, component_temp_k=313.15, resistance_to_coolant_k_per_w=_RESISTANCE_K_PER_W
    )

    assert heat_removed_w == 0.0
    assert power_w == 0.0


def test_component_at_or_below_activation_temp_produces_zero_heat_removed() -> None:
    """A real compressor doesn't pull heat back INTO a component that's
    already at or below its target -- the resistance-based cap floors at
    zero via max(0.0, ...), same reasoning as cooling.py's chiller not
    running in reverse.
    """
    spec = _make_refrigerant_loop()
    heat_removed_w, power_w = step_refrigerant_loop(
        spec, duty=1.0, component_temp_k=_ACTIVATION_TEMP_K, resistance_to_coolant_k_per_w=_RESISTANCE_K_PER_W
    )

    assert heat_removed_w == 0.0
    assert power_w == spec.power_w.value  # compressor still runs at commanded duty regardless


def test_resistance_caps_heat_removed_below_nominal_capacity() -> None:
    """Milestone D's own validation finding: for any realistic
    resistance_to_coolant_k_per_w, the resistance -- not the compressor's
    nominal rated capacity -- is the binding constraint. A modest 20K
    temperature differential over 0.3 K/W implies only ~66.7W transferable,
    far below this loop's 3500W nominal capacity.
    """
    spec = _make_refrigerant_loop()
    component_temp_k = _ACTIVATION_TEMP_K + 20.0
    heat_removed_w, power_w = step_refrigerant_loop(
        spec, duty=1.0, component_temp_k=component_temp_k, resistance_to_coolant_k_per_w=_RESISTANCE_K_PER_W
    )

    expected_resistance_limited_w = 20.0 / _RESISTANCE_K_PER_W
    assert heat_removed_w == expected_resistance_limited_w
    assert heat_removed_w < spec.capacity_w.value
    assert power_w == spec.power_w.value


def test_capacity_share_caps_heat_removed_when_resistance_would_allow_more() -> None:
    """The opposite direction: an artificially tiny resistance (implying a
    huge transferable rate) must still be capped by capacity_share_w*duty --
    the compressor's own rated ceiling is still real and finite.
    """
    spec = _make_refrigerant_loop()
    heat_removed_w, power_w = step_refrigerant_loop(
        spec,
        duty=1.0,
        component_temp_k=_ACTIVATION_TEMP_K + 20.0,
        resistance_to_coolant_k_per_w=0.001,  # would imply 20000W transferable, far above capacity
    )

    assert heat_removed_w == spec.capacity_w.value
    assert power_w == spec.power_w.value


def test_capacity_share_w_overrides_spec_capacity_for_multi_component_loops() -> None:
    spec = _make_refrigerant_loop()
    heat_removed_w, _power_w = step_refrigerant_loop(
        spec,
        duty=1.0,
        component_temp_k=_ACTIVATION_TEMP_K + 20.0,
        resistance_to_coolant_k_per_w=0.001,  # tiny, so capacity_share_w is the binding cap
        capacity_share_w=spec.capacity_w.value / 2.0,
    )

    assert heat_removed_w == spec.capacity_w.value / 2.0


def test_duty_scales_power_linearly() -> None:
    spec = _make_refrigerant_loop()
    _heat_half_w, half_power_w = step_refrigerant_loop(
        spec, duty=0.5, component_temp_k=313.15, resistance_to_coolant_k_per_w=_RESISTANCE_K_PER_W
    )
    _heat_full_w, full_power_w = step_refrigerant_loop(
        spec, duty=1.0, component_temp_k=313.15, resistance_to_coolant_k_per_w=_RESISTANCE_K_PER_W
    )

    assert half_power_w == full_power_w / 2.0
