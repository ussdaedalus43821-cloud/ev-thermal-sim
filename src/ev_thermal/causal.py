"""Causal trace assembly: builds an append-only log of `CausalEvent`s during a sim run
and reconstructs the causal chain (via `caused_by`) explaining why a given event
happened.

Only `make_causal_event` exists so far: a stateless constructor other physics
modules (motor.py, inverter.py, ...) call to log an event consistently. The
append-only log and causal-chain reconstruction described above belong to
sim.py, once a sim loop exists to drive them.
"""

from __future__ import annotations

import uuid

from ev_thermal.schema import CausalEvent


def make_causal_event(
    sim_time_s: float,
    event_type: str,
    description: str,
    affected_component: str,
    severity: str,
    caused_by: list[str] | None = None,
) -> CausalEvent:
    return CausalEvent(
        event_id=str(uuid.uuid4()),
        sim_time_s=sim_time_s,
        event_type=event_type,
        description=description,
        caused_by=caused_by or [],
        affected_component=affected_component,
        severity=severity,
    )
