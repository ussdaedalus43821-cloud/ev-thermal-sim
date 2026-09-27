"""Reproducible runner for this project's second mistake-library entry:
"Combining battery and powertrain cooling loops without upsizing for the
added heat load (Audi e-tron)."

Per docs/DESIGN_QUESTIONS.md Q-0023, `counterfactual_profile` is the real,
unmodified `configs/profiles/audi_etron.yaml` -- its actual `battery_loop`
and `powertrain_loop` are genuinely separate, per research ("four
independent circuits... simplified here to two, still separate").
`failure_profile` replaces both with ONE combined loop that reuses
`powertrain_loop`'s exact hydraulic/radiator numbers and thermostat
setpoints verbatim -- modeling a manufacturer that combined the two real
circuits to save cost/parts count but never resized the loop (or
re-tuned its control) for the battery's added heat load. This is the
mirror image of Q-0019's Leaf entry: there, the REAL vehicle was the
failure and a hypothetical was the fix; here the REAL e-tron is the good
design and the hypothetical combined loop is the mistake.

e-tron was picked specifically because both its real loops already use the
same coolant ("50/50 water-glycol") -- a Chevrolet Bolt-based version was
considered and rejected because the real Bolt's two loops use genuinely
different coolant fluids (DEX-COOL vs. 50/50 water-glycol), which would
make "the manufacturer combined them" physically incoherent.

Every component's own resistance_to_coolant_k_per_w/
resistance_to_ambient_k_per_w stays byte-identical between failure_profile
and counterfactual_profile -- only which coolant node(s) each component
connects to differs, an even purer single-variable isolation than the
Leaf entry (which changed a resistance value AND added a loop).

Uses a MILD climate (Seattle-like, reused from flagship_comparison.py),
deliberately not the Leaf entry's hot Guam climate -- this failure mode is
duty-cycle-driven (sustained heavy driving heats the shared loop via the
motor/inverter), not climate-driven, and using the same mild climate
flagship_comparison.py treats as "the benign baseline" makes that point
directly rather than needing a hot climate to show an effect.

Usage (run as a module, same reason as the other examples/ scripts that
cross-import examples.flagship_comparison):
    uv run python -m examples.mistake_library_etron_shared_powertrain_loop
    uv run python -m examples.mistake_library_etron_shared_powertrain_loop --duration-years 10
"""

from __future__ import annotations

import argparse

from ev_thermal.mistake import run_mistake_case
from ev_thermal.profile_loader import load_profile
from ev_thermal.schema import Confidence, DTPolicy, ManufacturerProfile, MistakeCase, Scenario, Sourced
from examples.flagship_comparison import (
    default_charging_profile,
    default_day_type_mix,
    default_usage_profile,
    seattle_climate,
)

DEFAULT_PROFILE = "configs/profiles/audi_etron.yaml"
DEFAULT_DURATION_YEARS = 5
DEFAULT_SEED = 42

_FAILURE_SOURCE = (
    "counterfactual — MistakeCase teaching example (docs/DESIGN_QUESTIONS.md Q-0023), "
    "not real Audi e-tron research; battery_loop and powertrain_loop merged into one "
    "loop reusing powertrain_loop's own real hydraulic/thermostat values verbatim"
)


def _sourced(value: float, source: str = _FAILURE_SOURCE) -> Sourced[float]:
    return Sourced[float](value=value, confidence=Confidence.LOW, source=source)


def build_etron_with_shared_powertrain_loop() -> ManufacturerProfile:
    etron = load_profile(DEFAULT_PROFILE)

    powertrain_loop = next(
        loop for loop in etron.thermal_architecture.cooling_loops if loop.name == "powertrain_loop"
    )
    merged_loop = powertrain_loop.model_copy(
        update={
            "name": "shared_powertrain_battery_loop",
            "serves": ["battery_pack", "motor", "inverter"],
        }
    )
    new_thermal_architecture = etron.thermal_architecture.model_copy(
        update={
            "cooling_loops": [merged_loop],
            "shared_loop": True,
        }
    )
    assert etron.battery.cooling_mechanism is not None  # real e-tron profile always populates this
    new_battery = etron.battery.model_copy(
        update={
            "cooling_mechanism": etron.battery.cooling_mechanism.model_copy(
                update={
                    "value": (
                        "liquid-cooled via a shared coolant loop with the motor and inverter "
                        "(counterfactual: hypothetical combined-loop mistake for teaching comparison, "
                        "not the real Audi e-tron's separate-loop configuration)"
                    ),
                    "confidence": Confidence.LOW,
                    "source": _FAILURE_SOURCE,
                }
            ),
        }
    )
    return etron.model_copy(
        update={
            "name": "Audi e-tron (counterfactual mistake: shared, un-upsized powertrain/battery loop)",
            "thermal_architecture": new_thermal_architecture,
            "battery": new_battery,
        }
    )


def build_case() -> MistakeCase:
    failure_profile = build_etron_with_shared_powertrain_loop()
    counterfactual_profile = load_profile(DEFAULT_PROFILE)
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")
    scenario = Scenario(
        name="mistake-library-etron-shared-powertrain-loop",
        manufacturer_profile=None,
        climate=seattle_climate(),
        usage_profile=default_usage_profile(),
        charging_profile=default_charging_profile(),
        duration_years=DEFAULT_DURATION_YEARS,
        dt_policy=dt_policy,
        random_seed=DEFAULT_SEED,
    )
    return MistakeCase(
        name="Combining battery and powertrain cooling loops without upsizing for the added heat load (Audi e-tron)",
        failure_mode=(
            "Merging the battery's dedicated coolant loop into the existing powertrain loop, without "
            "increasing that loop's radiator/pump/fan capacity or re-tuning its thermostat for the "
            "battery's tighter target, lets the motor and inverter's waste heat raise the shared "
            "coolant's temperature under sustained heavy driving -- and that heat conducts straight "
            "into the battery, which no longer has its own dedicated, tightly-controlled coolant path."
        ),
        failure_profile=failure_profile,
        counterfactual_profile=counterfactual_profile,
        scenario=scenario,
        evidence=[
            "docs/research/profiles_v1.md — Audi e-tron: real design keeps battery and powertrain "
            "cooling genuinely separate ('four independent circuits... simplified here to two, still "
            "separate'), each independently controlled to its own target -- battery 25-35C, tightly "
            "controlled -- rather than sharing one loop's control logic.",
            "docs/research/profiles_v1.md — Kia Niro EV: a real vehicle that DOES share one coolant "
            "loop across battery, motor, inverter, and heat pump, and documents this exact risk "
            "category directly ('failure mode is integration, not hardware... shared coolant loop "
            "sometimes prioritized cabin comfort over pack conditioning') -- corroborating that "
            "shared-loop integration mistakes are a real, documented failure category, not a "
            "hypothetical concern invented for this comparison. NOT a claim that this hypothetical "
            "reproduces the real Niro's specific numbers: the real Niro's actual degradation is "
            "best-in-class (97.25% SoH at 100,000km), evidence that a shared loop CAN be sized/tuned "
            "correctly -- this case models what happens when it isn't.",
        ],
        lesson=(
            "Sharing a coolant loop across the battery and powertrain is not inherently a mistake -- "
            "real vehicles do it successfully. The mistake is reusing an existing powertrain-only "
            "loop's capacity and control setpoints unchanged after adding the battery to it: the "
            "combined loop must be sized and controlled for ALL of its heat sources together, not "
            "just the ones it was originally designed around."
        ),
        severity="warning",
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
