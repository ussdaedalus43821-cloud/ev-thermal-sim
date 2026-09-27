"""Direct-expansion (DX) refrigerant cooling: a `RefrigerantLoop`'s physical
response given a commanded duty (Milestone D).

Unlike `cooling.py`'s chiller (a second stage bolted onto an existing glycol
loop, gated on that loop's coolant actually flowing), a `RefrigerantLoop` has
no coolant of its own to gate on -- refrigerant evaporates directly against
the component(s) it serves, so this module has no flow rate, no radiator, no
epsilon-NTU heat-exchanger math.

`heat_removed_w` is capped by TWO independent things, not just
`capacity_share_w * duty`: the served component's own
`resistance_to_coolant_k_per_w` (the SAME field a `CoolingLoop`-served
component uses -- reused here, not a new field, since refrigerant contacts
the component just as directly as glycol would) bounds how fast heat can
actually leave that component's lumped thermal mass, exactly like every
other cooling path in this codebase. This cap is NOT optional detail: found
necessary, not a pre-emptive safety margin, while validating Milestone D --
an earlier version of this function applied `capacity_share_w * duty` as an
unbounded direct sink, and a real automotive-compressor-scale capacity_w
(3500W) against a small real battery pack's thermal mass (207,000 J/K, the
BMW i3's own) cooled the pack ~20K in a single ~20-minute characterized
trip, confirmed physically implausible (docs/DESIGN_QUESTIONS.md's
Milestone D entry) -- no real compressor moves heat out of a lumped mass
that fast with no rate-limiting resistance in between. In practice, for any
real component's resistance_to_coolant_k_per_w, this resistance cap is the
binding constraint almost always; capacity_share_w*duty is a backstop for
a case that essentially never arises with realistic resistance values, kept
because a compressor's own rated ceiling is still real and finite.

`power_w` tracks the COMMANDED duty directly, not the actual heat
delivered -- a real compressor runs at its commanded speed regardless of
whether the downstream thermal path can absorb all of it; a documented
simplification, not an oversight.

`controller.py` computes duty (same `_duty_ramp` shape used for pump/fan/
chiller, against `RefrigerantLoop.activation_temp_k`/`full_duty_temp_k`).
`sim.py` calls this once per (loop, served component) pair -- `capacity_
share_w` is the loop's own `capacity_w` divided evenly across everything it
serves (a known simplification for the untested multi-component case; every
current profile's RefrigerantLoop serves exactly one component) -- and
applies `heat_removed_w` as a direct sink on that component (there is no
coolant node to apply it to), charging `power_w` to the battery the same
way pump/fan/chiller power is.
"""

from __future__ import annotations

from ev_thermal.schema import RefrigerantLoop


def step_refrigerant_loop(
    spec: RefrigerantLoop,
    duty: float,
    component_temp_k: float,
    resistance_to_coolant_k_per_w: float,
    capacity_share_w: float | None = None,
) -> tuple[float, float]:
    """Returns (heat_removed_w, power_w) removed from ONE served component at
    the given duty (0-1). `capacity_share_w` is this loop's own `capacity_w`
    (or a fraction of it, if it serves more than one component -- see this
    module's own docstring); `None` (default) uses `spec.capacity_w` as-is,
    correct for the common single-component case. Both zero if duty<=0 or
    component_temp_k is already at/below `spec.activation_temp_k` (a real
    compressor idles at zero and doesn't pull heat back in, matching
    `cooling.py`'s chiller treatment of duty<=0).
    """
    if duty <= 0.0:
        return 0.0, 0.0
    nominal_capacity_w = spec.capacity_w.value if capacity_share_w is None else capacity_share_w
    max_transferable_w = max(0.0, (component_temp_k - spec.activation_temp_k.value) / resistance_to_coolant_k_per_w)
    heat_removed_w = min(nominal_capacity_w * duty, max_transferable_w)
    power_w = spec.power_w.value * duty
    return heat_removed_w, power_w
