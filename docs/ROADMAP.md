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

- **Refrigerant chiller path is not implemented in `cooling.py`.**
  `CoolingLoop.chiller_present`/`chiller_capacity_w` exist in the schema
  but do nothing — a loop with `chiller_present=True` gets no extra heat
  rejection. This is the single most-repeated open item in the project's
  history (surfaced independently in the original cooling.py milestone,
  the BMW i3's DX-cooling approximation note, the Tier-2 parked-period
  coolant-loop investigation, the mistake library's first entry, and the
  Blue Bird school-bus profile — which is the first profile to actually
  set `chiller_present: true`). Root physical reason it keeps mattering:
  a radiator-only loop can only reject heat toward ambient, never below
  it; real hot-climate degradation is driven by sustained high ambient
  temperature itself, which only an active chiller can address. This is
  the single biggest lever for (a) making the BMW i3's battery-thermal
  modeling non-approximated (see Milestone D below), and (b) making a
  future hot-climate `MistakeCase`'s delta match real-world magnitudes
  (the current Leaf passive-vs-active-cooling case reports +0.06pp SOH
  over 5 years, far below the ~20%-loss-in-12-months real-world citation,
  precisely because of this gap). A materially bigger, separately-scoped
  physics addition (affects the full per-second model, not just Tier-2)
  deserving its own plan review — deliberately not implemented opportunistically
  inside any of the milestones that ran into it.
- **Milestone D (scheduled but not next): refrigerant-loop schema
  abstraction.** The schema has no way to represent an active refrigerant
  loop (the BMW i3's DX system, or a battery chiller circuit) as anything
  other than a `CoolingLoop` (glycol-shaped) or the node-level ambient
  path. The i3's profile currently approximates DX cooling with an
  unrealistically low `resistance_to_ambient_k_per_w` on its battery node
  — a stand-in, not a real refrigerant-thermodynamics model. **Do not use
  i3-vs-Leaf battery comparisons for publication until this lands.**
  Deliberately ordered behind aging/long-horizon capability (now built) —
  was correctly sequenced, not abandoned.
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
- **`test_long_horizon_validation.py`'s reference generator still uses the
  OLD SOC-snapping assumption, and needs its own follow-up fix (Q-0016,
  raised not resolved).** Currently still passes only because both
  real-profile losses are small enough at its 60-day window to sit under
  the 0.1% absolute tolerance floor — relative error grew from ~0.8-4%
  historically to 15.64%/10.14% post-parked-SOC-fix. A longer window or
  different seed could push this past the floor and fail.
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
  (matching `configs/profiles/*.yaml`'s convention) is undecided.** Not
  yet justified with only one Python-constructed entry.
- Real chiller physics (see "Thermal / cooling physics" above) is the
  single biggest lever for making a future hot-climate `MistakeCase`'s
  delta match real-world magnitudes.

## Transition seam

See `docs/seam_reconnaissance.md` for the full seam analysis; this is
just the current punch list, from `docs/MILESTONES.md`'s "Transition
seam" milestone.

- **Gap 2 (Transition route duty-cycle detail) remains deliberately
  untouched.** Its payoff is gated behind gap 1 (a real vehicle profile
  to characterize against); revisit as part of the school-bus profile's
  actual integration into Transition, not populated speculatively ahead
  of that.
- **Transition-side wiring is not started**: a `manufacturerProfileKey`-
  style field on `VehicleClass` pointing at the new Blue Bird profile, and
  the precomputed SOH-delta table interface itself (shape proposed in
  `docs/seam_reconnaissance.md` §5).
- **`Climate`'s `altitude_m`/`solar_irradiance_w_per_m2` still aren't on
  Transition's `ClimateZone`** (`solarYieldFactor` is a different
  quantity — annual generation yield, not peak irradiance). A real
  `Scenario` for the seam still can't be built until these exist there
  too, even though the three temperature fields (gap 3) are closed.
  Altitude is a trivial per-location lookup; a generic peak-irradiance
  placeholder (the passenger profiles all use one) would suffice for a
  first version.
- **Done: the Blue Bird profile now has a real `UsageProfile`/
  `ChargingProfile` and has been run through a real Tier-1 characterization
  and a 90-day Tier-2 trajectory** — see `docs/MILESTONES.md`'s "Blue Bird
  Vision Electric validation" milestone for the full record, including a
  real causal-event bug this validation caught and fixed (affecting all
  seven profiles, not just this one) and a second implausible-placeholder
  fix specific to this profile (`inverter.peak_current_a`). Still open: the
  `UsageProfile`/`ChargingProfile` built for this were constructed for this
  validation pass, not yet formally adopted as the profile's canonical
  usage/charging data for a published Transition-seam comparison — revisit
  once that comparison is actually being built.
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
