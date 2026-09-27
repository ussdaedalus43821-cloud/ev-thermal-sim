# Roadmap

Forward-looking work extracted from `docs/MILESTONES.md`'s full
milestone-by-milestone log — items that are NOT yet built, scheduled for
later, or explicitly left undecided. `docs/MILESTONES.md` is the
historical record of what was built, decided, and why; this file is
what's still open. Each entry below notes which `docs/MILESTONES.md`
milestone it came from, for the full reasoning and evidence behind why
it's ordered/scoped the way it is.

Not exhaustive: many smaller "revisit if X becomes relevant" asides stay
embedded in `docs/MILESTONES.md`'s own findings, where extracting them
would fragment a coherent piece of reasoning for no real benefit. This
file holds the items that are genuinely discrete standalone work items.

---

## Thermal / cooling physics

- **Refrigerant chiller path IS implemented, real, unit-tested, and fully
  investigated (`docs/DESIGN_QUESTIONS.md` Q-0021 and its three follow-up
  milestones in `docs/MILESTONES.md`) — all three candidate causes for why
  it never visibly moved a published number are now closed with evidence,
  and the honest conclusion is a structural limitation, not a missing
  parameter.** `CoolingLoop.chiller_present`/`chiller_capacity_w`/
  `chiller_power_w` are real physics in `cooling.py`/`controller.py`/
  `sim.py`/`long_horizon.py`, confirmed engaging at full capacity under
  real conditions (coolant-node hot-soak initialization fixed; a real,
  NOAA-sourced hot climate — Phoenix, AZ — makes it run flat out;
  `chiller_capacity_w` is now sourced from real commercial bus-BTMS
  product data, not a guess — see the milestones for each). **What all
  three investigations converged on**: even a real, doubled chiller
  capacity only grew the per-trip cooling effect proportionally (e.g.
  0.35K → 0.50K on one phase) while the 1-year aggregate SOH effect stayed
  three to four orders of magnitude too small to matter either way. The
  blocker was never really the specific wattage — it's a genuine TIMESCALE
  mismatch between the battery pack's large thermal mass (990,000 J/K) and
  a single ~20-minute characterized trip, which no further parameter
  re-derivation can fix. Tesla Model 3 in the flagship comparison DID move
  substantially (20-year: 14.35% → 14.84%, ~3.4% relative) from the
  hot-soak-initialization fix alone, since its coolant was previously
  unrealistically cool during hot-soak trips — that's the one place this
  work changed a real published number, and it wasn't the chiller
  operating that did it. This is still the single biggest lever for (a)
  making the BMW i3's battery-thermal modeling non-approximated (see
  Milestone D below — a separate, larger schema question, unaffected by
  this work) and (b) making a future hot-climate `MistakeCase`'s delta
  match real-world magnitudes — but the negligible 1-year Tier-2 result
  already reflects BOTH the per-trip AND the parked-period chiller
  mechanisms together (Tier-2 mixes trip days and fully-parked days over
  the year, and both paths engage the chiller), not just the trip-level
  one — checked directly, not assumed: a full parked day starting at 40°C
  under Phoenix's peak-summer ambient never sustains above the threshold
  long enough for the chiller to show a measurable effect there either
  (pack temp caps essentially at the threshold, doesn't cross it
  meaningfully even over 24 hours). So this isn't a case of "the parked
  path is untried" — both paths were exercised and both show the same
  structural ceiling. Accepting the trip/day-level model can't surface
  this effect at these thermal-mass and duty-cycle magnitudes is the
  honest conclusion, not a search for a different parameter or mechanism.
- **Done: Milestone D — refrigerant-loop schema abstraction**
  (`docs/DESIGN_QUESTIONS.md` Q-0024). The BMW i3's real DX battery
  cooling is now a real `RefrigerantLoop` (schema + `refrigerant.py` +
  `controller.py`/`sim.py`/`long_horizon.py` wiring), not approximated via
  `resistance_to_ambient_k_per_w`. A resistance-cap correctness bug (an
  unbounded direct heat sink let a single trip implausibly overcool the
  pack) was found and fixed mid-implementation by reusing `ThermalNode.
  resistance_to_coolant_k_per_w` as a genuine rate limit — see Q-0024 and
  `docs/MILESTONES.md`'s corresponding milestone for the full record.
  **Do not use i3-vs-Leaf battery comparisons for publication yet** — the
  mechanism is real and validated under two climates (Guam, Seattle), but
  not yet the broader multi-climate/duty-cycle validation pass the six
  passenger profiles' other mechanisms have had; that's the actual
  remaining gate on lifting this caveat, not the schema/mechanism itself.
- **Open follow-up: the BMW i3 battery's `resistance_to_coolant_k_per_w`
  (0.3 K/W) is noticeably weaker than the same field's placeholder value
  on other actively-cooled profiles (e-tron 0.015, Leaf's counterfactual
  0.02, ~0.02-0.05 elsewhere).** It predates Milestone D (set when the
  field was genuinely unused) and now that it's load-bearing for the
  refrigerant loop's rate limit, its magnitude deserves real scrutiny —
  not performed yet. Revisit alongside any future i3 spec-sourcing pass.
- **Open follow-up: multi-climate/duty-cycle validation for the i3's new
  RefrigerantLoop**, matching the chiller's own post-implementation
  investigation arc (hot-soak fix, real-climate check, real capacity
  sourcing) — this milestone validated under Guam and Seattle only.
- **`controller.py`'s cooling-command policy (`_duty_ramp`, pump/fan duty
  vs. temperature above thermostat) is an explicit `TODO(tuning)`
  placeholder, not a calibrated policy.**

## Aging model fidelity

- **Per-vehicle chemistry aging coefficients — deferred until the thermal
  story is demonstrated in isolation (future E3 or later, not scheduled).**
  All six passenger profiles currently share identical aging coefficients
  by deliberate design (isolates thermal architecture as the sole
  variable); real LMO/NCA/NMC/Samsung chemistries age differently. Until
  this lands, vehicle-to-vehicle SOH differences are 100% attributable to
  thermal behavior — a real, current limitation worth remembering when
  reading any cross-vehicle comparison.
- **Calendar aging's purely-linear-in-time formulation (no saturation
  term) is a known extrapolation limit** — the two calibration anchors
  only cover a 5-10 year window; a 20-year run extrapolates 2-4x past
  that. Accepted (Q-0017) as a documented limit, not fixed. Carries
  forward as a deferred aging-model fidelity item — any multi-year SOH
  number presented outside `docs/MILESTONES.md`'s own record should still
  carry `docs/model_reference.md`'s "Validated time horizon" caveat.
- **Neither calendar-aging calibration anchor was ever run against a
  climate with real seasonal amplitude** (Leaf/Guam: 2K seasonal, 4K
  daily; Model3/Seattle: 8K/6K; both anchors themselves were constant-
  temperature, zero-amplitude single-condition benchmarks — see `aging.py`'s
  "Coefficient provenance"). The Blue Bird's 20-year point estimate
  (`docs/MILESTONES.md`'s "Blue Bird Vision Electric — 20-year point
  estimate" milestone) surfaced this concretely, not just theoretically:
  Columbus, OH's 12.5K seasonal / 5.11K daily amplitude produced HIGHER
  calendar-aging loss (56.14% over 20 years) than the Leaf's near-constant
  ~30C Guam climate (49.29%), despite Columbus's much milder annual mean
  and this profile's active liquid cooling vs. the Leaf's passive air
  cooling — the opposite of the pre-run expectation. Likely mechanism
  (evidence-consistent, not chased down or fixed): the calendar term's
  Arrhenius exponential is convex in temperature, so a climate whose hot
  half reaches Guam-like peaks while its cold half drops well below it
  does not average out to "milder than Guam" in aging-rate terms — real
  physics, but never tested against an anchor with this much seasonal
  swing. Re-deriving or bounding the calendar-aging coefficients against a
  realistic seasonal-amplitude range (not just a realistic mean
  temperature) is open, unscoped future work — not attempted opportunistically
  inside the milestone that found it, same discipline as the chiller-path
  gap above.
- **Anchor calibration sensitivity to assumed constant-SOC conditions**
  (from the parked-SOC model milestone): the two calendar-aging
  coefficients were calibrated against anchors stated under constant
  80%/60% SOC — realistic-trajectory predictions carry this systematic
  uncertainty until real fleet SOC distributions justify re-deriving the
  coefficients.
- **`Climate`'s annual/diurnal cosine phases are hardcoded Northern-
  Hemisphere constants** (`ANNUAL_PEAK_DAY_OF_YEAR`, `DIURNAL_PEAK_HOUR`)
  — `Climate` has no hemisphere/timezone field. Southern-Hemisphere /
  inverted-seasonal climates are unsupported until it gains one.
- **`hot_soak_offset_k` is climate-invariant, but physically should scale
  with `Climate.solar_irradiance_w_per_m2`** (a pack in Niger at noon vs.
  Seattle at noon see very different solar loading). Deferred until real
  soak data exists to calibrate a solar-load-scaled version against.
- **Entropic heat is inert until a real dOCV/dT is sourced.** Every
  `BatterySpec` fixture sets `docv_dt_v_per_k=0.0` — a placeholder
  standing in for chemistry data not yet available, flagged via
  `InertEntropicHeatWarning`. Matters most for fast charging (entropic
  heat scales with `|I|`, ohmic heat with `I²`, so the omission is
  relatively larger at high C-rate) — the resulting SOH-loss bias
  magnitude is explicitly unknown, not estimated. Source real dOCV/dT
  curves in the spec-sourcing milestone.

## Fast charging / preconditioning

- **Preconditioning is deferred entirely — explicitly rejected, not just
  deprioritized (Q-0004).** No initial-pack-temp-offset placeholder
  either: that would model the effect without the mechanism, and the
  mechanism has a real energy cost (feeds back into SOC/range) this
  simulator exists to get right. Every fast-charge characterization is
  therefore equally pessimistic across all profiles (no preconditioning
  benefit for anyone) — **this is the single biggest remaining gap in the
  fast-charge story**, and specifically means the Leaf-vs-Model3
  fast-charge comparison won't show its real differentiator until this
  lands. The real feature (thermal load + SOC draw + arrival-temp effect)
  is contingent on a route/navigation concept this simulator doesn't have
  yet, and is its own later milestone.
- **`FAST_CHARGE_SESSION` day interaction with the daily charge-reset
  event is unresolved (Q-0015, raised not resolved).** Currently no
  special-casing — a fast-charge day's session and the day's normal reset
  both apply, redundant but harmless at today's ~5.5% day-type-mix
  share. Revisit if a fast-charge-focused analysis needs the distinction.
- **`ChargingProfile.dc_charge_power_kw`'s field placement is flagged, not
  resolved.** Whether it belongs on `ChargingProfile` at all, or should
  split between a charger abstraction and a per-vehicle max-acceptance-
  rate field, since real effective charge rate is
  `min(charger.max_kw, vehicle.max_acceptance_kw)`. Revisit whoever adds
  a second charger type.

## Long-horizon / Monte Carlo / fork branching

- **Fork branching v2 (Tier-2/long-horizon checkpoint resume) remains
  deferred, its own future milestone.** `LongHorizonCheckpoint` doesn't
  carry `rng_state` or enough schedule-position info to resume from;
  `simulate_long_horizon` always regenerates its RNG/schedule from
  `seed`, starting at day 0. Deliberately not bundled with fork branching
  v1 (full-fidelity forking, done) — gets its own plan review when it's
  next.
- **i.i.d. Monte Carlo sampling over day-type schedule-order variance is a
  legitimate future MC mode, not adopted in v1.** v1 holds the day-type
  schedule seed fixed across every sample (a deterministic shuffle of the
  exact stated annual mix, not i.i.d. draws) — a separate, deferred MC
  mode would sample over schedule order too.
- **`test_long_horizon_validation.py`'s reference generator (Q-0016):
  resolved.** The stale SOC-snapping cadence was fixed to mirror
  `_parked_soc_trajectory`'s own once-per-day reset. A second, distinct
  residual it surfaced — `sim.py`'s aux-draw not being driving-state-aware
  — remains open; see "Charging model gaps" below.
- **`typical_start_soc`'s exact semantics under the parked-SOC model are
  unresolved (Q-0014, raised not resolved).** Current implemented
  behavior (descriptive reference point, not an enforced daily value) was
  not deliberated as definitively correct — it's what naturally fell out
  of the implementation. Revisit once there's a concrete reason to (e.g.
  a downstream consumer needs it to mean something precise).
- **`Scenario.overrides` vs. `ForkPoint.overrides` — not unified,
  undecided.** `Scenario.overrides` is a separate, pre-existing, still-
  unused field with narrower documented scope than what `ForkPoint.
  overrides` (fork branching v1) now supports. A future milestone can
  decide whether to wire it up, deprecate it in favor of the fork
  mechanism, or leave it alone.

## Mistake library

- **Whether the mistake library's own content format should move to YAML
  (matching `configs/profiles/*.yaml`'s convention) is undecided.** Now
  has two Python-constructed entries (Nissan Leaf, Audi e-tron) — still
  not a strong enough signal either way; revisit once a third entry exists
  or the format question actually blocks something.
- Real chiller physics (see "Thermal / cooling physics" above) is the
  single biggest lever for making a future hot-climate `MistakeCase`'s
  delta match real-world magnitudes.
- **Done: second entry** — "Combining battery and powertrain cooling loops
  without upsizing for the added heat load (Audi e-tron)"
  (`examples/mistake_library_etron_shared_powertrain_loop.py`,
  `docs/DESIGN_QUESTIONS.md` Q-0023) — unblocks Milestone D per the user's
  own stated sequencing. Uses a real, already-implemented mechanism
  (`CoolingLoop.serves` sharing one coolant node across components) to
  show a real, non-negligible SOH delta (+2.82pp at 5yr, +11.37pp at a
  20yr point estimate) from merging a battery loop into an un-upsized
  powertrain loop, under a MILD climate — deliberately distinguishing this
  duty-cycle-driven mistake from the Leaf entry's climate-driven one. See
  `docs/MILESTONES.md`'s corresponding milestone for the full record,
  including why every one of the six real vehicles' own documented
  failures (Bolt's cell defect, i3's DX refrigerant, Niro's preconditioning/
  cabin-priority story) turned out NOT usable as-is for a second entry.

## Transition seam

See `docs/seam_reconnaissance.md` for the full seam analysis; this is
just the current punch list, from `docs/MILESTONES.md`'s "Transition
seam" milestone.

- **Gap 2 (Transition route duty-cycle detail) remains deliberately
  untouched.** Its payoff is gated behind gap 1 (a real vehicle profile
  to characterize against); revisit as part of the school-bus profile's
  actual integration into Transition, not populated speculatively ahead
  of that.
- **Done: a first, real, one-row SOH-delta table exists**
  (`examples/generate_seam_table.py`, `SeamTableRow`/`SeamTable` in
  `schema.py`) — Blue Bird Vision Electric / Columbus, OH, a single fixed
  `duty_cycle_archetype`, `methodology="point_estimate"` (not the MC
  distribution §5 originally proposed — a deliberate, documented v1 scope
  choice, not silently substituted). Its cumulative 20-year loss matches
  the already-validated Blue Bird figure exactly, confirming internal
  consistency. Every row carries its own caveats (point-estimate-not-
  median, SOC/time-horizon extrapolations, unvalidated commercial-
  chemistry coefficients, chiller-not-engaged-by-this-climate) directly in
  the JSON artifact, not just in this repo's docs. See
  `docs/MILESTONES.md`'s "Generate a first Transition-seam table row"
  milestone.
- **A real, previously-unidentified gap this surfaced**: Transition's
  `ClimateZone.key` (e.g. `"5A"`) is an ASHRAE zone code, not a unique
  per-location identifier — both current presets share it. The seam table
  keys `climate_zone` by a descriptive per-location string instead
  (`"columbus_oh"`); Transition-side wiring will need to resolve this
  collision for real, not treat it as solved.
- **Done: Transition-side wiring** — `VehicleClass.manufacturerProfileKey?:
  string` and `Route.batterySohPercent: number | null` exist in
  `Transition/src/types.ts`, School Bus Depot's one vehicle class points
  at `"blue_bird_vision_electric"`, and `rollEvents()` decrements each
  converted route's `batterySohPercent` monthly from the seam table
  (`Transition/src/seamTable.ts`, generated from `examples/
  generate_seam_table.py`'s output, not hand-transcribed), displayed on
  the Overview tab. Scoped, by the user's explicit choice, to **track
  only — no cash/uptime/DSCR consequence yet**. See `docs/MILESTONES.md`'s
  "Transition-side wiring" milestone for the full record, including the
  `Route`↔`VehicleClass` link gap this surfaced (routes don't reference
  their vehicle class directly; the lookup uses `vehicleClasses[0]`, a v1
  simplification valid only while every preset has exactly one class) and
  the still-unresolved `climate_zone` keying collision noted above (this
  wiring matches on `vehicleClass` only, so the collision wasn't yet
  forced to a decision). Committed in `Transition` (`0c6c5c2`) but not
  pushed — flagged for the user, same as the prior `Transition` commit.
  **Open follow-ups, not built here**: any actual gameplay consequence for
  `batterySohPercent` (a cost, an event, a DSCR effect); a real per-route
  `VehicleClass` reference for a genuinely multi-class fleet; resolving
  the `climate_zone` collision once a route needs to match on it; and a
  seam-table row for Tank Wash's truck class, which still has none.
- **Done: `Climate`'s `altitude_m`/`solar_irradiance_w_per_m2` are now on
  Transition's `ClimateZone`** (as `altitudeFt`/`solarIrradianceWPerM2`,
  Fahrenheit/imperial convention matching that file's existing temperature
  fields) — real elevations for both presets (Columbus, OH ~889ft; Cedar
  Rapids, IA ~810ft), a shared 250 W/m² placeholder for irradiance
  (`solarYieldFactor` remains a different quantity — annual generation
  yield, not peak irradiance). A real `Scenario` for the seam no longer
  fails on missing `Climate` fields. See `docs/seam_reconnaissance.md`
  §7 and `docs/MILESTONES.md`'s corresponding milestone.
- **Done: the Blue Bird profile has a real `UsageProfile`/`ChargingProfile`,
  now CANONICAL (`docs/DESIGN_QUESTIONS.md` Q-0022, not scaffolding scoped
  to a single validation pass), run through a real Tier-1 characterization
  and both 90-day and 20-year Tier-2 trajectories, under both its native
  climate and a real hot-climate stress test.** See `docs/MILESTONES.md`'s
  "Blue Bird Vision Electric validation," "Chiller path implementation,"
  "Hot climate chiller investigation," "Source real chiller_capacity_w
  data," and "Promote Blue Bird's UsageProfile/ChargingProfile to
  canonical" milestones for the full record — including a real causal-
  event bug fixed across all seven profiles, an implausible-placeholder
  fix (`inverter.peak_current_a`), a real chiller-path implementation, and
  a canonical SOC convention (`typical_end_soc=1.0`, `typical_start_soc≈
  0.5333`, both derived from this vehicle's own disclosed range/usage —
  carries an extrapolation caveat, since 1.0 sits outside `aging.py`'s
  80%/60% calendar-aging calibration range) and `day_type_mix` (cold=79,
  hot=16, neutral=85, derived live from Columbus's own real climate, not
  an even split). This is now the most thoroughly validated profile in
  the project.
- **The second Transition vehicle class (tank-wash truck) is explicitly
  deferred** until the seam is proven end-to-end on the primary (school
  bus) case.
- **The seven aging-rate coefficients, reused verbatim for the Blue Bird
  profile, have never been validated against any commercial/heavy-duty
  vehicle's chemistry or duty cycle.** Don't treat this profile's SOH
  trajectory as validated the way the six passenger profiles' are, and
  don't use it in a published comparison without re-examining this gap
  first.

## Categorical fields not yet `Sourced`-wrapped

- **`chemistry`, `motor_type`, `topology`, `coolant_type` are plain
  unwrapped `str`, not `Sourced[str]`**, inconsistent with newer
  categorical fields (`cooling_mechanism`, `heat_pump_type`) which are
  wrapped. Deliberately not retrofitted — real churn (every profile
  fixture/test constructs these positionally) for no immediate payoff.
  Retrofit in a later milestone when there's an actual reason to touch
  every call site anyway.

## Schema-only fields not yet read by any physics module

- **`ThermalArchitecture.heat_pump_recovers_powertrain_waste_heat`** —
  descriptive metadata distinguishing a bolt-on cabin heat pump from an
  integrated waste-heat-recovery system; wiring actual recovery into the
  combined thermal network is a future milestone.
- **`ambient_temp_k` is inert in motor/inverter/battery `step_*`
  functions** — none of `MotorSpec`/`InverterSpec`/`BatterySpec` gives its
  `ThermalNode` an ambient conductance in those isolated per-component
  calls (only `sim.py`'s combined network actually uses ambient coupling).

## Charging model gaps

- **The adaptive dt policy's "or charging" clause isn't wired up** — the
  tiered policy was meant to be "driving OR charging: fine dt," but
  sim.py only checks vehicle speed, since there's no external-charger
  model yet to make "parked but charging" a distinguishable state. Wire
  this in once a charger model exists.
- **`sim.py`'s aux-draw (`aux_current_a`, from `hvac_power_w +
  accessory_load_w`) is applied unconditionally every step, not
  driving-state-aware.** During a real parked period, `hvac_power_w` is
  not a realistic 24/7 drain — `long_horizon.py`'s Tier-2 decay model
  already accounts for this by using `accessory_load_w` alone while
  parked (see `simulate_long_horizon`'s own docstring), but `sim.py`
  itself has no such distinction, so any full-fidelity per-second run
  through a parked period over-draws SOC relative to Tier-2's own
  assumption. Confirmed as a real, measurable residual (not just
  theoretical) by `docs/DESIGN_QUESTIONS.md` Q-0016's resolution: after
  fixing `test_long_horizon_validation.py`'s reference-generator cadence
  to otherwise match Tier-2, a 0.122%/0.035% absolute (Leaf/Model 3)
  residual gap remained, consistent with this exact mechanism. Closing it
  means making `sim.py`'s aux-draw driving-state-aware — a real
  core-physics change (not a test-harness fix), deserving its own plan
  review, not attempted opportunistically inside Q-0016's resolution.
- **`advance_charging_step` doesn't model cabin HVAC/accessory draw during
  a charging session at all** — found while wiring the chiller's own
  parasitic draw into the charge current request (Q-0021): cooling-loop
  draw (pump/fan/chiller) now nets against the charger's power budget, but
  `aux_power_w` (hvac/accessory) is a separate, pre-existing simplification
  this fix deliberately did not touch. A real DC fast-charge session with
  cabin conditioning running would draw against the same charger budget.
  Revisit alongside a real charger model.

## Physics simplifications flagged for eventual replacement

- **Motor mechanical-loss coefficient needs a real efficiency map**
  (spec-sourcing milestone) — the current placeholder produces implausible
  absolute losses at realistic driveline speeds; the demo profile's
  values were recalibrated for plausibility but likely overshot low
  (~30°C steady-state at cruise vs. a real motor's typical 60-90°C).
- **Cross-domain torque/current/power conversion in `controller.py`
  ignores motor/inverter conversion losses** — a fully loss-aware version
  needs actual operating-point efficiency, which is circular at this
  supervisory layer. Real stall-torque limiting (below the minimum speed
  for torque conversion) needs a separate current-limit table, not
  modeled yet.
- **Categorical descriptor fields retrofit** — see above.
- **Multi-loop/dual-motor cooling architecture beyond what Milestones
  A-C already built** — `ManufacturerProfile.thermal_architecture.
  thermal_network` (the generic node/edge topology description) is still
  unused; sim.py builds its combined network from `cooling_loops`/
  `serves` directly rather than reading this more general structure.
