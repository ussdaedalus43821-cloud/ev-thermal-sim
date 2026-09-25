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

## Q-0006 — EFC normalization: nominal or current (SOH-scaled) capacity?

- Date: 2026-09-24
- Context: Milestone E2e's first real Leaf-vs-Model3 comparison surfaced a
  genuine modeling gap in `aging.py`: `compute_aging_step`'s cycle-aging
  term uses `throughput_ah = abs(applied_current_a) * dt_s / 3600.0`, an
  absolute amp-hour quantity never normalized by pack capacity. Hand-traced
  proof: holding C-rate and temperature equal, `cycle_delta` scales EXACTLY
  linearly with `capacity_nominal_ah` (Leaf/Model3 ratio 3.017, matching
  their capacity ratio to three decimal places) — meaning pack size, not
  just thermal architecture, is a live confound in any cross-vehicle
  cycle-aging comparison. The fix is to normalize throughput into
  "equivalent full cycles" (EFC): `throughput_efc = throughput_ah /
  capacity_ah`. The open question is which capacity to divide by —
  `BatterySpec.capacity_nominal_ah` (the fixed, unaged reference value,
  same field `compute_aging_step` already takes `spec` — the UNAGED spec —
  from) or the pack's current, SOH-scaled effective capacity (which
  `apply_soh_to_battery_spec` computes, but `compute_aging_step` is never
  called with an already-aged spec — see its own docstring: "`spec` must
  be the pack's UNAGED spec... aging rate coefficients don't themselves
  change with SOH").
- Options considered:
  1. **Nominal capacity** (`capacity_nominal_ah`, fixed for the life of the
     spec). Simpler: EFC count is a straightforward linear rescaling of the
     existing `throughput_ah` term, no new dependency introduced into
     `compute_aging_step` (which today only ever sees the unaged spec — see
     Context above), and no feedback loop between SOH and its own aging
     rate to reason about or validate. Slightly physically imprecise at low
     SOH (a degraded pack's true 1.0 EFC is less absolute Ah than a fresh
     pack's, since capacity itself has shrunk) — a fresh pack and an 80%-
     degraded pack throughput-matched in raw Ah would count as a smaller
     EFC fraction for the degraded pack under this option, when arguably it
     should count as a larger one. (Claude's recommendation.)
  2. **Current (SOH-scaled) capacity.** More physically precise at low SOH
     — an EFC is defined relative to the pack's actual present capacity in
     the real-world convention. Requires `compute_aging_step` to take an
     additional current-SOH-aware capacity input (today it only receives
     the unaged `spec`), and introduces a genuine feedback loop: a pack's
     own past aging changes the denominator of its own future aging rate,
     which is real (accelerating degradation is a known real phenomenon)
     but adds an unverified feedback mechanism into a term this project has
     otherwise deliberately kept as one-way (calendar/cycle rates read
     `soh`/`soc`/`cell_temp_k` but do not read the aging model's own prior
     output as an input to itself elsewhere in `aging.py`).
- Blocking: yes
- Claude's recommendation: **Option 1 (nominal capacity).** Reasoning
  restated per the instruction that raised this question: cleaner anchor
  re-derivation (one variable changes, not two), no unverified feedback
  loop, and the difference between the two options is small at the SOH
  levels this simulator currently targets (E1/E2 runs generally examine
  SOH trajectories well above the range — deep into a pack's degraded
  life — where nominal-vs-current capacity would diverge meaningfully).
  State this choice explicitly in `aging.py`'s docstring rather than
  leaving it implicit, and revisit if a future milestone specifically
  targets low-SOH (near-end-of-life) runs as a real use case, where the
  approximation error would matter more.
- **Resolution (2026-09-24): Option 1 (nominal capacity), approved with the
  reasoning corrected.** The deciding factor is NOT simplicity — it's that
  current-capacity normalization would introduce an uncalibrated
  positive-feedback mechanism (SOH -> EFC rate -> SOH) this project cannot
  currently validate. `cycle_aging_rate_per_efc`'s field docstring in
  `schema.py` states this explicitly: normalized by `capacity_nominal_ah`
  (never the current SOH-scaled capacity), the feedback mechanism named as
  the reason, and the resulting trajectory flagged as a bounded
  approximation valid at SOH >= 0.7. See CLAUDE.md's Known deferred work
  for the implementation record.

## Q-0007 — Re-derivation source for `cycle_aging_rate_per_efc`

- Date: 2026-09-24
- Context: Fixing Q-0006 means renaming `cycle_aging_rate_per_ah` to
  `cycle_aging_rate_per_efc` and re-deriving its value against whatever
  anchor `cycle_aging_rate_per_ah` was originally fit against, expressed in
  EFC terms instead of Ah terms. Investigated directly (not assumed) by
  re-reading `aging.py`'s own "Coefficient provenance" docstring section:
  the single cycle-aging anchor is **"82% SOH at 100,000 kWh pack
  throughput at 25°C / 298.15K,"** converted to amp-hours via an ASSUMED
  reference pack voltage of 370V (not tied to any specific one of the six
  profiles — "a rough average across the six reference profiles") and an
  assumed reference C-rate of 1.0, giving ~270,270 Ah. This anchor is
  **energy-throughput-shaped (kWh), not amp-hour-shaped and not EFC-shaped
  in its original form** — it makes no reference to any specific pack's
  capacity at all, generic ("a fleet" pack throughput number), which is
  exactly why converting it to Ah already required inventing an assumed
  reference voltage rather than reading one off a real spec.
- Options considered:
  1. **Treat it as needing a THIRD assumed reference constant: a reference
     pack capacity**, to convert the existing 100,000 kWh -> 270,270 Ah
     anchor into an EFC count: `EFC = throughput_ah / reference_capacity_ah`.
     This is not a "rename-and-rescale" — the anchor was never EFC-shaped,
     so a genuinely new assumption is needed on top of the existing 370V
     and 1C ones, the same kind of "simplest defensible assumption, not
     fit" the existing docstring already uses for the other two unconstrained
     cycle-aging coefficients. A natural choice: use the same 370V reference
     alongside an assumed reference pack size (candidates: the mean or
     median `capacity_nominal_ah` across the six reference profiles, or a
     round generic number like 75 kWh / ~200Ah, roughly mid-pack across the
     six). (Claude's recommendation, pending which specific reference
     capacity value the user wants — see below.)
  2. **Reinterpret the anchor as already being "per representative pack"**
     and skip a separate reference-capacity assumption by reusing the same
     370V-and-implied-capacity figure some other way. Considered and set
     aside: no version of the existing anchor actually ties itself to a
     capacity value today (only voltage and C-rate were assumed), so this
     option doesn't avoid inventing a new number, it just tries to hide
     that a new number is being invented.
- Blocking: yes
- Claude's finding, not yet a recommendation on the exact number: the
  anchor was Ah-shaped (after an existing voltage-based conversion from its
  true original kWh form), NOT EFC-shaped, so **this re-derivation needs a
  new, explicitly-assumed reference pack capacity — a third free parameter
  alongside the existing 370V/1C assumptions — with its own stated
  reasoning documented in `aging.py`'s docstring, the same way the existing
  two assumptions are each individually justified there.** Needs the user's
  input on which reference capacity to assume (mean of the six profiles,
  a round generic number, or another convention) before the re-derivation
  in step 3 of the implementation plan can proceed.
- **Resolution (2026-09-24): Option 1, reference capacity = 75 kWh
  (~202.7 Ah at the existing 370V assumption), explicitly generic, NOT the
  mean (~153.5 Ah) or median (~175.7 Ah) of the six reference profiles' own
  `capacity_nominal_ah` values.** Reasoning: a mean/median would carry a
  hidden "this represents our fleet" claim that capacity, unlike voltage,
  doesn't support the same way — the existing 370V assumption is already a
  rough average across the six profiles and is treated as a shared,
  profile-agnostic constant; capacity varies far more (~4x) across the six
  profiles than voltage does, so averaging it would silently bake in a
  "typical pack size" assumption this project's aging-coefficient
  methodology explicitly avoids elsewhere (see `aging.py`'s "Shared
  coefficients" section). Conversion: `100,000 kWh / 75 kWh = 1,333.33 EFC`
  at 25°C/1C -> 82% SOH; the 370V assumption cancels out of this ratio
  entirely, since both the throughput and the reference capacity were
  converted through it. `cycle_aging_rate_per_efc = 0.18 / 1,333.33 =
  1.350e-4` (hand-verified before implementation, then confirmed against
  the code via `test_cycle_aging_matches_throughput_anchor_within_one_
  percent` in `tests/test_aging.py`). Documented as a third stated
  assumption alongside the existing 370V/1C pair, same register, in
  `aging.py`'s "Coefficient provenance" section. See CLAUDE.md's Known
  deferred work for the full implementation record and the corrected
  Leaf/Model3 comparison numbers.
