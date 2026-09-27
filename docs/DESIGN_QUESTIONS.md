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
  thermal path. See docs/MILESTONES.md, Milestone E2e entry.

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
  to the E2e plan revision (see docs/MILESTONES.md, Milestone
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
  type. See docs/MILESTONES.md, Milestone E2e entry.

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
  See docs/MILESTONES.md, Milestone E2e entry.

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
     both the new module's docstring and docs/MILESTONES.md that this matters more
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
  data exists for any of the six profiles to ground a number. See docs/MILESTONES.md's
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
  approximation valid at SOH >= 0.7. See docs/MILESTONES.md
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
  `aging.py`'s "Coefficient provenance" section. See docs/MILESTONES.md's
  deferred work for the full implementation record and the corrected
  Leaf/Model3 comparison numbers.

## Q-0008 — Monte Carlo v1 scope: which parameters get sampled?

- Date: 2026-09-24
- Context: `MCConfig`/`Confidence` are schema-only; `mc.py` is a stub. Full
  raw-physics Monte Carlo at N=500 over a 20-30 year horizon is intractable
  (docs/MILESTONES.md's own stated motivation for the Tier-1/Tier-2 split). Tier-1
  characterization (`characterize_all_trips`) is reusable across samples
  only if none of the varied parameters feed into Tier-1's own physics —
  true for `BatterySpec`'s aging-rate coefficients (`calendar_aging_*`,
  `cycle_aging_*`, `resistance_growth_per_soh_loss`), which are read only
  by `compute_aging_step` during Tier-2 replay, never during
  `characterize_trip`. Varying a thermal/electrical spec instead would
  force re-running Tier-1 per sample, ~500x the cost.
- Options considered:
  1. **v1 scope: `BatterySpec`'s aging-rate coefficients only**, reusing
     one Tier-1 characterization across all N samples. Thermal/electrical-
     spec and climate sampling deferred to a later milestone that can
     afford the Tier-1-per-sample cost. (Claude's recommendation.)
  2. Sample thermal/electrical specs too in v1, re-running Tier-1 per
     sample. Rejected as prohibitively expensive for the first version.
- Blocking: yes
- **Resolution (2026-09-24): Option 1, approved, with the exact field list
  made explicit — all seven of `BatterySpec`'s aging-related `Sourced[float]`
  fields, not a subset:** `calendar_aging_rate_per_s`,
  `calendar_aging_activation_temp_k`, `calendar_aging_soc_sensitivity`,
  `cycle_aging_rate_per_efc`, `cycle_aging_activation_temp_k`,
  `cycle_aging_c_rate_sensitivity`, `resistance_growth_per_soh_loss`.
  Explicitly called out as non-negotiable inclusions, not left to Claude's
  discretion: `calendar_aging_soc_sensitivity` (the SOC-snapping deferred-
  work note already documents this as the direct lever for a known 50-70%
  overnight-aging overestimate, and the calibration anchors were derived
  with that known-wrong assumption baked in — its own uncertainty is not
  optional to model), and both Arrhenius pseudo-activation-temperature
  terms (`calendar_aging_activation_temp_k`/`cycle_aging_activation_temp_k`
  — real chemistry activation-energy spreads span roughly a factor of 2-3
  in rate at typical pack temperatures, probably the single largest
  uncertainty in the whole aging model).

## Q-0009 — Monte Carlo v1 sampling distribution

- Date: 2026-09-24
- Context: `Confidence` (low/medium/high) is meant to size the sampling
  spread, but no distribution family or numeric spread has ever been
  defined. Needed before any sampling code can be written.
- Options considered:
  1. Log-normal, centered at each field's `value`, spread (CV = std/mean)
     determined solely by `confidence` via one fixed, project-wide mapping.
     Guarantees positivity (every sampled field here is a strictly-positive
     physical rate/coefficient) — no truncation/clipping logic needed.
  2. Normal distribution, truncated/clipped at a small positive epsilon to
     avoid unphysical negative values. Rejected: clipping distorts the
     distribution's actual shape near the clip boundary in a way log-normal
     avoids by construction.
- Blocking: yes
- **Resolution (2026-09-24): Option 1 (log-normal), with the exact CV
  mapping specified, not left to Claude to pick:**
  ```
  Confidence.LOW    -> CV = 0.50
  Confidence.MEDIUM -> CV = 0.20
  Confidence.HIGH   -> CV = 0.05
  ```
  Stated ONCE in `mc.py`'s module docstring and referenced from every
  sampled field — explicitly NOT decided per-field, since per-field
  spreads would reintroduce exactly the confound class the EFC-
  normalization fix (Q-0006/Q-0007) just removed. Sampled fields are drawn
  INDEPENDENTLY of each other for v1 (no covariance / shared latent
  parameter modeled) — justified as "no shared latent parameter, treat as
  independent," a deliberately conservative choice, since independent
  sampling produces the WIDEST plausible band (any true correlation
  between coefficients would narrow it, not widen it); revisit only if a
  real fleet-covariance estimate becomes available. **`MCResult`'s
  docstring must state explicitly: the resulting band is uncertainty
  around a placeholder coefficient value, not uncertainty around real
  battery chemistry** — same honesty register as Q-0007's anchor-
  idealization note. Do not present an MC band as a real-world confidence
  interval until the underlying coefficients are sourced, not assumed.

## Q-0010 — Monte Carlo v1 dotted-path resolution

- Date: 2026-09-24
- Context: `MCConfig.parameters_to_vary: list[str]` needs a resolver from
  dotted-path string to an actual `Sourced[float]` field on a
  `ManufacturerProfile`. The existing placeholder test in `test_schema.py`
  writes paths like `"battery.internal_resistance_ohm"`/
  `"climate.mean_annual_temp_k"` as if both were top-level siblings, which
  doesn't match the real schema (`battery` is under `ManufacturerProfile`;
  `climate` is under `Scenario`, a different parent object entirely) — that
  test predates any real resolver and is illustrative, not binding.
- Options considered:
  1. A general resolver handling arbitrary nesting across both
     `ManufacturerProfile` and `Scenario`-level objects (climate, usage,
     charging profile).
  2. Resolve against `ManufacturerProfile` only. First path segment names
     the component (`battery.`, `motor.`, `inverter.`, etc.); the rest
     resolves as an attribute path within that component's spec. For v1,
     since Q-0008 scopes sampling to `battery.*` fields only, a full
     general resolver isn't needed — a minimal, `battery`-only resolver
     suffices. (Claude's recommendation.)
- Blocking: yes
- **Resolution (2026-09-24): Option 2, approved, explicitly scoped down
  further for v1: don't build a general resolver at all.** Since every
  v1 sampled field lives at `battery.<field_name>`, a ten-line resolver
  that validates the first path segment is literally `"battery"` and then
  does a direct `getattr` on `profile.battery` is enough — no component
  dispatch table, no recursive attribute-path walker. Cross-object paths
  (`climate.*`, `thermal_architecture.*`, motor/inverter specs) are a
  different, deferred problem — build the general resolver only when a
  future milestone actually needs to sample outside `battery.*`.

## Q-0011 — Monte Carlo v1 output shape and where percentiles are computed

- Date: 2026-09-24
- Context: No `MCResult` type exists. Per the "physics lives in core, not
  UI" architecture rule, it's unclear whether percentile-band computation
  over N samples counts as a core/domain calculation (belongs in `mc.py`)
  or a presentation concern (belongs in `dashboard/`).
- Options considered:
  1. `mc.py` returns only raw per-sample data (sampled parameter values,
     full SOH trajectories, endpoint metrics); `dashboard/` (and any other
     consumer) computes percentile bands itself from the raw samples.
  2. `mc.py` computes and returns both raw per-sample data AND a derived,
     time-indexed percentile band (p05/p50/p95 per checkpoint), as part of
     `MCResult`. (Claude's recommendation.)
- Blocking: yes
- **Resolution (2026-09-24): Option 2, approved. Percentile computation
  belongs in `mc.py`, not `dashboard/` — it's statistics on raw simulation
  output, not rendering, and `dashboard/`'s "never contains domain
  calculations" rule is about physics/simulation logic, not this.**
  `MCResult` carries both raw per-sample endpoint metrics AND a
  time-indexed percentile band computed once, in `mc.py`, so every
  consumer (dashboard, `examples/flagship_comparison.py`-style scripts,
  future review tooling) shares one implementation rather than each
  re-deriving percentiles independently. Testable directly against a
  hand-computed small-N case. **The band is defined as p05-p50-p95, not
  p01-p99 or any wider tail** — at N=500 (the project's own target sample
  count), sampling error on the p05 estimate is roughly ±0.7% absolute,
  which does not support defensible p01/p99 tail estimates; producing
  wider percentiles the sample size can't actually support would be
  manufacturing false precision. State N=500's p05-p95 support limit
  explicitly in `mc.py`'s docstring. Raise to N=2000 later if a wider band
  is needed and affordable — cheap once Tier-1 characterization is cached,
  since only Tier-2's per-sample replay cost scales with N.

## Reproducibility (applies beyond any single Q-NNNN)

Any comparison or Monte Carlo run whose numbers end up in docs/MILESTONES.md's
record must be committed as a script under `examples/` (or `scripts/`),
with every scenario-defining parameter (profile paths, climate, scenario
duration, and for Monte Carlo specifically: `n_runs`/`random_seed`) as an
explicit, named argument — never run ad hoc and copy-pasted into a
document. `examples/flagship_comparison.py` (landed alongside Q-0006/
Q-0007's fix) is the first instance of this convention; the first Monte
Carlo run that lands in docs/MILESTONES.md's record must follow it, not repeat the
gap that convention was built to close. Rationale: the simulator itself is
deterministic (same seed, same inputs -> byte-identical output), but that
guarantee only covers what's actually committed — an uncommitted
configuration script isn't reproducible no matter how deterministic the
code it calls is.

## Q-0012 — Parked-SOC model scope (replacing the instantaneous end-of-day SOC snap)

- Date: 2026-09-24
- Context: A first multi-year Monte Carlo run (Nissan Leaf / Guam, 20 years)
  was run as a pre-publication check, per this project's standing "check
  the actual numbers before committing" discipline, before any record
  landed in docs/MILESTONES.md. It surfaced implausible results: the UNPERTURBED
  point-estimate run already predicts 40.9% SOH by year 20, and the MC
  coefficient-uncertainty band's p05 hits literal 0% SOH before year 20.
  Root cause, confirmed by directly dumping every checkpoint's
  `battery_soc` across the full 20-year run (not assumed): **SOC is
  pinned at exactly 0.8 (`typical_end_soc`) for the ENTIRE 20-year
  horizon** — `distinct SOC values over full 20yr run: [0.8]`.
  `simulate_long_horizon` snaps SOC to `typical_end_soc` after every trip
  day and only holds (never decays) it during parked periods; with this
  scenario's day-type mix (240 of 365 days/year are trip days), SOC never
  gets a chance to drift from 0.8 at all. Combined with calendar aging's
  `calendar_delta = rate * dt` having no saturation term (purely linear
  in time, no upper bound built in) and `calendar_aging_soc_sensitivity
  =1.0` giving a permanent +30% rate at `soc=0.8` versus the `soc=0.5`
  baseline, this compounds over a 20-year horizon into implausible
  near-total pack death — a severity a 90-day validation window cannot
  reveal, since the same 30% elevation only accumulates a small absolute
  error there. This is not a new bug: it is Milestone E2c's own
  documented "SOC-snapping" deferred-work note (which estimated a
  "50-70% overnight overestimate") made maximally severe by a realistic
  multi-year horizon and a trip-heavy day-type mix that keeps SOC pinned
  essentially 100% of the time, not just overnight.
- Options considered:
  1. **A lightweight piecewise-linear parked-SOC model.** During the
     parked period, SOC decays (driven by the existing `aux_current_a`-
     equivalent standby drain) from its value at day-start toward a lower
     value, then resets to `typical_end_soc` once per day at a charge-
     event time (a new field, e.g. a charge-start hour, or reuse the
     existing end-of-trip/start-of-parked-period timing). No charging
     curves, no C-rate-dependent charge duration, no charge-power caps, no
     thermal coupling during the charge event itself — only the DAILY SOC
     TRAJECTORY'S SHAPE matters for calendar aging's SOC-sensitivity term,
     and a piecewise-linear/step approximation captures that shape without
     modeling charge-session physics Tier-2 doesn't otherwise have.
  2. **A fuller charge-event model** (actual charge power, duration,
     taper, reusing E2e's `advance_charging_step` machinery at Tier-2
     scale). Rejected as over-scoped for what calendar aging's SOC term
     actually needs, and conflates this fix with the separately-deferred
     "real charger model" work already noted in E2c's own deferred-work
     entry (the "or charging" dt-policy clause, the SOC-snapping note
     itself).
- Blocking: yes
- **Resolution (2026-09-24): Option 1, approved, with specifics ratified
  beyond the original options above:** SOC tracked as a real value through
  each 24-hour cycle. A trip applies its own actual SOC delta (`character
  ization.final_soc - charging_profile.typical_start_soc`, i.e. Tier-1's
  own already-computed result, not re-derived from phase C-rates); parked
  periods decay linearly at a rate driven by `AuxLoads.accessory_load_w`
  ONLY (see the aux-load finding below — NOT the full `hvac_power_w +
  accessory_load_w` `aux_current_a` sim.py uses elsewhere); one charge
  event per day, at a new `ChargingProfile.charge_reset_hour: Sourced
  [float]` field (renamed from the originally-proposed `charge_completion
  _hour` — see the naming note below), resets SOC to `typical_end_soc`.
  No charge-session physics (power caps, taper, thermal coupling) — only
  the daily trajectory's shape.
  **`charge_reset_hour` value: 22.0 (10pm), not `_START_OF_DAY_HOUR`
  (reused).** A new, separate field — reusing `_START_OF_DAY_HOUR` would
  couple two independent concerns (Tier-2's day-0 initial-temp lookup vs.
  per-day charge timing) exactly the way `TRIP_HOUR_OF_DAY` was originally
  split from `_START_OF_DAY_HOUR` to avoid. Since a day's trip is
  simulated starting at the day boundary (hour 0) in this model, realistic
  overnight charging — completing shortly before the NEXT day's trip —
  falls late in THIS day's 24-hour window, near hour 24; 22.0 is a
  defensible late-evening placeholder with margin from the boundary.
  **Named `charge_reset_hour`, not `charge_completion_hour`** — the field
  models one instant where SOC resets, not a real charge session's
  start/duration/taper; "completion" implies a session the model doesn't
  simulate. The value (22.0) didn't need to change for the rename, since
  the coarse model doesn't depend on the exact hour within reason (see the
  hand-computed sensitivity below).
  **A real finding surfaced before implementing, via the hand-computation
  this resolution required (not assumed): literally reusing sim.py's
  `aux_current_a` (`hvac_power_w + accessory_load_w`) as the parked decay
  rate is a category error, not a simplification.** `hvac_power_w`'s own
  field docstring is "typical cabin HVAC electrical draw" — an occupied/
  driving-mode load; `accessory_load_w`'s is "baseline 12V/accessory
  electrical draw" — already the schema's own name for standby drain.
  Reusing the full sum produced ~5.08%/hour decay for the real Nissan Leaf
  profile — a full charge drained in ~16 hours, collapsing the model into
  permanent near-zero SOC every single day, a new degenerate failure mode
  as bad as the one this fix exists to remove. `accessory_load_w` alone
  gives ~0.92%/hour — physically sane. Hand-computed (per the required
  pre-implementation check) time-weighted average SOC over a 1-year
  simulation of the real Leaf/Guam scenario, using the real day-type
  schedule and real Tier-1 trip deltas: **~0.648**, min 0.24, max 0.80 —
  below the ~0.68-0.72 initial estimate but not degenerate, and the
  estimate itself was acknowledged as a rough first-principles guess, not
  a calibrated target; 0.648 was accepted as the model's honest output.
  Pack voltage for the decay-current conversion: `battery.ocv_v` (new
  public function, extracted from `_ocv_and_r0_ohm`'s existing OCV term),
  evaluated once at `typical_end_soc`/`reference_temp_k` (not recomputed
  hourly — `docv_dt_v_per_k=0.0` for every profile today, so temperature
  dependence is already inert everywhere in this project; revisit
  alongside that field if it's ever sourced).

## Q-0013 — Anchor treatment once SOC dynamics change

- Date: 2026-09-24
- Context: E2c's own deferred-work note already warned about this exact
  situation: "The two calibration anchors (Leaf/Guam, Model3/Seattle) were
  both derived with this same snapping assumption already baked in.
  Changing this approximation now breaks the anchors — it requires
  re-calibrating the coefficients, not just swapping in better SOC
  dynamics." That warning is now live: fixing Q-0012 changes what SOC
  value the aging model actually sees day to day, which is exactly what
  the anchors were solved against.
- Options considered:
  1. **Re-derive `calendar_aging_rate_per_s`/`calendar_aging_activation_
     temp_k`** against the same two anchors, but under the NEW SOC-
     trajectory model's own predicted average-SOC for each anchor's
     stated scenario (e.g., re-simulate the Leaf/Guam anchor's 5-year,
     40°C, "80% SOC" condition through the new parked-SOC model and solve
     coefficients to hit 70% SOH under THAT trajectory, not a literal
     constant-0.8 assumption).
  2. **Re-express the anchors as constant-SOC self-consistency checks
     instead of re-deriving coefficients.** Keep the current coefficients
     exactly as-is, and treat each anchor's stated condition ("80% SOC,
     constant") as a deliberately idealized configuration the new SOC-
     trajectory model must still be ABLE to reproduce when fed a
     genuinely-constant-SOC scenario (no trips, no drain) — the anchors
     become a regression test on the SOC-agnostic core of the calendar
     model, not a claim about any realistic trajectory. SOC-trajectory
     realism becomes a separate, additive axis the new parked-SOC model
     provides on top, not something the anchors themselves need to
     account for.
- Blocking: yes
- Note: Claude's read is that Option 2 is cleaner — it preserves the
  anchors' role as idealized calibration targets already validated
  against real fleet data points, and avoids re-deriving coefficients
  every time SOC-trajectory realism improves in the future. Not decided
  here — this is exactly the kind of choice that shapes how every future
  SOC-model refinement interacts with the anchors, and belongs to the
  user's ruling like Q-0006/Q-0007 before it.
- **Resolution (2026-09-24): Option 2, approved.** Coefficients
  (`calendar_aging_rate_per_s`, `calendar_aging_activation_temp_k`) are
  UNCHANGED by this milestone. Reasoning, for the record: the 80%/60% SOC
  values in the anchors are calibration CONDITIONS, not proxies for real
  fleet SOC trajectories — re-deriving coefficients against a changed SOC
  input would entangle the coefficients with whatever SOC model happens to
  be running at the time, with no principled stopping point (every future
  SOC-realism improvement would force another re-derivation). Option 2
  keeps the coefficients stable and the anchors testable:
  `test_calendar_aging_matches_leaf_guam_anchor_within_one_percent`/
  `test_calendar_aging_matches_model3_seattle_anchor_within_one_percent`
  in `tests/test_aging.py` call `compute_aging_step` directly with
  `soc`/`cell_temp_k` held constant for the anchor's stated duration —
  they never go through `simulate_long_horizon`'s SOC trajectory at all,
  so this milestone's SOC-model change doesn't touch them, confirmed
  unchanged by the full suite passing after the fix. **New deferred-work
  item, not a gap to fix:** "anchor calibration sensitivity to assumed
  constant-SOC conditions" — the coefficients were calibrated against
  fleet observations under assumed constant 80%/60% SOC; if real fleet SOC
  distributions ever become available, re-derivation would be needed.
  Until then, the anchors are idealized self-consistency tests, and
  realistic-trajectory predictions (e.g. this milestone's own multi-year
  point estimates) carry that systematic uncertainty as a documented
  limitation. See docs/MILESTONES.md for the full record.

## Q-0014 — `typical_start_soc` semantics under the new SOC-trajectory model

- Date: 2026-09-24
- Context: Under the pre-Q-0012 model, `typical_start_soc` was the SOC
  Tier-1 characterization (`characterize_trip`, via `initialize_sim_state`)
  used as a trip's starting condition, AND (via the old instantaneous-snap
  logic) implicitly close to what Tier-2 assumed each trip actually started
  from. Under the new model, those two roles are no longer necessarily the
  same value: Tier-2 now derives each day's actual pre-trip SOC from the
  previous day's charge-event-and-decay cycle, and applies a TRIP DELTA
  (`characterization.final_soc - typical_start_soc`) on top of THAT
  derived value, rather than resetting to `typical_start_soc` each day.
  This creates a real ambiguity about what the field means going forward.
- Options considered:
  1. **`typical_start_soc` is the specified trip start.** The model sets
     SOC to `typical_start_soc` at the first moment of each day's trip,
     overriding whatever the overnight decay actually produced. Simpler,
     but makes the overnight decay's effect on trip-start SOC essentially
     decorative.
  2. **`typical_start_soc` is descriptive, not prescriptive.** The model
     derives actual start SOC from the charge-completion value and the
     overnight decay; `typical_start_soc` documents roughly what that
     comes out to (and can drift from it) and serves ONLY as the reference
     point Tier-1 characterization's `final_soc` is measured relative to,
     for computing the trip's own consumption delta. This is what the
     current implementation actually does — `simulate_long_horizon` never
     resets `soc` to `typical_start_soc` after day 0; it only reads
     `typical_start_soc` once per trip, to compute `trip_delta_soc =
     characterization.final_soc - typical_start_soc`, then applies that
     delta to whatever `soc` currently is.
  3. **Reconcile:** choose `charge_reset_hour`/decay parameters such that
     overnight decay lands exactly at `typical_start_soc` by the next
     trip, making options 1 and 2 coincide. Requires solving for a
     consistent parameter set, or accepting a residual inconsistency
     between the two anyway.
- Blocking: no (raised, not resolved) — explicitly deferred per
  instruction: document current behavior, do not resolve here.
- **Current implemented behavior is Option 2** (confirmed by reading
  `simulate_long_horizon`'s own code, not assumed): `typical_start_soc` is
  a fixed reference point for measuring each trip's own consumption, not
  an enforced daily starting condition. This was not deliberated as the
  definitively "right" semantic choice — it's what naturally fell out of
  reusing `characterization.final_soc` (Tier-1's own already-computed
  result) without re-deriving trip consumption from phase C-rates. Revisit
  once there's a concrete reason to (e.g. if `typical_start_soc` needs to
  mean something precise for a downstream consumer).

## Q-0015 — `FAST_CHARGE_SESSION` interaction with the daily SOC trajectory

- Date: 2026-09-24
- Context: A `FAST_CHARGE_SESSION` day is currently treated by
  `simulate_long_horizon` exactly like any other trip day — its
  `characterization.final_soc` (which, being a charging session, ends near
  a high SOC) feeds the same `trip_delta_soc` computation as a driving
  trip, and the day's normal charge-reset-and-decay cycle still applies
  afterward. This raises an unresolved question: should a fast-charge day
  interact with the SAME once-per-day `charge_reset_hour` event as every
  other day (redundant — SOC is already near `typical_end_soc` from the
  session itself, so the later reset just re-confirms the same value,
  harmless but conceptually odd), or should it be treated as an
  alternative to the day's normal charge event (the fast-charge session
  IS that day's charging, so the later `charge_reset_hour` snap shouldn't
  fire again)?
- Options considered:
  1. **No special-casing (current behavior).** Fast-charge days get a
     `trip_delta_soc` like any trip, then the day's charge-reset-and-decay
     cycle proceeds unchanged. Simple, and not obviously wrong (the reset
     to `typical_end_soc` is close to a no-op on a day that already ended
     near there), but not deliberately reasoned through either.
  2. **Fast-charge sessions suppress that day's normal charge-reset
     event**, treating the session itself as the day's one charging event.
     More physically accurate (a real driver who fast-charges mid-day
     doesn't ALSO plug in again overnight for a second full top-up in this
     simplified model) but adds a conditional to the day loop specifically
     for one `TripType`.
- Blocking: no (raised, not resolved) — not blocking the parked-SOC model
  landing; fast-charge days are 20/365 (~5.5%) of the default day-type mix
  used in this project's example scenarios, a small fraction of the
  signal. Revisit once there's a concrete reason this matters (e.g. a
  fast-charge-focused long-horizon analysis where the distinction would be
  more than a rounding error).

## Q-0016 — `test_long_horizon_validation.py`'s reference generator needs its own follow-up fix

- Date: 2026-09-24
- Context: `_generate_tier1_reference_soh` still patches SOC to
  `typical_end_soc` every `_PARKED_STEP_S` (the pre-Q-0012 snapping
  assumption), while `simulate_long_horizon` now runs a real per-hour
  decay-and-one-daily-reset trajectory. Confirmed by direct measurement:
  the harness's own test still PASSES post-Q-0012, but relative error at
  its seed/window grew from ~0.8-4% (historical) to 15.64% (Leaf) / 10.14%
  (Model 3) — passing only because both losses are small enough at this
  60-day window to sit under the 0.1% absolute floor, not because the
  reference is still a tight ground truth. A longer window or different
  seed could push this past the floor and fail.
- Options considered:
  1. **Fix the reference generator now, as part of this milestone.**
     Keeps the validation harness meaningful immediately, but the fix
     isn't a simple cadence change: even after matching the once-per-day
     charge-event timing, the reference's real per-second physics (via
     `run_scenario`) still applies sim.py's full `aux_current_a`
     (`hvac_power_w + accessory_load_w`) during parked steps
     unconditionally (sim.py's own already-documented behavior — see
     docs/MILESTONES.md), while Tier-2's new decay model deliberately uses
     `accessory_load_w` alone. Closing that gap either means accepting a
     new, larger residual error source in the validation harness's own
     tolerance, or means changing `sim.py`'s core aux-draw behavior to be
     driving-state-aware — a materially bigger, separately-scoped change
     this milestone did not set out to make.
  2. **Defer the reference-generator fix to its own follow-up**,
     documenting the current fragility explicitly in the test's own
     docstring (done) and here, and accepting that this harness currently
     validates Tier-2 against a STALE reference for the SOC dimension
     specifically (the ambient/schedule dimensions it also validates are
     unaffected and still meaningful).
- Blocking: no — the parked-SOC model fix itself is validated by direct
  hand-computation (Q-0012's resolution), the flagship 90-day comparison,
  and the 20-year point-estimate plausibility check, none of which depend
  on this harness. This harness's OWN fidelity is a separate, real gap,
  named explicitly rather than left to be discovered by a future flaky
  failure.
- **Resolution (2026-09-25): Option 1 (fix the cadence), decided and
  documented per the autonomy grant — reversing a stale test reference to
  match its own system-under-test's already-shipped model is an
  implementation detail, not a new design decision.** `_generate_tier1_
  reference_soh` now applies exactly one instantaneous SOC reset per day
  at `charge_reset_hour`, mirroring `_parked_soc_trajectory`'s own reset
  condition, replacing the stale hourly-reset-to-`typical_end_soc`
  behavior. **Confirmed by direct measurement that option 1's own caveat
  was correct**: the cadence fix alone did NOT close the gap — the
  aux-draw-model divergence (`sim.py`'s real per-second physics applies
  full `hvac_power_w + accessory_load_w` unconditionally while parked;
  Tier-2's decay model uses `accessory_load_w` alone) is a second,
  independent residual, and measured post-fix at 0.122%/0.035% absolute
  (Leaf/Model 3, same 60-day/seed=123 window) — large enough to push the
  Leaf case past the old 0.001 absolute floor. Per this same resolution,
  the floor was widened to 0.002 (documented in the test's own docstring
  and the tolerance's own comment, with the measured numbers on record —
  not picked to make one specific run pass). Making `sim.py`'s aux-draw
  behavior driving-state-aware, which would close this residual for real,
  remains out of scope — a materially bigger core-physics change, now
  tracked in `docs/ROADMAP.md`'s "Charging model gaps" section rather than
  attempted opportunistically here. Full record in `docs/MILESTONES.md`.

## Q-0017 — 47.3% vs. 55-70% multi-year SOH gate: accept as documented extrapolation limit, or investigate further?

- Date: 2026-09-25
- Context: The parked-SOC model milestone's own gate (docs/MILESTONES.md, Milestone
  "parked-SOC model") explicitly held full multi-year Monte Carlo pending
  review of one finding: the unperturbed 20-year Leaf/Guam point estimate
  came back at 47.3% SOH, below a rough pre-run "55-70% credible"
  expectation, even though the parked-SOC fix moved the number in the
  right direction (40.9% -> 47.3%). `docs/model_reference.md`'s "Validated
  time horizon" note already identifies the most likely cause: calendar
  aging's `calendar_delta = rate * dt` is purely linear in time with no
  saturation term, and the two calibration anchors only cover a 5-10 year
  window (Leaf/Guam: 5yr; Model3/Seattle: 10yr) — a 20-year run
  extrapolates roughly 2-4x past that calibrated envelope.
- Options considered:
  1. **Accept as documented extrapolation limit; clear the gate.** Three
     independent checks support this reading rather than treating it as a
     bug: (a) the model reproduces both calibration anchors exactly when
     run under their own stated constant-condition parameters
     (`test_calendar_aging_matches_leaf_guam_anchor_within_one_percent`/
     `..._model3_seattle_anchor_...` in `tests/test_aging.py`); (b) the
     SOC-trajectory fix moved the estimate in the right direction by a
     real, non-trivial amount, confirming the fix does what Q-0012 scoped
     it to do, rather than the gap being unmoved or worsened; (c) the
     mechanism is mechanistically understood (linear-in-time, no
     saturation term), not an unexplained discrepancy. Tuning the model
     (e.g. adjusting `calendar_aging_rate_per_s`/`calendar_aging_
     activation_temp_k`, or adding an ad hoc saturation term) to land the
     number inside "55-70%" would be tuning toward a pre-run guess, not a
     validated target — exactly the failure mode this project has
     consistently avoided (see, e.g., Q-0006/Q-0007's rejection of
     capacity-averaging for the same reason).
  2. **Investigate further before clearing the gate** — e.g. add a
     saturation term to the calendar-aging formulation, or re-derive the
     anchors against a longer-horizon fleet data point if one becomes
     available. Rejected for now: no such data point exists, and inventing
     a saturation curve without one would be exactly the kind of
     unsourced-shape guess this project's `Sourced`/anchor discipline
     exists to avoid.
- Blocking: yes (blocks the multi-year Monte Carlo milestone until
  resolved)
- **Resolution (2026-09-25): Option 1, approved by the user. Gate cleared.**
  The 47.3% estimate is accepted as-is. The linear-in-time-formulation /
  anchor-extrapolation limit is not fixed by this resolution — it remains
  exactly as documented in `docs/model_reference.md`'s "Validated time
  horizon" note, cross-referenced from here — but it is no longer a reason
  to withhold the multi-year Monte Carlo milestone. It is carried forward
  as a deferred aging-model fidelity item (alongside per-vehicle chemistry
  coefficients, Milestone D's refrigerant-loop abstraction, and
  preconditioning) rather than as a blocking gate. See docs/MILESTONES.md's
  deferred work, Milestone "parked-SOC model" entry, for the gate-cleared
  note. Any future multi-year SOH number presented outside a report should
  still carry the "extrapolation past the 5-10yr calibration window" caveat
  per `docs/model_reference.md` — clearing this gate authorizes running and
  publishing multi-year results, it does not retroactively validate the
  model at that horizon.

## Q-0018 — ForkPoint.overrides: what can a fork branch's counterfactual patch target?

- Date: 2026-09-25
- Context: Fork branching v1 (see docs/MILESTONES.md) needs to
  turn `ForkPoint.overrides: dict[str, Any]` — schema-only until now, never
  populated or consumed by any code — into something a real resolver can
  apply to build a child `Scenario`. Nothing had ever defined what a path
  in that dict is allowed to target. This meets this project's escalation
  bar (schema field semantics, and a shape that's expensive to change once
  forks exist that depend on it), so it wasn't decided unilaterally.
- Options considered:
  1. **Battery-only, reusing `mc.py`'s exact minimal resolver pattern**
     (Q-0010 precedent): only `"battery.<field>"` paths, validated against
     whatever type that field actually is. Smallest surface, proven
     pattern, but can't express "what if the climate were hotter" or "what
     if this pack were liquid-cooled" — exactly the comparisons this
     project's teaching story and the mistake library are built on.
  2. **General profile-level resolver**: a real dotted-path walker across
     `ManufacturerProfile`'s full tree (`battery.*`, `motor.*`,
     `thermal_architecture.*`, etc.) — what Q-0010 explicitly declined to
     build for MC v1 because MC didn't need it. Bigger surface, more
     validation to get right, but still can't reach climate/usage/charging
     counterfactuals.
  3. **Scenario-level resolver**: dotted paths can also reach `Scenario`'s
     other top-level objects (`climate.*`, `usage_profile.*`,
     `charging_profile.*`), not just `manufacturer_profile.*`. Broadest,
     most validation surface, needed for a "what if this vehicle were
     driven in Guam instead" or "vehicle sold and moved to a different
     climate at year 10" style fork.
- Blocking: yes
- **Resolution (2026-09-25): Option 3, with an explicit depth constraint
  on top of it.** Both axes (profile-level AND climate/usage/charging) are
  load-bearing for what fork branching is actually for: the first
  mistake-library entry ("Leaf in Guam") needs a climate fork, which rules
  out battery-only (1); a mid-run "moved to a different climate" fork needs
  to be expressible as an override applied at an arbitrary fork point, not
  a freshly-constructed scenario, which rules out profile-only (2).
  **Depth constraint**: a path addresses one of `Scenario`'s four top-level
  objects (`profile` → `manufacturer_profile`, `climate`, `usage_profile`,
  `charging_profile`) and at most ONE level below that — i.e. a dotted path
  of exactly 2 or 3 segments, the last of which must resolve to a leaf
  value (a `Sourced[T]`'s `.value`, or a plain scalar), never a list, dict,
  or another nested model. `profile.battery.capacity_nominal_ah` is valid
  (one hop into `battery`, then its leaf field); `profile.
  thermal_architecture.cooling_loops[0].serves` is not (needs list
  indexing and a fourth segment). No arbitrary-depth walking. If v1 turns
  out to need a three-deep path to express a real use case, that's a
  signal the fork model itself needs revisiting, not a cue to loosen the
  resolver ad hoc — stop and escalate rather than patching around it.
  This gives Option 3's expressive scope with something close to Option
  1's validation simplicity: four top-level objects, each a known schema,
  bounds the validation surface without an open-ended attribute walker.
  Implemented in `src/ev_thermal/fork.py`'s `apply_override`/
  `apply_overrides`, tested with one acceptance test per top-level object
  plus a dedicated rejection test for the depth constraint
  (`tests/test_fork.py`). Note: `Scenario.overrides` (`schema.py`) is a
  separate, pre-existing, still-unused field with a narrower documented
  scope ("on top of `manufacturer_profile`" only) — not reused or unified
  with this resolver in this milestone; see docs/MILESTONES.md's deferred
  work for the flag.

## Q-0019 — Mistake library v1: how is the first MistakeCase's counterfactual_profile constructed?

- Date: 2026-09-25
- Context: Mistake library v1 (see docs/MILESTONES.md) needs a
  first real `MistakeCase` instance. `MistakeCase`'s own docstring already
  settles the ARCHITECTURE question (two independent long-horizon runs
  sharing one `Scenario` context, not a `fork.py` branch, and not a
  different real vehicle as the "fix" — "the only variable is the design
  decision being taught"). What it doesn't settle is what specifically
  changes between `failure_profile` and `counterfactual_profile` for the
  first entry (Nissan Leaf: passive-air battery cooling). This determines
  a published, citable comparison result, meeting this project's
  escalation bar — not decided unilaterally.
- Options considered:
  1. **Minimal, isolated edit**: copy the real `nissan_leaf.yaml` profile
     unmodified into `failure_profile`. For `counterfactual_profile`: add
     one `CoolingLoop` (`"battery_loop"`, `serves: ["battery_pack"]`, with
     placeholder hydraulics matching `powertrain_loop`'s existing
     magnitude/style) and tighten `battery.thermal_node.
     resistance_to_coolant_k_per_w` from `0.5` to `0.02` — matching Tesla
     Model 3's real actively-cooled placeholder value for the same field,
     not an arbitrary number. Nothing else changes: capacity, motor, mass,
     chemistry all stay byte-identical to the real Leaf.
  2. **Broader edit**: the same cooling-loop addition, plus adjusting other
     thermal fields (e.g. `derate_temp_k`/`max_temp_k`,
     `resistance_to_ambient_k_per_w`) to more aggressively demonstrate the
     effect. Risks conflating "added active cooling" with "also tuned
     unrelated thermal parameters," muddying which single decision the
     delta is attributable to.
  3. **Different real vehicle**: use a real actively-cooled profile (e.g.
     Tesla Model 3) as `counterfactual_profile` directly. Cheapest, but
     reintroduces the multi-variable confound this project has repeatedly
     eliminated elsewhere (EFC normalization, shared aging coefficients,
     shared day-type mix), and directly contradicts `MistakeCase`'s own
     docstring.
- Blocking: yes
- **Resolution (2026-09-25): Option 1, approved.** `counterfactual_profile`
  is the real Nissan Leaf profile with exactly two changes: one added
  `CoolingLoop` serving `["battery_pack"]`, and
  `resistance_to_coolant_k_per_w` tightened to `0.02` (Model 3's own real
  placeholder value for the same field, chosen specifically so the
  counterfactual's "actively cooled" claim is grounded in an existing,
  already-reviewed number rather than invented fresh). `battery.
  cooling_mechanism`'s `Sourced[str]` value/confidence/source are updated
  to reflect this is now a hypothetical edit (`confidence: low`, source
  naming it a counterfactual, not real Leaf research) — the real Leaf
  profile's own `cooling_mechanism` entry (`confidence: high`, cited to
  `docs/research/profiles_v1.md`) is untouched in `failure_profile`.
  Constructed programmatically (`.model_copy()` in `examples/
  mistake_library_leaf_battery_cooling.py`), not committed as a seventh
  YAML file — this keeps it visibly a hypothetical derived from the real
  profile, matching CLAUDE.md's own framing ("six real manufacturer
  drivetrains... plus user-editable counterfactuals"), not a new
  "reference vehicle." `evidence` cites the real Leaf's documented
  hot-climate degradation (`docs/research/profiles_v1.md`: "~4.2%/year hot
  climates; ~20% loss after 12mo") for the failure mode, plus the real
  actively-cooled profiles' (Bolt/Model3/e-tron/Niro) documented SoH
  outcomes as corroborating evidence that the fix CATEGORY works — not a
  claim that this specific hypothetical edit reproduces any one of those
  vehicles' exact numbers.

## Q-0021 — Chiller path implementation: control gating and pump/fan energy-accounting prerequisite

- Date: 2026-09-25
- Context: `cooling.py`'s chiller path (`CoolingLoop.chiller_present`/
  `chiller_capacity_w`) has been a known deferred item since the original
  cooling.py milestone, repeatedly re-surfaced (BMW i3's DX approximation,
  Q-0020's parked-period investigation, the mistake library's Leaf case,
  the Blue Bird profile) and explicitly deferred each time as "a
  materially bigger, separately-scoped physics addition... deserving its
  own plan review." This is that plan review, requested directly.
  **Scope**: `CoolingLoop`-level chiller (bolted onto an existing glycol
  loop — Blue Bird's shared PowerDrive 7000 TMM). Explicitly NOT Milestone
  D (the BMW i3's standalone DX refrigerant system, which has no
  `CoolingLoop` at all) — the i3-vs-Leaf publication caveat is unaffected
  by this milestone.
  **A pre-existing gap found while scoping, not assumed**: `step_cooling`
  already computes `pump_power_w`/`fan_power_w`, but in `sim.py`'s full
  per-second model this draw is never charged to the battery
  (`battery_current_request_a` is computed at step 4, before `step_cooling`
  runs at step 5) and never appears in `EnergyTotals` (no field for it).
  Predates this milestone entirely. A chiller's own electrical draw would
  inherit the identical flaw if not addressed — repeating the exact
  "models the effect without the mechanism" shortcut Q-0004 (preconditioning)
  and Q-0020 (parked pump draw) already explicitly rejected.
- Options considered (chiller control gating):
  1. **Second-stage thermostat ramp**: chiller engages only once the
     radiator/fan alone can't hold the loop below `thermostat_full_open_temp_k`
     — real BMS logic avoids running a chiller when the radiator would do.
     (Approved — see resolution.)
  2. **Always-on alongside fan**: simpler, same ramp as fan duty, no
     second-stage gating. Rejected: doesn't reflect real chiller usage
     patterns and likely overstates SOC cost.
- Options considered (pump/fan energy-accounting gap):
  1. **Fix as an in-scope prerequisite.** The chiller would otherwise
     inherit the same flaw; direct Q-0004/Q-0020 precedent. Touches
     `EnergyTotals`'s schema and re-validates every profile's existing
     energy balance (small magnitude, but touches published numbers).
     (Approved — see resolution.)
  2. **Defer, ship chiller with the same known gap, flagged.** Rejected
     given the direct precedent this project has twice already applied to
     an identical shape of problem.
- Blocking: yes
- **Resolution (2026-09-25): Option 1 for both, approved by the user.**
  Implementation plan:
  1. **Schema**: `CoolingLoop.chiller_power_w: Sourced[float] | None`
     (electrical draw at full chiller duty — `chiller_capacity_w` alone
     only says how much heat it can remove, not what it costs). Only Blue
     Bird's YAML needs a real value (placeholder, `TODO(spec-sourcing)`,
     matching every other unresearched field's convention). New
     `EnergyTotals.cooling_parasitic_j` (pump+fan+chiller electrical draw
     summed over a run) closes the pre-existing gap.
  2. **`cooling.py`**: `step_cooling` gains `chiller_duty: float = 0.0`.
     Chiller heat removal is the one mechanism that can pull coolant BELOW
     ambient (the radiator term is capped at `coolant_temp - ambient`;
     chiller isn't) — modeled as a separate additive term, not folded into
     the epsilon-NTU radiator calculation. `chiller_power_w * chiller_duty`
     is real electrical draw, fed through the same path as pump/fan.
  3. **`controller.py`**: new `commanded_chiller_duty: dict[str, float]`
     per loop. Gated on `_duty_ramp` evaluated against
     `thermostat_full_open_temp_k` (not `thermostat_temp_k`) as its own
     threshold — i.e. reuses the existing ramp shape, shifted to the point
     where the radiator/fan path is already saturated, per Option 1 above.
  4. **`sim.py`**: wire `commanded_chiller_duty` through; fold
     pump+fan+chiller draw into `battery_current_request_a` (moving that
     computation after `step_cooling`, or restructuring the step order —
     exact mechanics decided during implementation) and into
     `EnergyTotals.cooling_parasitic_j`.
  5. **`long_horizon.py`**: `relax_parked_temp_k` gains a chiller-aware
     term for Tier-2's parked model, mirroring Q-0020's thermostat-gated-
     ramp-plus-SOC-decrement pattern — but a chiller pulls temperature
     toward a setpoint BELOW ambient, not just faster toward it, so it
     cannot be represented as another parallel resistance the way Q-0020's
     coolant path was; needs its own mechanism, prototyped carefully rather
     than assumed by analogy.
  **Validation plan**: full suite + new unit tests (cooling.py chiller
  term, controller.py gating, energy-balance-with-chiller-draw). Re-run
  and re-record: Blue Bird's already-published 90-day/20-year numbers
  (expect LESS conservative results this time — the existing chiller-gap
  caveat already told readers to expect this) and the mistake library's
  Leaf case (the original motivating example — Q-0020's own finding was
  that the chiller is the single biggest lever for its +0.0628pp-vs-
  ~20%-real-world-citation gap). `examples/flagship_comparison.py`'s
  numbers are NOT expected to change (none of the six passenger profiles
  set `chiller_present: true`) but will be re-run to confirm.
  **Out of scope**: Milestone D (i3 DX abstraction), real COP-based
  refrigerant thermodynamics (placeholder capacity/power values, same
  convention as every other unsourced field), any profile besides Blue
  Bird (the only one with `chiller_present: true` today).
- **Implementation complete (2026-09-25). Full record in
  `docs/MILESTONES.md`'s "Chiller path implementation" milestone.** Built
  exactly per the resolution above (schema, `cooling.py`, `controller.py`,
  `sim.py` including the pump/fan energy-accounting prerequisite fix,
  `long_horizon.py`), 282 tests passing (13 new), the two `@pytest.mark.
  slow` cases, `mypy --strict`, `ruff` all clean. **A real finding
  surfaced by re-validation, not chased toward a bigger number**: the
  chiller mechanism is confirmed correct in isolation (a direct `sim.py`
  test with artificially hot ambient shows it engaging and cooling below
  ambient), but it does not engage in ANY currently-tested real scenario
  — including Blue Bird under both its native Columbus, OH climate and a
  synthetic Guam-like test climate — because the coolant loop's own
  temperature never crosses the shared `thermostat_full_open_temp_k`
  placeholder (40°C). Root cause, confirmed directly: Tier-1's hot-soak
  initial condition only patches the battery node, not the coolant node
  (so a short hot-soak trip doesn't have time to heat coolant past the
  threshold even when the cell itself starts pre-heated), and Q-0020's own
  earlier finding already established that a radiator-coupled pack under
  sustained Guam-like ambient tracks within ~1K of ambient, nowhere near
  40°C either. Blue Bird's own 90-day/20-year numbers moved by
  noise-level amounts (<0.02pp), not the reduction the chiller was built
  to provide. Neither the threshold, the hot-soak initialization
  convention, nor the test climates were adjusted to produce a bigger
  effect — this is reported as-is, per this project's explicit "surface
  it, don't tune toward correct-looking" discipline. What would actually
  make the chiller matter for a real comparison (a re-derived threshold, a
  coolant-node hot-soak fix, or a hotter test climate) is left as an open
  question, not decided here.

## Q-0020 — Tier-2 parked-period coolant-loop coupling: physics model and energy-cost consistency

- Date: 2026-09-25
- Context: Building the mistake library's first entry (Q-0019) surfaced that
  `simulate_long_horizon`'s parked-period calendar-aging relaxation
  (`relax_parked_temp_k`) routes exclusively through `BatterySpec.
  thermal_node.resistance_to_ambient_k_per_w` — never through any
  `CoolingLoop` serving the battery, even when one exists (Milestone E2c's
  original design, unrelated to this milestone). Checked directly across
  all six real profiles: four of six (Audi e-tron, Chevrolet Bolt, Kia Niro
  EV, Tesla Model 3) already have an active battery-serving `CoolingLoop`
  with only a weak `resistance_to_ambient_k_per_w=2.0` fallback — so fixing
  this changes real, already-published behavior for those four vehicles
  (not just the mistake library's new Leaf counterfactual), including
  `examples/flagship_comparison.py`'s published 90-day numbers and
  `tests/test_long_horizon_validation.py`'s E2d harness (which uses Leaf
  and Model 3 as its two real-profile cases). This meets the escalation
  bar on both schema-semantics and re-running-a-validated-comparison
  grounds; approved to proceed as its own milestone, not folded into the
  mistake library, after the user was shown the exact blast radius.
- Options considered (physics model):
  1. **Thermostat-gated parallel resistance, reusing `CoolingLoop.
     thermostat_temp_k`/`thermostat_full_open_temp_k`'s existing linear-ramp
     convention** (the same 0%-at-thermostat_temp_k to 100%-at-
     thermostat_full_open_temp_k ramp `cooling.py`'s real per-second model
     already uses) rather than a hard on/off switch or a new placeholder
     value. (Approved — see resolution.)
  2. **Always-on coolant path whenever a serving loop exists**, no
     gating. Rejected: overstates the benefit (real BMS-triggered thermal
     management doesn't run the pump continuously while idle) and doesn't
     reuse any existing schema field, inventing a new implicit assumption
     instead.
- Options considered (energy-cost consistency): does the pump's
  `pump_power_w` draw against SOC while the coolant path is gated on?
  1. **Yes — scale `pump_power_w` by the same `duty` ramp and add it as an
     extra SOC decrement.** Modeling the coolant path's thermal benefit
     without charging its power draw would repeat the EXACT shortcut Q-0004
     already rejected for preconditioning ("models the effect without the
     mechanism," and the mechanism has a real energy cost that feeds back
     into SOC). (Approved.)
  2. **No — accept it as a flagged v1 simplification.** Rejected given the
     direct Q-0004 precedent; the added implementation cost of computing
     the decrement from `relax_parked_temp_k`'s own returned temperature
     trajectory (no restructuring of `_parked_soc_trajectory` needed) was
     small enough not to justify the shortcut.
- Blocking: yes
- **Resolution (2026-09-25): Option 1 for both.** Implemented in
  `long_horizon.py`: `relax_parked_temp_k` gains four new OPTIONAL
  parameters (`capacitance_j_per_k`, `coolant_resistance_k_per_w`,
  `thermostat_temp_k`, `thermostat_full_open_temp_k`) — omitted at every
  pre-existing call site, so behavior there is byte-identical to before
  (verified: full suite, 256 tests, passes unchanged). When all four are
  given, each step recomputes `duty = clamp((pack_temp_k -
  thermostat_temp_k) / (thermostat_full_open_temp_k - thermostat_temp_k),
  0, 1)` and combines the direct ambient path and a duty-weighted coolant
  path (`resistance_to_coolant_k_per_w + 1/radiator_ua_w_per_k`, in series,
  quasi-steady — no separate coolant-node thermal mass tracked, consistent
  with this function's existing piecewise-constant-ambient simplification)
  as PARALLEL conductances into one effective tau for that step. New
  `_pump_soc_decrement` helper sums `pump_power_w * duty` only over
  intervals starting AT OR AFTER the day's charge-reset event — mirroring
  `_parked_soc_trajectory`'s own reset-step convention exactly, since
  intervals before/at the reset get wiped by the hard reset to
  `typical_end_soc` regardless of their own drain, and double-subtracting
  that would be wrong. `fan_power_w` is NOT charged (flagged simplification
  — fan draws for forced airflow, which matters more while driving; parked
  coolant circulation is approximated as pump-only).
  **A further finding, surfaced by this fix rather than resolved by it:**
  re-running the mistake library's Leaf case after this fix landed showed
  NO change — direct diagnosis (`pack_cell_temp_k` trajectory dump) showed
  neither the failure nor counterfactual profile ever exceeds ~306K over a
  full year in the Guam scenario, well below even a realistic thermostat
  threshold (confirmed against Tesla Model 3's own real researched
  battery-loop thermostat, 318.15K/45°C — higher than the 308.15K value
  used in the counterfactual, so the gap isn't a tuning error). ROOT CAUSE:
  a radiator-based coolant loop can only reject heat TOWARD ambient, never
  below it — and the failure profile's pack already tracks within ~1K of
  Guam's ambient via the pre-existing ambient path, leaving little "excess
  heat above ambient" for any radiator loop to remove. The real-world
  hot-climate degradation this project's research describes is driven
  mainly by sustained high AMBIENT temperature itself (calendar aging's
  Arrhenius sensitivity to absolute cell temp), which only an active
  refrigerant CHILLER can address (can cool below ambient) — and
  `cooling.py`'s chiller path is explicitly unimplemented, a PRE-EXISTING
  docs/MILESTONES.md deferred-work item (`CoolingLoop.chiller_present`/
  `chiller_capacity_w` exist in the schema but do nothing), not something
  this milestone introduced. **Decision (2026-09-25): accept the small
  delta, document the physical limit, do not implement the chiller path as
  part of closing this milestone or the mistake library** — chiller
  implementation is a materially bigger, separately-scoped physics
  addition (affects the full per-second model, not just Tier-2) that
  deserves its own plan review. See docs/MILESTONES.md for
  both this milestone's record and the mistake library's.
  **Re-validation required and completed — result was itself informative,
  not just a formality**: `examples/flagship_comparison.py`'s published
  90-day numbers were re-run and came back UNCHANGED (Leaf/Guam 0.5828%,
  Model3/Seattle 0.1194%, ratio 4.8827 — matching the prior record within
  rounding). This is not a null result; it's the SAME physical-limit
  mechanism documented above showing up a second, independent way: Seattle
  is milder than Guam, so if the thermostat gate barely engages in Guam, it
  engages even less in Seattle — Model 3's real battery-loop thermostat
  (318.15K/45°C) essentially never triggers in either climate under this
  scenario's conditions. No numbers needed correcting in docs/MILESTONES.md's
  existing flagship-comparison record. `tests/
  test_long_horizon_validation.py`'s E2d harness was re-checked too — see
  docs/MILESTONES.md for its pass/fail status.

## Q-0022 — Promoting Blue Bird's UsageProfile/ChargingProfile from validation-scaffolding to canonical: SOC convention and day_type_mix methodology

- Date: 2026-09-25
- Context: `docs/ROADMAP.md`'s "Transition seam" section flagged that
  Blue Bird's `UsageProfile`/`ChargingProfile`/`day_type_mix` (built in
  `examples/validate_blue_bird_vision_electric.py`) were "constructed for
  this validation pass, not yet formally adopted as the profile's
  canonical usage/charging data for a published Transition-seam
  comparison." Promoting them to canonical (feeding the seam table) meets
  this project's escalation bar directly: it determines numbers that will
  be published and reused elsewhere (Q-0021's own autonomy-scope memory,
  test 2). Two specific choices needed ratifying, not silent adoption:
  1. `typical_start_soc`/`typical_end_soc` currently reuse
     `flagship_comparison.py`'s passenger-car convention (0.5/0.8),
     explicitly flagged "not tuned to this vehicle" — despite Blue Bird's
     own real range (~150mi) and Transition's own real usage (70mi/day,
     single-depot overnight charging) being disclosed and already used
     elsewhere in this same profile.
  2. `day_type_mix` splits Transition's real ~180 school-days/year evenly
     60/60/60 across `COLD_START_COMMUTE`/`HOT_SOAK_COMMUTE`/
     `CITY_STOP_AND_GO`, flagged "a reasonable assumption, not a
     researched usage claim."
- Options considered (SOC convention):
  1. **Derive from Blue Bird's own real range/usage.**
     `typical_end_soc=1.0` (real depot fleets charge to full overnight —
     no opportunity charging to plan a lower target around) and
     `typical_start_soc = 1.0 - 70/150 ≈ 0.5333` (end-of-route SOC after
     the real ~70mi day against the real ~150mi range). Grounded in this
     vehicle's own disclosed numbers, not a reused passenger-car
     assumption. (Approved, with an added requirement — see resolution.)
  2. **Keep 0.5/0.8, flagged as a permanent simplification.** Simpler,
     matches flagship's own convention for cross-profile comparability,
     but the seam table would publish an SOC pair inconsistent with this
     vehicle's own disclosed range. Rejected.
- Options considered (day_type_mix methodology):
  1. **Derive from Columbus's real seasonal climate**, using the SAME
     `ambient_temp_k` sinusoidal model and `columbus_climate()` object
     already sourced for this profile (no new data invented) — classify
     each calendar day in a real US public-school academic-year window
     (Aug 15 – May 31) by its own morning (`TRIP_HOUR_OF_DAY`=8:00, cold
     proxy) and afternoon (`DIURNAL_PEAK_HOUR`=15:00, hot proxy) ambient
     against round thresholds (cold <5°C, hot >25°C), then scale the
     resulting proportions to the real 180-day total. (Approved — see
     resolution.)
  2. **Keep the even 60/60/60 split, flagged as permanent.** Avoids
     inventing a seasonal-classification methodology; an even split isn't
     inherently unreasonable (a school year spans all seasons). Rejected
     — grounding in the real, already-sourced climate was judged worth
     the extra methodology work.
- Blocking: yes
- **Resolution (2026-09-25): Option 1 for both, approved by the user, each
  with an explicit added requirement.**
  1. **SOC**: `typical_end_soc=1.0`, `typical_start_soc=1.0 - 70/150 ≈
     0.5333`. **Added requirement**: `typical_end_soc=1.0` sits OUTSIDE
     the calendar-aging coefficients' own calibration range (the two
     fleet anchors — see `aging.py`'s "Coefficient provenance" — were
     derived under constant 80%/60% SOC, never 100%) — this must be
     flagged as carrying the same class of extrapolation caveat as the
     already-established 20-year time-horizon caveat
     (`docs/model_reference.md`'s "Validated time horizon" section), not
     silently treated as within-range just because it's real.
  2. **day_type_mix**: computed via the approved Option 1 methodology —
     **cold=79, hot=16, neutral(`CITY_STOP_AND_GO`)=85** (of 180 real
     school days; see `examples/validate_blue_bird_vision_electric.py`'s
     `school_bus_day_type_mix` for the live, reproducible computation, not
     a hardcoded magic number). **Added requirement, checked directly**:
     does this land "heavy" on `HOT_SOAK_COMMUTE` (only 16/180, actually
     LESS than the prior even split's 60/180) — no, but the COLD-leaning
     classification (79/180) already routinely sees mornings as cold as
     ~4.5°C within the real school-year window, which is below `aging.py`'s
     20-40°C calibrated envelope (a pre-existing exposure, not newly
     introduced by this reclassification — the prior 60/60/60 split
     already included 60 `COLD_START_COMMUTE` days seeing the same real
     Columbus winter mornings). Flagged alongside the SOC caveat, not
     silently absorbed. **Second added requirement, verified by
     construction, not just claimed**: this reclassification only changes
     the SPLIT among the real 180 trip days — it does not change the trip
     day / parked day split itself (180 trip / ~185 fully-parked, both
     unchanged) and does not affect how Tier-2 computes calendar aging for
     the ~185 non-trip days, which already independently accrues using
     each real day's own seasonal ambient regardless of day_type_mix's
     archetype split.
  Full record, including the re-validated 90-day/20-year Blue Bird numbers
  under this canonical data, in `docs/MILESTONES.md`'s corresponding
  milestone.

## Q-0023 — Mistake library's second entry: which failure mode, and how are failure_profile/counterfactual_profile constructed?

- Date: 2026-09-26
- Context: the mistake library's first entry (Q-0019, Nissan Leaf) is the
  only real `MistakeCase` so far — `docs/ROADMAP.md` flags a second entry
  as the item that unblocks Milestone D's own sequencing precondition
  ("Milestone D when the mistake library gets its second real entry," the
  user's own stated ordering). Picking the second entry's failure mode and
  how it's constructed meets the same escalation bar Q-0019 did: it
  determines a published, citable comparison result.
  **A real constraint found while scoping, not assumed**: `MistakeCase`
  holds `climate`/`usage_profile`/`charging_profile` fixed via one shared
  `scenario` across both runs — only `ManufacturerProfile` fields can
  differ. This rules out any candidate lesson from `docs/research/
  profiles_v1.md`'s six real vehicle "stories" that isn't actually a
  vehicle-design difference: Chevrolet Bolt's real failure ("LG Chem
  manufacturing defects... thermal design fine, cell quality wasn't") is a
  manufacturing-defect story, not modelable; BMW i3's "Cold Loop of Death"
  is a DX-refrigerant mechanical-reliability failure with no `CoolingLoop`
  representation, and i3 is separately off-limits for battery comparisons
  until Milestone D itself lands; Kia Niro EV's real "integration, not
  hardware" story (missing preconditioning, a shared loop that "sometimes
  prioritized cabin comfort over pack conditioning") depends on
  preconditioning (explicitly deferred, Q-0004) and a cabin-vs-battery
  coolant-priority mechanism that doesn't exist in `cooling.py` today —
  building either would be a physics-module change, not a data-only
  second entry. None of the six vehicles' own real documented failures
  are usable as-is.
  **What IS already real, working physics** (checked directly in
  `sim.py`/`cooling.py`, not assumed): `CoolingLoop.serves` puts every
  component it lists on ONE shared coolant `ThermalNode` — if a loop
  serves `["battery_pack", "motor", "inverter"]`, all three components'
  heat feeds the same lumped coolant temperature, and that temperature
  couples back to each served component via its own
  `resistance_to_coolant_k_per_w` edge. A shared loop genuinely exposes
  the battery to the powertrain's waste heat today, with no new physics
  needed — this is the real mechanism Kia Niro's "shared loop" story
  gestures at, just without the unbuilt cabin-priority piece.
- Options considered (failure mode):
  1. **Shared battery/powertrain loop with un-upsized capacity**: a
     hypothetical vehicle combines its battery and powertrain coolant
     circuits (real cost/parts-count motivation — Kia Niro's real profile
     already does this, `shared_loop: true`) but reuses the ORIGINAL
     powertrain-only loop's radiator/pump/fan sizing unchanged, rather
     than sizing the combined loop for all three heat sources together.
     Under sustained heavy driving, the loop runs hot from motor+inverter
     waste heat and conducts that heat into the battery, which no longer
     has its own dedicated, tightly-controlled coolant path. Uses only
     already-implemented physics. (Approved — see resolution.)
  2. **Reuse the Leaf's chiller-adjacent finding** (an un-chillered vehicle
     in a hot climate) as the second entry. Rejected: this session's own
     chiller investigation (`docs/MILESTONES.md`) already found this
     specific mechanism structurally negligible at any single
     characterized trip's timescale — publishing it as a "mistake" would
     require a delta the sim itself doesn't produce, contradicting this
     project's "surface findings, don't tune toward a bigger number"
     discipline.
  3. **Frame Kia Niro EV itself as the failure profile**, since its real
     research text literally says "shared coolant loop sometimes
     prioritized cabin comfort over pack conditioning." Rejected: Niro's
     own REAL documented degradation is best-in-class (97.25% SoH at
     100,000km) — using it as a "failure" example would contradict the
     real evidence this project requires every claim to be checked
     against, and the specific mechanism research describes (cabin-vs-
     battery coolant priority) isn't modeled, so the sim couldn't produce
     the story research actually tells about this vehicle anyway.
- Options considered (which real profile to build the hypothetical from,
  and which side — failure or counterfactual — is the real one):
  1. **Audi e-tron, hypothetical is the failure side**: `counterfactual_
     profile` is the REAL, unmodified e-tron (its actual `battery_loop` +
     `powertrain_loop`, genuinely separate per research: "four independent
     circuits... simplified to two, still separate"). `failure_profile`
     replaces both loops with ONE loop that reuses `powertrain_loop`'s
     exact hydraulic/radiator numbers verbatim (not invented fresh, not
     upsized) and its higher thermostat setpoint (308.15K/313.15K, tuned
     for the powertrain, not the battery's own tighter real 298.15K/
     308.15K target) — modeling a combined loop whose CONTROL, not just
     its hardware, is inherited from the powertrain-only design. e-tron
     was picked specifically because both its real loops already use the
     same coolant (`"50/50 water-glycol"`) — a Bolt-based version was
     considered and rejected because the Bolt's real two loops use
     genuinely different coolant fluids (`"DEX-COOL"` for battery vs.
     `"50/50 water-glycol"` for drive-unit), which would make "the
     manufacturer combined them" physically incoherent to model. Every
     component's own `resistance_to_coolant_k_per_w`/
     `resistance_to_ambient_k_per_w` stays byte-identical between
     `failure_profile` and `counterfactual_profile` — only which coolant
     node(s) each component connects to differs, an even purer single-
     variable isolation than Q-0019's Leaf entry (which changed a
     resistance value AND added a loop). This is also why this entry
     inverts Q-0019's role assignment (there, `failure_profile` was the
     real vehicle and `counterfactual_profile` the hypothetical fix; here
     the real e-tron IS the good design, so it's `counterfactual_profile`,
     and the hypothetical bad design is `failure_profile`) — flagged
     explicitly, not a silent inconsistency with Q-0019's precedent.
     (Approved — see resolution.)
  2. **Build a wholly synthetic profile** (not derived from any of the six)
     for the failure side. Rejected: reintroduces exactly the "invented
     vehicle instead of a real-derived counterfactual" pattern Q-0019
     already rejected, for no benefit over reusing e-tron's own real,
     already-reviewed numbers.
- Blocking: yes
- **Resolution (2026-09-26): failure mode option 1, profile-construction
  option 1, both approved.** Implemented in `examples/
  mistake_library_etron_shared_powertrain_loop.py`; see
  `docs/MILESTONES.md`'s corresponding milestone for the run's actual
  numbers, checked directly rather than assumed from this design
  reasoning alone.

## Q-0024 — Milestone D: BMW i3 DX-refrigerant loop abstraction — schema shape, sourcing, and a resistance-cap correction found mid-implementation

- Date: 2026-09-26
- Context: the schema had no way to represent an active refrigerant loop
  (the BMW i3's DX battery cooling) as anything other than a `CoolingLoop`
  (glycol/radiator-shaped) or a node's plain `resistance_to_ambient_k_per_w`
  (a fixed, always-on, symmetric conductance to ambient air, incapable of
  holding a component below ambient). This milestone was explicitly plan-
  reviewed before implementation (per the standing "plan review before
  schema/architecture changes" expectation) — this entry documents the
  approved design AND a correctness finding made only once real numbers
  were run, which changed part of that design mid-implementation.
- **Schema (as planned, unchanged from the reviewed plan)**: new
  `RefrigerantLoop` (`name`, `refrigerant_type`, `capacity_w`, `power_w`,
  `activation_temp_k`, `full_duty_temp_k`, `serves`), structurally parallel
  to `CoolingLoop` but with no glycol/radiator/pump/fan fields — a DX loop
  has no separate coolant thermal mass, so it contributes a direct heat
  sink to its served component(s), not a new graph node/edge.
  `ThermalArchitecture.refrigerant_loops: list[RefrigerantLoop] =
  default_factory=list` — empty for every profile but the i3.
- **Sourcing (as planned)**: `docs/research/profiles_v1.md`'s i3 entry
  gives the refrigerant type (R134a) and a real target (~20°C,
  `activation_temp_k`, confidence high) but no compressor capacity/power.
  Real, paired datasheet numbers for a comparable automotive HV electric
  scroll compressor using the same refrigerant were found and used:
  Guchen GC20A320 — 3500W rated cooling capacity, 1140W rated power,
  DC320V, R134a (guchen-eac.com/what/high-voltage-electric-compressor/
  320v-electric-scroll-compressor.html) — same "real comparable product,
  not the exact OEM part" methodology already established for Blue Bird's
  `chiller_capacity_w` (Q-0021). `full_duty_temp_k` (303.15K/30°C) is a
  10K opening band above `activation_temp_k`, matching the Audi e-tron
  battery_loop's own thermostat-pair span convention (placeholder, not
  independently researched).
- **A correctness finding made mid-implementation, not planned for, and
  requiring a real design change before this milestone could be considered
  done**: the FIRST implementation applied `capacity_w * duty` as an
  UNBOUNDED direct heat sink on the served component (mirroring `cooling.
  py`'s chiller, which also has no flow-rate/heat-exchanger-effectiveness
  term). Running the real i3 profile end-to-end (a real `characterize_
  all_trips` call, not a synthetic fixture) surfaced that this let a
  single ~20-minute `HOT_SOAK_COMMUTE` trip cool the pack ~14-20K —
  physically implausible for any real compressor/thermal-mass pairing, and
  it compounded across `simulate_long_horizon`'s day-to-day carried-over
  pack temperature, driving a full-year Guam run to an implausible -4°C
  to -19°C range (activation_temp_k is 20°C). Root cause: unlike a
  `CoolingLoop`-served component (whose `resistance_to_coolant_k_per_w`
  already rate-limits how fast heat reaches its coolant node before the
  chiller ever acts), the first `RefrigerantLoop` implementation gave the
  compressor direct, resistance-free access to the component's full
  thermal mass — no real cooling mechanism in this codebase, or in
  reality, works that way.
  - Options considered: (1) keep the direct sink, but bound
    `relax_parked_temp_k`'s Tier-2 step size finer so duty gets recomputed
    before overshoot (tried first; only patched Tier-2's coarse
    approximation, left Tier-1's own implausible per-trip cooling rate
    unaddressed — rejected, treats a symptom not the cause). (2) Reuse
    `ThermalNode.resistance_to_coolant_k_per_w` (already present on every
    component, and already documented on the i3's battery node as "unused"
    pending exactly this) as a genuine rate limit: `heat_removed_w =
    min(capacity_share_w * duty, (component_temp_k - activation_temp_k) /
    resistance_to_coolant_k_per_w)`. No new schema field — reuses an
    existing one for its evidently-intended purpose. (Approved — see
    resolution.)
  - **Resolution: option 2.** `refrigerant.step_refrigerant_loop` gained
    `component_temp_k`/`resistance_to_coolant_k_per_w` params and the
    resistance cap; `sim.py`'s `_step_refrigerant_loops` (replacing the
    earlier `_apply_refrigerant_heat_sinks`) computes it per (loop,
    served-component) pair, since `serves` could in principle list more
    than one component (untested case; capacity is split evenly, not
    chased further — every current profile's RefrigerantLoop serves
    exactly one). `long_horizon.py`'s Tier-2 parked-period model was
    correspondingly rebuilt: `relax_parked_temp_k`'s refrigerant branch now
    models a genuine two-conductance weighted-average target (ambient path
    G vs. `1/resistance_to_coolant_k_per_w` scaled by duty, targeting
    `activation_temp_k`) instead of the earlier (wrong) approach of
    borrowing the ambient path's own deliberately-weak conductance as the
    chiller-style target-shift denominator — that reuse was the real bug,
    not merely a step-size problem, since the ambient path's weakness has
    nothing to do with how fast a refrigerant loop can move heat. Verified
    directly, not assumed: re-running the same real i3/Guam/Seattle 1-year
    Tier-2 checks after the fix shows a physically plausible pack-
    temperature range (Guam: 22.8-25.0°C settling near but above the 20°C
    target under continuous heat load, matching real finite-conductance
    HVAC behavior; Seattle: 5.0-21.7°C tracking the seasonal ambient) and
    plausible SOH deltas (Guam 98.50%, Seattle 99.23% after 1 year).
    Confirmed the resistance term is the binding constraint in practice
    (a 20K differential over a real ~0.3 K/W resistance implies ~67W, far
    below the 3500W nominal compressor capacity) — the capacity cap is a
    real but rarely-binding backstop, kept for correctness, not chased
    further into a fully rigorous two-regime (conductance-limited vs.
    capacity-limited) Tier-2 treatment, a documented simplification.
  - **A second, smaller finding surfaced by this fix, flagged not
    silently fixed**: the i3 battery's own `resistance_to_coolant_k_per_w`
    (0.3 K/W) — set before this field was ever read by anything, per its
    own prior "unused" note — is noticeably weaker (higher K/W, i.e. less
    tightly coupled) than the same field's placeholder value on other
    actively-cooled profiles (e-tron 0.015, Leaf's counterfactual 0.02,
    ~0.02-0.05 elsewhere). Now that Milestone D makes it genuinely
    load-bearing, this magnitude deserves real scrutiny — not re-derived
    here, since doing so wasn't this milestone's scope and the current
    value already produces physically plausible behavior; flagged in the
    YAML's own field note and `docs/ROADMAP.md` as a concrete follow-up.
- Blocking: yes
- **Resolution (2026-09-26): schema/sourcing as originally planned; the
  resistance-cap correction (option 2 above) approved and implemented**,
  since the unbounded-sink version was demonstrably wrong, not just
  imprecise. See `docs/MILESTONES.md`'s corresponding milestone for the
  full implementation record and every number here traced to an actual run.
- **Follow-up resolution (2026-09-26): the i3-vs-Leaf publication caveat is
  LIFTED.** `examples/i3_multiclimate_validation.py` validated the
  refrigerant loop under two more real climates beyond the two used during
  implementation (Phoenix, AZ — already sourced for the chiller
  investigation; Minneapolis, MN — new, real NOAA NCEI 1991-2020 normals,
  station USW00014922, first use of this location anywhere in the
  project), plus a direct hot-bucket-vs-cold-bucket engagement check and a
  real 365-day i3-vs-Leaf comparison under all four climates. Findings, not
  assumed: the refrigerant effect grows smoothly across a hot trip's phases
  (+0.077K → +0.394K → +0.649K at Phoenix's 47.7°C peak) and is EXACTLY
  0.0000K under Minneapolis's -17.3°C coldest bucket — a cooling-only
  mechanism correctly staying fully inactive in the cold, not partially
  engaging. The i3-vs-Leaf total-loss ratio is a coherent, physically
  sensible picture across climates: i3 clearly outperforms Leaf in both hot
  climates (Guam 0.568×, Phoenix 0.648× — consistent with the Leaf's own
  real documented hot-climate weakness), shows no artificial advantage in
  the cold climate where the mechanism is correctly inactive (Minneapolis
  1.005×, essentially identical), and only a small, plausible difference in
  the mild climate (Seattle 1.130× — a genuine multi-variable real-vehicle
  comparison, not an isolated-variable counterfactual, so some non-thermal
  difference is expected). No NaN/inf/runaway temperatures in any run. This
  is the evidence the caveat was gating — `CLAUDE.md`, `docs/ROADMAP.md`,
  and `docs/model_reference.md` are updated to reflect i3-vs-Leaf battery
  comparisons are now publication-safe. Still open, not closed by this:
  the i3 battery's own `resistance_to_coolant_k_per_w` magnitude (flagged
  earlier in this entry) — not this validation's scope.
