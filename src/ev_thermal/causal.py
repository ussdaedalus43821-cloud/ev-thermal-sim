"""Causal trace assembly: builds an append-only log of `CausalEvent`s during a sim run
and reconstructs the causal chain (via `caused_by`) explaining why a given event
happened.

Only `make_causal_event` exists so far: a stateless constructor other physics
modules (motor.py, inverter.py, ...) call to log an event consistently. The
append-only log and causal-chain reconstruction described above belong to
sim.py, once a sim loop exists to drive them.

`event_id` is a deterministic hash of the event's own content, not a random
uuid4: sim.py's whole premise is exact fork-replay reproducibility (same
seed + same inputs -> byte-identical output, including the full `SimState`,
whose `event_ids` list embeds these), which a process-random ID would break
on every single run. A collision would need two events with identical
sim_time_s, event_type, affected_component, and description — for
descriptions that embed a specific numeric reading (as every current caller's
do), that's not a realistic concern.
"""

from __future__ import annotations

import hashlib

from ev_thermal.schema import CausalEvent


def make_causal_event(
    sim_time_s: float,
    event_type: str,
    description: str,
    affected_component: str,
    severity: str,
    caused_by: list[str] | None = None,
) -> CausalEvent:
    event_id = hashlib.sha256(f"{sim_time_s!r}|{event_type}|{affected_component}|{description}".encode()).hexdigest()[
        :16
    ]
    return CausalEvent(
        event_id=event_id,
        sim_time_s=sim_time_s,
        event_type=event_type,
        description=description,
        caused_by=caused_by or [],
        affected_component=affected_component,
        severity=severity,
    )
