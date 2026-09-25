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

## Q-0008 — Monte Carlo v1 scope: which parameters get sampled?

- Date: 2026-09-24
- Context: `MCConfig`/`Confidence` are schema-only; `mc.py` is a stub. Full
  raw-physics Monte Carlo at N=500 over a 20-30 year horizon is intractable
  (CLAUDE.md's own stated motivation for the Tier-1/Tier-2 split). Tier-1
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

Any comparison or Monte Carlo run whose numbers end up in CLAUDE.md's
record must be committed as a script under `examples/` (or `scripts/`),
with every scenario-defining parameter (profile paths, climate, scenario
duration, and for Monte Carlo specifically: `n_runs`/`random_seed`) as an
explicit, named argument — never run ad hoc and copy-pasted into a
document. `examples/flagship_comparison.py` (landed alongside Q-0006/
Q-0007's fix) is the first instance of this convention; the first Monte
Carlo run that lands in CLAUDE.md's record must follow it, not repeat the
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
  landed in CLAUDE.md. It surfaced implausible results: the UNPERTURBED
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
  limitation. See CLAUDE.md's Known deferred work for the full record.

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
     CLAUDE.md), while Tier-2's new decay model deliberately uses
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
