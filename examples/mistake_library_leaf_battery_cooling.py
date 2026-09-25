"""Reproducible runner for this project's first mistake-library entry:
"Nissan Leaf: passive-air battery cooling vs. active liquid cooling."

Per docs/DESIGN_QUESTIONS.md Q-0019, `counterfactual_profile` is the real
`configs/profiles/nissan_leaf.yaml` profile with exactly two changes: one
added `CoolingLoop` serving `["battery_pack"]`, and `battery.thermal_node.
resistance_to_coolant_k_per_w` tightened from 0.5 to 0.02 -- Tesla Model 3's
own real placeholder value for the same field, chosen so the counterfactual's
"actively cooled" claim is grounded in an existing, already-reviewed number
rather than invented fresh. Nothing else changes: capacity, motor, mass,
chemistry all stay byte-identical to the real Leaf, so the resulting delta
is attributable to exactly one design decision, per `MistakeCase`'s own
docstring ("the only variable is the design decision being taught").

Constructed programmatically here, not committed as a seventh YAML file --
this keeps it visibly a hypothetical derived from the real profile, not a
new "reference vehicle" (see CLAUDE.md's framing: "six real manufacturer
drivetrains... plus user-editable counterfactuals").

Reuses `examples/flagship_comparison.py`'s climate/day-type-mix/charging-
profile builders (Guam-like climate, per this case's failure mode: hot
ambient plus repeated fast charging with no active battery cooling).

Usage (run as a module, same reason as the other examples/ scripts that
cross-import `examples.flagship_comparison`):
    uv run python -m examples.mistake_library_leaf_battery_cooling
    uv run python -m examples.mistake_library_leaf_battery_cooling --duration-years 10
"""

from __future__ import annotations

import argparse

from ev_thermal.mistake import run_mistake_case
from ev_thermal.profile_loader import load_profile
from ev_thermal.schema import Confidence, CoolingLoop, DTPolicy, ManufacturerProfile, MistakeCase, Scenario, Sourced
from examples.flagship_comparison import (
    DEFAULT_PROFILE_A,
    default_charging_profile,
    default_day_type_mix,
    default_usage_profile,
    guam_climate,
)

DEFAULT_DURATION_YEARS = 5
DEFAULT_SEED = 42

_COUNTERFACTUAL_SOURCE = (
    "counterfactual — MistakeCase teaching example (docs/DESIGN_QUESTIONS.md Q-0019), "
    "not real Nissan Leaf research; resistance_to_coolant_k_per_w tightened to match "
    "Tesla Model 3's real actively-cooled placeholder value (0.02 K/W)"
)


_PLACEHOLDER_SOURCE = "mistake-library placeholder, matches powertrain_loop's own magnitude"


def _sourced(value: float, source: str = _PLACEHOLDER_SOURCE) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source=source)


def build_leaf_with_active_battery_cooling() -> ManufacturerProfile:
    leaf = load_profile(DEFAULT_PROFILE_A)

    battery_loop = CoolingLoop(
        name="battery_loop",
        coolant_type="50/50 water-glycol",
        loop_volume_l=_sourced(4.0),
        pump_max_flow_l_per_min=_sourced(15.0),
        pump_power_w=_sourced(60.0),
        radiator_ua_w_per_k=_sourced(300.0),
        fan_power_w=_sourced(100.0),
        chiller_present=False,
        chiller_capacity_w=None,
        serves=["battery_pack"],
        coolant_specific_heat_j_per_kg_k=_sourced(3600.0),
        coolant_density_kg_per_m3=_sourced(1050.0),
        thermostat_temp_k=_sourced(308.15),
        thermostat_full_open_temp_k=_sourced(313.15),
        radiator_frontal_area_m2=_sourced(0.15),
        fan_max_air_velocity_mps=_sourced(5.0),
    )
    new_thermal_architecture = leaf.thermal_architecture.model_copy(
        update={
            "cooling_loops": [*leaf.thermal_architecture.cooling_loops, battery_loop],
            "shared_loop": False,
        }
    )
    assert leaf.battery.cooling_mechanism is not None  # real Leaf profile always populates this
    new_battery = leaf.battery.model_copy(
        update={
            "thermal_node": leaf.battery.thermal_node.model_copy(
                update={
                    "resistance_to_coolant_k_per_w": leaf.battery.thermal_node.resistance_to_coolant_k_per_w.model_copy(
                        update={"value": 0.02}
                    )
                }
            ),
            "cooling_mechanism": leaf.battery.cooling_mechanism.model_copy(
                update={
                    "value": (
                        "liquid-cooled (counterfactual: hypothetical active battery cooling "
                        "loop added for teaching comparison, not a real Nissan Leaf configuration)"
                    ),
                    "confidence": Confidence.LOW,
                    "source": _COUNTERFACTUAL_SOURCE,
                }
            ),
        }
    )
    return leaf.model_copy(
        update={
            "name": "Nissan Leaf (counterfactual: active battery cooling)",
            "thermal_architecture": new_thermal_architecture,
            "battery": new_battery,
        }
    )


def build_case() -> MistakeCase:
    failure_profile = load_profile(DEFAULT_PROFILE_A)
    counterfactual_profile = build_leaf_with_active_battery_cooling()
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")
    scenario = Scenario(
        name="mistake-library-leaf-battery-cooling",
        manufacturer_profile=None,
        climate=guam_climate(),
        usage_profile=default_usage_profile(),
        charging_profile=default_charging_profile(),
        duration_years=DEFAULT_DURATION_YEARS,
        dt_policy=dt_policy,
        random_seed=DEFAULT_SEED,
    )
    return MistakeCase(
        name="No active battery cooling in hot climates (Nissan Leaf)",
        failure_mode=(
            "Passive air-only battery cooling lets the pack run hot under sustained "
            "high ambient temperature and repeated DC fast charging, accelerating "
            "calendar and cycle aging with no mechanism to shed the heat."
        ),
        failure_profile=failure_profile,
        counterfactual_profile=counterfactual_profile,
        scenario=scenario,
        evidence=[
            "docs/research/profiles_v1.md — Nissan Leaf: '~4.2% per year in hot "
            "climates; some owners reported ~20% loss after 12 months in hot climates'",
            "docs/research/profiles_v1.md — Chevrolet Bolt/Tesla Model 3/Audi e-tron/"
            "Kia Niro EV: all four actively liquid-cool the battery and report "
            "markedly better real-world SoH retention (~89-97% at 100,000km) than "
            "the Leaf's documented hot-climate degradation, corroborating that the "
            "FIX CATEGORY (active battery cooling) works — not a claim that this "
            "specific hypothetical edit reproduces any one of those vehicles' exact "
            "numbers.",
        ],
        lesson=(
            "Active battery cooling is not a luxury feature — in a hot climate with "
            "frequent fast charging, it is the difference between a pack that ages "
            "gracefully and one that doesn't, and the decision to omit it cannot be "
            "corrected later without physically retrofitting the vehicle."
        ),
        severity="critical",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--duration-years", type=int, default=DEFAULT_DURATION_YEARS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    case = build_case()
    day_type_mix = default_day_type_mix()
    result = run_mistake_case(case, day_type_mix, duration_years=args.duration_years, seed=args.seed)

    print(f"{case.name}")
    print(f"severity={case.severity}  duration_years={args.duration_years}  seed={args.seed}")
    print()
    print(f"failure ({case.failure_profile.name}) final SOH:        {result.final_soh_failure * 100:.4f}%")
    print(
        f"counterfactual ({case.counterfactual_profile.name}) final SOH: {result.final_soh_counterfactual * 100:.4f}%"
    )
    print(f"soh_delta (counterfactual - failure): {result.soh_delta * 100:+.4f} percentage points")


if __name__ == "__main__":
    main()
