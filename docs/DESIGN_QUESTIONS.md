# Design Questions

Ambiguities encountered during implementation that need a design
decision. Claude Code appends here instead of deciding silently.

Format:
## Q-NNNN — Title
- Date:
- Context:
- Options considered:
- Blocking: yes | no

---

## Q-0001 — Where does charge-mode step logic live?

- Date: 2026-09-24
- Context: Milestone E2e needs to simulate a DC fast-charge session, which is
  a fundamentally different step sequence from `advance_one_step`'s driving
  pipeline (no driver torque request, no motor/inverter mechanical output,
  vehicle stationary). `battery.compute_battery_electrical_step` already
  fully supports charging today (branches on the sign of `current_request_a`
  throughout — separate C-rate/voltage limits, cold-derate charge-only, etc.
  — already exercised via regen braking), so the gap is entirely in what
  orchestrates a call to it when there's no driving happening at all.
- Options considered:
  1. A separate `advance_charging_step` function beside `advance_one_step`
     in `sim.py`, with its own shorter pipeline (charge current request →
     `compute_battery_electrical_step` → cooling duty via `step_controller`
     with a zero torque request → combined thermal solve → state advance),
     plus a parallel `characterize_fast_charge_session` beside
     `characterize_trip`. (Claude's recommendation.)
  2. A mode flag threaded through `advance_one_step`, with no-op branches
     for driver/motor/inverter/vehicle-dynamics when charging.
  3. A branch inside `battery.step_battery` itself.
- Blocking: yes

## Q-0002 — Charge power profile / taper shape

- Date: 2026-09-24
- Context: Real DC fast charging tapers as SOC rises (CC then CV-like
  behavior). Manufacturer taper curves are proprietary; need a defensible
  placeholder shape.
- Options considered:
  1. Request constant power (derived from charger-rated power) and let the
     already-existing voltage-limit expression in
     `compute_battery_electrical_step` (`(max_terminal_voltage_v - ocv_v +
     v_rc_v) / r0_ohm`, which shrinks as OCV rises toward
     `max_terminal_voltage_v`) taper the *applied* current naturally — no
     new schema type. (Claude's recommendation.)
  2. A new `FastChargeCurve` schema type with explicit knee-point/taper
     parameters.
- Blocking: yes

## Q-0003 — New `dc_fast_charge_power_kw` field on `ChargingProfile`

- Date: 2026-09-24
- Context: `ChargingProfile.charge_power_kw` is documented as "typical
  charge power" — for a profile where the predominant charger is Level 2
  (~11 kW) but `dc_fast_charge_fraction` is nonzero, that field can't also
  represent DC fast-charge power (50-250 kW). A fast-charge characterization
  needs its own power figure.
- Options considered:
  1. Add `dc_fast_charge_power_kw: Sourced[float]` to `ChargingProfile`,
     populated as a placeholder across all six profiles, same pattern as
     every other placeholder-stage field in this project. (Claude's
     recommendation.)
  2. Reuse `charge_power_kw` for both purposes (would misrepresent DC fast
     charging for any profile where the predominant charger is slower).
- Blocking: yes

## Q-0004 — Preconditioning: model or defer?

- Date: 2026-09-24
- Context: Modern EVs warm the pack before fast charging; this is a large
  real-world effect and is explicitly why the Leaf struggles at fast
  charging and the Model 3 doesn't — one of this project's flagship
  teaching comparisons. `ChargingProfile.preconditioning_enabled: bool`
  already exists in the schema but is completely unwired (no physics module
  reads it). Doing this properly needs a "the vehicle knows a charge stop is
  coming" signal that doesn't exist anywhere in this simulator (no route/
  navigation concept) plus real control logic (when to start heating, how
  much power to spend, traded off against range) — a genuine feature in its
  own right, not just plumbing.
- Options considered:
  1. **Defer entirely.** No preconditioning modeling in E2e. Every profile's
     fast-charge characterization is equally pessimistic (no one gets the
     benefit) — defensible as a worst-case baseline, but the Leaf-vs-Model3
     fast-charge comparison won't show its real differentiator until a
     later, dedicated milestone.
  2. **Model the outcome only, not the mechanism:** an initial-pack-temp
     offset for `FAST_CHARGE_SESSION` when `preconditioning_enabled=True`
     (same pattern as `cold_soak_offset_k`/`hot_soak_offset_k`) — pack
     simply starts at a favorable temperature, zero physics for the energy
     cost or timing of getting there. Finally wires up the existing dead
     schema field; shows the *direction* of the real effect without
     modeling *why* it works or what it costs.
  Claude leaned weakly toward option 2 but explicitly declined to decide
  this one unilaterally, given the user named it as "the single biggest gap
  a critic would point to."
- Blocking: yes

## Q-0005 — Accept inert entropic heat for fast-charge?

- Date: 2026-09-24
- Context: `docv_dt_v_per_k=0.0` across all six profiles today makes
  entropic heat (`I*T*dOCV/dT`) inert everywhere, already flagged via the
  existing `InertEntropicHeatWarning` mechanism (fires automatically
  whenever `docv_dt_v_per_k==0.0` — no new code needed for the warning
  itself). Entropic heat scales with `|I|`, so this gap is more
  consequential for fast-charge (high sustained current) than for normal
  driving, where it was already accepted as secondary.
- Options considered:
  1. Accept the gap for E2e (irreversible I²R heat, the dominant term at
     high C-rate regardless, is already fully modeled); add emphasis in
     both the new module's docstring and CLAUDE.md that this matters more
     here than the existing note implies; defer real `docv/dT` sourcing to
     the spec-sourcing milestone. (Claude's recommendation, matches the
     user's own stated instinct.)
  2. Source real `docv/dT` curves now, ahead of the spec-sourcing milestone,
     specifically to unblock a credible fast-charge thermal signature.
- Blocking: yes
