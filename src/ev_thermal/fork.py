"""Fork-based what-if branching: creates a `ForkPoint` from an existing `SimState`,
clones state (including RNG state) without touching the parent run, and continues
simulation under modified parameters.

## v1 scope: full-fidelity (Tier-1-style) forking only

This module lets a caller branch a running `sim.run_scenario` from any point
in its history: capture a `ForkPoint`, apply its `overrides` to build a new
child `Scenario`, and continue via `sim.run_scenario(initial_state=...)` --
the resume primitive that already existed and is already tested
(`test_fork_replay_from_saved_state_is_identical`). Tier-2/long-horizon
(`long_horizon.simulate_long_horizon`) resume is a separate, deferred
milestone: `LongHorizonCheckpoint` doesn't carry `rng_state` or enough
schedule-position information to resume from, and extending it is a
materially bigger change than this module makes.

## `overrides` path shape (see docs/DESIGN_QUESTIONS.md Q-0018)

A `ForkPoint.overrides` key is a dotted path of exactly 2 or 3 segments:

    <top_level>.<leaf>
    <top_level>.<sub_object>.<leaf>

`top_level` is one of `"profile"` (-> `Scenario.manufacturer_profile`),
`"climate"`, `"usage_profile"`, or `"charging_profile"` -- Scenario's own
four top-level fields, `"profile"` used as a shorter alias for the
schema's actual `manufacturer_profile` attribute name. At most one further
hop into a single nested `BaseModel` field is allowed (e.g. `profile.
battery`); the final segment must resolve to a LEAF value -- a `Sourced[T]`
(its `.value` gets patched, confidence/source untouched) or a plain scalar
field -- never a list, dict, or another nested model. No arbitrary-depth
walking, no list/dict indexing: `"profile.battery.capacity_nominal_ah"`
is valid, `"profile.thermal_architecture.cooling_loops[0].serves"` is not
(it would need list indexing AND a fourth segment). This bound keeps the
validation surface to four known top-level schemas, at the cost of not
being able to express a deeper counterfactual -- if a real use case needs
one, that's a signal to revisit this design, not to loosen the resolver
ad hoc.

Note: `Scenario.overrides` (`schema.py`) is a separate, pre-existing,
still-unused field with narrower documented scope ("on top of
manufacturer_profile" only, per its own field description) -- not reused
or wired up here. This module's resolver operates directly on a `Scenario`
and returns a new one; `Scenario.overrides` is untouched, flagged in
docs/MILESTONES.md as a follow-up rather than unified with this mechanism in this
milestone.

## `state_id`/`scenario_id` divergence after a fork

`SimState.state_id` is `f"{scenario_id}-step-{step_count:06d}"`, and
`sim.py` always carries `state.scenario_id` forward unchanged across steps
(never re-derived from whatever `Scenario` a later `run_scenario` call
happens to receive). Left alone, a forked branch and its parent's own
un-forked continuation would produce IDENTICAL `state_id`s at the same
`step_count` -- a real collision, not just a cosmetic issue, since
`state_id`/`fork_lineage` are how a caller reconstructs which branch a
given state belongs to. `branch()` below namespaces this away by giving
the child a new `scenario_id` (`f"{parent_scenario_id}-fork-{fork_id}"`)
and rewriting the returned state's own `state_id` to match at the same
`step_count` -- so every subsequent step's `state_id` (computed the normal
way sim.py already does) is automatically unique to that branch, including
across a fork-of-a-fork chain.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from ev_thermal.schema import ForkPoint, Scenario, SimState, Sourced

_TOP_LEVEL_ATTR_BY_PATH_PREFIX: dict[str, str] = {
    "profile": "manufacturer_profile",
    "climate": "climate",
    "usage_profile": "usage_profile",
    "charging_profile": "charging_profile",
}


def _apply_leaf_override(obj: BaseModel, leaf_name: str, value: Any, path: str) -> BaseModel:
    if leaf_name not in type(obj).model_fields:
        raise ValueError(f"override path {path!r}: {type(obj).__name__} has no field {leaf_name!r}")
    current = getattr(obj, leaf_name)
    if isinstance(current, Sourced):
        if not isinstance(value, type(current.value)):
            raise TypeError(
                f"override path {path!r}: value {value!r} does not match "
                f"{leaf_name}'s existing type {type(current.value).__name__}"
            )
        return obj.model_copy(update={leaf_name: current.model_copy(update={"value": value})})
    if isinstance(current, (BaseModel, list, dict)):
        raise ValueError(
            f"override path {path!r}: {leaf_name!r} is not a leaf value "
            f"(got {type(current).__name__}) -- see this module's docstring, Q-0018"
        )
    if current is not None and not isinstance(value, type(current)):
        raise TypeError(
            f"override path {path!r}: value {value!r} does not match "
            f"{leaf_name}'s existing type {type(current).__name__}"
        )
    return obj.model_copy(update={leaf_name: value})


def apply_override(scenario: Scenario, path: str, value: Any) -> Scenario:
    """Applies one `path -> value` override to `scenario`, returning a NEW
    `Scenario` -- `scenario` itself is never mutated (pydantic `model_copy`
    always constructs a fresh object). See this module's docstring for the
    exact path shape ratified in Q-0018.
    """
    segments = path.split(".")
    if len(segments) not in (2, 3):
        raise ValueError(
            f"override path {path!r} must have 2 or 3 dotted segments "
            "(<top_level>.<leaf> or <top_level>.<sub_object>.<leaf>) -- see Q-0018"
        )
    top_prefix = segments[0]
    if top_prefix not in _TOP_LEVEL_ATTR_BY_PATH_PREFIX:
        raise ValueError(
            f"override path {path!r} must start with one of {sorted(_TOP_LEVEL_ATTR_BY_PATH_PREFIX)} -- see Q-0018"
        )
    top_attr = _TOP_LEVEL_ATTR_BY_PATH_PREFIX[top_prefix]
    top_obj = getattr(scenario, top_attr)
    if top_obj is None:
        raise ValueError(f"override path {path!r}: scenario.{top_attr} is None")

    if len(segments) == 2:
        new_top_obj = _apply_leaf_override(top_obj, segments[1], value, path)
    else:
        sub_name, leaf_name = segments[1], segments[2]
        if sub_name not in type(top_obj).model_fields:
            raise ValueError(f"override path {path!r}: {type(top_obj).__name__} has no field {sub_name!r}")
        sub_obj = getattr(top_obj, sub_name)
        if not isinstance(sub_obj, BaseModel):
            raise ValueError(
                f"override path {path!r}: {top_prefix}.{sub_name} is not a single nested "
                "object -- overrides go at most one level below a top-level Scenario field, "
                "never into a list or dict (see this module's docstring, Q-0018)"
            )
        new_sub_obj = _apply_leaf_override(sub_obj, leaf_name, value, path)
        new_top_obj = top_obj.model_copy(update={sub_name: new_sub_obj})

    return scenario.model_copy(update={top_attr: new_top_obj})


def apply_overrides(scenario: Scenario, overrides: dict[str, Any]) -> Scenario:
    """Applies every `overrides` entry in turn via `apply_override`."""
    for path, value in overrides.items():
        scenario = apply_override(scenario, path, value)
    return scenario


def create_fork_point(state: SimState, overrides: dict[str, Any], description: str) -> ForkPoint:
    """Builds a `ForkPoint` capturing a what-if branch from `state`.

    `fork_id` is a deterministic content hash (parent_state_id, sim_time_s,
    description, overrides), not `uuid4` -- matching `causal.
    make_causal_event`'s convention for the same reason: fork replay must be
    exactly reproducible, and a process-random ID would break that.
    `created_at` is the one genuinely non-deterministic field, by design
    (per its own field description, "wall-clock time the fork was created"
    -- audit metadata, not part of `fork_id` or any replay-determinism
    chain).
    """
    overrides_json = json.dumps(overrides, sort_keys=True, default=str)
    fork_id = hashlib.sha256(
        f"{state.state_id}|{state.sim_time_s!r}|{description}|{overrides_json}".encode()
    ).hexdigest()[:16]
    return ForkPoint(
        fork_id=fork_id,
        parent_state_id=state.state_id,
        sim_time_s=state.sim_time_s,
        description=description,
        overrides=overrides,
        created_at=datetime.now(UTC),
    )


def branch(parent_scenario: Scenario, parent_state: SimState, fork_point: ForkPoint) -> tuple[Scenario, SimState]:
    """Builds the child `(Scenario, SimState)` pair for continuing a fork.

    Applies `fork_point.overrides` to `parent_scenario` (a new `Scenario`;
    `parent_scenario` is untouched) and returns a state equal to
    `parent_state` except `fork_lineage` has `fork_point.fork_id` appended,
    and `scenario_id`/`state_id` are rewritten to a fork-namespaced value
    (see this module's docstring, "state_id/scenario_id divergence after a
    fork") -- `parent_state` itself is never mutated. Continue simulating
    via `sim.run_scenario(profile, child_scenario, duration_s,
    initial_state=child_state)`.
    """
    child_scenario_id = f"{parent_state.scenario_id}-fork-{fork_point.fork_id}"
    child_scenario = apply_overrides(parent_scenario, fork_point.overrides).model_copy(
        update={"name": child_scenario_id}
    )
    child_state = parent_state.model_copy(
        update={
            "scenario_id": child_scenario_id,
            "state_id": f"{child_scenario_id}-step-{parent_state.step_count:06d}",
            "fork_lineage": [*parent_state.fork_lineage, fork_point.fork_id],
        }
    )
    return child_scenario, child_state
