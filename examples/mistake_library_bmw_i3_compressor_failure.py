"""Reproducible runner for this project's third mistake-library entry:
"DX refrigerant compressor internal failure ('Black Death') leaves the
battery with no active cooling (BMW i3)."

Per docs/DESIGN_QUESTIONS.md Q-0025, both `failure_profile` and
`counterfactual_profile` are the SAME real vehicle (`configs/profiles/
bmw_i3.yaml`) -- unlike the Leaf entry (real vehicle vs. a hypothetical
fix) or the e-tron entry (a hypothetical mistake vs. the real vehicle),
this is the first mistake-library entry where BOTH sides are grounded in
the same real profile, differing only in whether the DX compressor has
internally failed. `counterfactual_profile` is the real, unmodified i3
(working compressor). `failure_profile` removes `thermal_architecture.
refrigerant_loops` entirely -- the real documented consequence of a "Black
Death" compressor failure ("entire battery cooling system can't be
flushed, requiring full pack + cooling system replacement",
docs/research/profiles_v1.md) is total, not partial, loss of the cooling
path, so an empty list is the correct mapping, not a reduced capacity_w.

This is a genuinely different lesson category from the Leaf entry (a
manufacturer's DESIGN decision to omit active cooling) and the e-tron
entry (an integration mistake in a hypothetical combined-loop design):
this is a RELIABILITY risk in an otherwise well-designed, working system
-- the i3's real DX cooling is validated (docs/MILESTONES.md's Milestone D
and its multi-climate follow-up) to work correctly when it works; this
entry is about what happens when a real, documented single point of
failure in that same system fails. Only possible to publish now that
Milestone D's i3-vs-Leaf-comparisons caveat is lifted and the mechanism
itself is validated -- an i3 comparison built before that would have
carried the same "not yet validated" caveat as every other i3 number.

Uses Phoenix, AZ's real climate (already sourced and i3-validated for
Milestone D's multi-climate follow-up, examples/i3_multiclimate_
validation.py) -- the real consequence of losing active cooling is a
climate-dependent one, and Phoenix's real, documented heat is what makes
it vivid, same reasoning the Leaf entry's Guam climate choice used.

Usage (run as a module, same reason as the other examples/ scripts that
cross-import examples.flagship_comparison):
    uv run python -m examples.mistake_library_bmw_i3_compressor_failure
    uv run python -m examples.mistake_library_bmw_i3_compressor_failure --duration-years 20
"""

from __future__ import annotations

import argparse

from ev_thermal.mistake import run_mistake_case
from ev_thermal.profile_loader import load_profile
from ev_thermal.schema import Confidence, DTPolicy, ManufacturerProfile, MistakeCase, Scenario
from examples.chiller_hot_climate_investigation import phoenix_climate
from examples.flagship_comparison import default_charging_profile, default_day_type_mix, default_usage_profile

DEFAULT_PROFILE = "configs/profiles/bmw_i3.yaml"
DEFAULT_DURATION_YEARS = 5
DEFAULT_SEED = 42

_BLACK_DEATH_SOURCE = (
    "docs/research/profiles_v1.md — BMW i3: \"DX cooling elegant... but fragile -- if A/C compressor fails "
    "internally ('Black Death'), entire battery cooling system can't be flushed, requiring full pack + "
    "cooling system replacement\"; corroborated by "
    "https://www.theautopian.com/how-the-black-death-can-destroy-a-bmw-i3-instantly-and-with-no-warning/ "
    "(real, documented BMW i3/i3s A/C compressor internal-failure reports) -- counterfactual failure state "
    "for teaching comparison, not a claim the real profile's own compressor is failed"
)


def build_i3_with_failed_compressor() -> ManufacturerProfile:
    i3 = load_profile(DEFAULT_PROFILE)

    no_refrigerant_architecture = i3.thermal_architecture.model_copy(update={"refrigerant_loops": []})
    assert i3.battery.cooling_mechanism is not None  # real i3 profile always populates this
    new_battery = i3.battery.model_copy(
        update={
            "cooling_mechanism": i3.battery.cooling_mechanism.model_copy(
                update={
                    "value": (
                        "DX refrigerant compressor internally failed ('Black Death') -- refrigerant can no "
                        "longer circulate, battery has no active cooling path at all (counterfactual failure "
                        "state for teaching comparison, not a claim about the real profile's own compressor)"
                    ),
                    "confidence": Confidence.LOW,
                    "source": _BLACK_DEATH_SOURCE,
                }
            ),
        }
    )
    return i3.model_copy(
        update={
            "name": "BMW i3 (failure state: DX compressor internally failed, 'Black Death')",
            "thermal_architecture": no_refrigerant_architecture,
            "battery": new_battery,
        }
    )


def build_case() -> MistakeCase:
    failure_profile = build_i3_with_failed_compressor()
    counterfactual_profile = load_profile(DEFAULT_PROFILE)
    dt_policy = DTPolicy(min_dt_s=1.0, max_dt_s=60.0, max_temp_change_per_step_k=0.5, method="adaptive-euler")
    scenario = Scenario(
        name="mistake-library-bmw-i3-compressor-failure",
        manufacturer_profile=None,
        climate=phoenix_climate(),
        usage_profile=default_usage_profile(),
        charging_profile=default_charging_profile(),
        duration_years=DEFAULT_DURATION_YEARS,
        dt_policy=dt_policy,
        random_seed=DEFAULT_SEED,
    )
    return MistakeCase(
        name="DX refrigerant compressor internal failure ('Black Death') leaves the battery with no active "
        "cooling (BMW i3)",
        failure_mode=(
            "The DX refrigerant compressor fails internally -- a real, documented BMW i3/i3s failure mode "
            "('Black Death') -- and refrigerant can no longer circulate through the battery's microchannel "
            "extrusions. Unlike a glycol loop, which can be flushed and repaired, this is total, unrecoverable "
            "loss of the battery's only active cooling path, often with no warning, requiring full pack + "
            "cooling system replacement to fix -- but the vehicle keeps being driven in the meantime, aging "
            "the battery under passive-only cooling exactly as if it had never had active cooling at all."
        ),
        failure_profile=failure_profile,
        counterfactual_profile=counterfactual_profile,
        scenario=scenario,
        evidence=[
            _BLACK_DEATH_SOURCE,
            "docs/MILESTONES.md's Milestone D and multi-climate-validation entries -- confirms the real, "
            "working DX refrigerant mechanism (this case's counterfactual) is itself validated, not "
            "approximated, so the delta measured here is attributable to the compressor failure specifically, "
            "not to any remaining uncertainty in how the working mechanism is modeled.",
        ],
        lesson=(
            "A single point of failure in an otherwise elegant, working active-cooling system is a real risk "
            "category of its own, distinct from a manufacturer never building active cooling in the first "
            "place -- the DX refrigerant compressor is the ONE component this vehicle's entire battery-cooling "
            "strategy depends on, and its failure mode (unlike a multi-component glycol loop, which can "
            "degrade more gracefully or be serviced) is often total, sudden, and expensive to fix precisely "
            "because the design that made it elegant (no glycol, no extra pumps) is the same design that "
            "makes it unrepairable when it breaks."
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
