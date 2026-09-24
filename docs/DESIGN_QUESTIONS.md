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
- **Resolution (2026-09-24): Option 1, approved conditionally.** Separate
  functions, on the explicit condition that heat-generation physics is not
  duplicated between the charge and discharge paths — divergent charge/
  discharge heat models is "the exact failure mode that kills comparative
  sims." Condition holds by construction, not by promise: `advance_charging_
  step` calls `battery.compute_battery_electrical_step` directly (the exact
  same function `advance_one_step` already calls for discharge), just with a
  negative `current_request_a` — that function already computes
  `irreversible_heat_w`/`entropic_heat_w` identically for both directions,
  so there is no second heat model to keep in sync. Same reuse applies to
  the thermal solve: `advance_charging_step` calls the same `step_thermal_
  network` over the same combined network `advance_one_step` builds, with
  motor/inverter heat inputs at zero rather than a separate/divergent
  thermal path. See CLAUDE.md's Known deferred work, Milestone E2e entry.

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
- **Resolution (2026-09-24): REFRAMED. Neither original option — this is
  the one decision that changes what E2e is.** Constant power + natural
  voltage-limit taper models the *battery's* physics but not the *BMS's*.
  Real DC fast charging is limited by an explicit BMS-enforced
  `I_max(SOC, T_cell)` curve, which IS the thermal-protection mechanism —
  it's the specific reason a passively-cooled Leaf in Guam throttles hard
  and a liquid-cooled, actively-preconditioned pack doesn't. Shipping E2e
  without it means E2e doesn't unlock the comparison it's on the roadmap
  for. A separate `FastChargeCurve` type was also rejected as the wrong
  abstraction regardless: a static SOC-only curve is temperature-blind,
  backwards for a thermal simulator.
  **Ratified design: constant power request PLUS a sourced
  `I_max(SOC, T_cell)` limit; the natural taper is implicit in that limit,
  not a separate mechanism.** Scope call: the BMS current limit IS
  Milestone E2e, not deferred to a later E2e-2 — E2e is "the Leaf-in-Guam
  story," not just charge-mode plumbing. Full curve-shape design deferred
  to the E2e plan revision (see CLAUDE.md's Known deferred work, Milestone
  E2e entry, for the ratified schema shape).
  **Dependency surfaced by this resolution:** Q-0001's `advance_charging_
  step` now has a third responsibility beyond "absorb current" and "run the
  thermal solve" -- it must also apply the `I_max(SOC, T_cell)` limit. This
  doesn't reopen Q-0001 (still separate functions, still no duplicated heat
  physics), but the plan for it must reflect this before implementation.

## Q-0003 — New `dc_fast_charge_power_kw` field on `ChargingProfile` (renamed `dc_charge_power_kw` on resolution)

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
- **Resolution (2026-09-24): Option 1, approved with two nits.** (1) Field
  renamed `dc_charge_power_kw` — "fast" was redundant given "dc" already
  implies it. (2) Flagged, not blocking: whether this belongs on
  `ChargingProfile` at all, or should split between a charger abstraction
  and a per-vehicle max-acceptance-rate field. Real effective charge rate
  is `min(charger.max_kw, vehicle.max_acceptance_kw)`. `ChargingProfile` is
  currently the vehicle-usage-context abstraction (paired with a specific
  `ManufacturerProfile` via `Scenario`), not a standalone charger model —
  if a second charger type is ever added, conflating "what the charger can
  deliver" with "what the vehicle can accept" in one field may need
  unwinding. Not addressed in E2e; noted for whoever adds a second charger
  type. See CLAUDE.md's Known deferred work, Milestone E2e entry.

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
- **Resolution (2026-09-24): Option 1. Defer entirely. Option 2 (the
  temp-offset placeholder) explicitly rejected, not just deprioritized.**
  Reasoning: option 2 is a trap of a specific kind — it models the
  *effect* of preconditioning without its *mechanism*, and the mechanism
  has a real energy cost that feeds back into SOC and range, which are
  exactly the quantities this simulator exists to get right. A temp-offset
  placeholder hides that coupling. Downstream code and analyses would start
  depending on the placeholder's presence, and removing it later to build
  the real feature means unwinding whatever got built on top of it — this
  project's methodological strength is not taking that kind of shortcut,
  and that's worth protecting deliberately. If a "what if preconditioned?"
  answer is needed before the real feature lands (e.g., for the mistake-
  library entry), it should be a one-off analysis script that re-runs the
  sim with a warmer initial pack temperature — an experiment, not a schema
  field. The real feature (preconditioning as thermal load + SOC draw +
  arrival-temp effect, contingent on a route/navigation concept this
  simulator doesn't have yet) is its own later milestone, not part of E2e.
  See CLAUDE.md's Known deferred work, Milestone E2e entry.

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
- **Resolution (2026-09-24): Option 1, approved, "emphasize in docs" made
  concrete.** A vague caveat doesn't survive refactoring; a specific,
  actionable note does. Added to `docs/model_reference.md`'s "Aging model
  confidence and limits" section: entropic heat is omitted (not modeled as
  zero by measurement, simply unsourced — `docv_dt_v_per_k=0.0` for all six
  profiles); the qualitative direction is defensible at fast-charge rates
  specifically because ohmic heat scales with `I^2` while entropic heat
  scales linearly with `I`, so entropic heat's relative contribution to
  total heat generation shrinks as C-rate rises — real physics, not
  vehicle-specific data. The magnitude of the resulting SOH-loss bias is
  explicitly marked unknown/flagged, not estimated, since no real `docv/dT`
  data exists for any of the six profiles to ground a number. See CLAUDE.md's
  Known deferred work, Milestone E2e entry, and `docs/model_reference.md`
  directly for the full note.
