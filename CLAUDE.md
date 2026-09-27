# CLAUDE.md

## Project

Electric Drivetrain Thermal Simulator: a comparative thermal-management teaching
tool. It models six real manufacturer drivetrains (Nissan Leaf, Tesla Model 3,
Chevrolet Bolt, Audi e-tron, BMW i3, Kia Niro EV) plus user-editable
counterfactuals, over 20–30 year vehicle lifetimes, to teach why heat
management decisions compound over a vehicle's life. It supports Monte Carlo
uncertainty propagation, fork-based what-if branching from any simulated
moment, and causal traces that explain *why* every simulated event happened,
not just *what* happened. A separate, later-added commercial-vehicle track
(`configs/profiles_commercial/`) feeds a seam into a sister project,
Transition — see `docs/seam_reconnaissance.md`.

## Architecture rules

- **Physics lives in core, not UI.** All simulation and domain logic belongs
  in `src/ev_thermal/*.py`. `dashboard/` is presentation-only: it reads
  simulation output and renders it, and must never contain physics,
  simulation stepping, or domain calculations.
- **SI units internally.** All internal computation uses SI units (kelvin,
  seconds, joules, watts, kilograms, meters, ohms). Field names carry their
  unit as a suffix (e.g. `capacitance_j_per_k`), and every field's unit is
  additionally documented via its pydantic `Field(description=...)`.
  Convert to display units (°C, kWh, miles) only in `dashboard/`.
- **Every uncertain field is `Sourced`.** Any real-world value that is
  measured, estimated, or sourced from a datasheet/teardown/forum
  (component specs, empirical coefficients, usage assumptions) must be
  wrapped in `Sourced[T]` with a `confidence` and a `source`. Plain physics
  constants (e.g. the specific heat of water, the Stefan–Boltzmann constant)
  are not wrapped — they aren't uncertain, they're definitions.
- **Adaptive time stepping.** The sim loop steps using `DTPolicy`, not a
  fixed dt. Step size must respond to how fast state is changing (e.g.
  thermal transients need finer steps than a long highway cruise).
- **Fork-safe, serializable state.** `SimState` must be fully serializable,
  including RNG state, so a fork can branch from any simulated moment
  without mutating or depending on the parent run's live objects. Never
  hold simulation state that can't round-trip through
  `model_dump_json` / `model_validate_json`.
- **Failure/fix pairing.** A `MistakeCase` is a teaching unit, not a single
  scenario: it always carries a `failure_profile` *and* a
  `counterfactual_profile` run under the same shared `Scenario`, so the
  lesson is the delta between them, not just a bad outcome in isolation.
- **Aging coefficients are shared across a comparison's reference profiles
  by design**, not per-vehicle-chemistry — this isolates thermal
  architecture as the sole independent variable in any SOH comparison.
  Don't give one profile its own aging coefficients without a deliberate
  decision to do so; it would break the causal attribution these
  comparisons exist to demonstrate.
- **Don't invent parameters.** If a value, field, or model isn't specified,
  research the answers or ask before adding it rather than guessing a plausible-sounding number or
  shape.

## Coding standards

- Python 3.11, dependencies and virtualenvs managed with `uv`.
- All domain/schema data is a pydantic v2 `BaseModel` — no bare dataclasses
  or dicts for anything that crosses a module boundary.
- Type-annotate everything; `mypy --strict` must pass.
- Format and lint with `ruff`.
- Every new schema field needs a round-trip serialization test in
  `tests/`; every new physics function needs a unit test against a
  hand-computed or literature value once physics modules exist.
- No comments explaining *what* code does — names should do that. Only
  comment the *why* when it's genuinely non-obvious (a workaround, a
  subtle invariant, a source citation for a magic number).
- No speculative abstractions: build what the current task needs, not what
  a future task might need.

## Design protocol

- Read `docs/DESIGN_QUESTIONS.md` at the start of every session.
- Before non-trivial changes, check whether the task's ambiguities are
  already logged there.
- If a decision is needed and isn't already answered, do NOT decide
  silently — append it to `docs/DESIGN_QUESTIONS.md` as a new `Q-NNNN`
  entry, following the format in that file's header.
- When a question is answered/ratified, fold the resolution into
  `docs/MILESTONES.md`'s log (as already practiced), and remove or mark
  resolved the corresponding entry in `docs/DESIGN_QUESTIONS.md`. Add any
  genuinely still-open follow-up to `docs/ROADMAP.md`.

## Known deferred work

The full milestone-by-milestone history — what was built, decided, and
why, with every hand-traced number and design-question resolution — lives
in `docs/MILESTONES.md`. What's still open (not yet built, scheduled for
later, or left undecided) is tracked in `docs/ROADMAP.md`. This section is
just the handful of gaps load-bearing enough to know before touching
related code:

- **Refrigerant chiller physics ARE implemented and fully investigated
  (Q-0021 plus three follow-ups: hot-soak coolant initialization, a
  real-climate investigation, and real chiller_capacity_w sourcing).**
  `cooling.py`/`controller.py`/`sim.py`/`long_horizon.py` all have real,
  unit-tested chiller behavior, confirmed engaging at full capacity under
  a real, NOAA-sourced hot climate (Phoenix, AZ) with `chiller_capacity_w`
  now sourced from real commercial bus-BTMS product data (10kW, was a
  5000W guess). **All three candidate causes for why it never moved a
  published number are closed, and the conclusion is structural, not
  fixable by more sourcing**: it's a timescale mismatch between the
  pack's thermal mass and a single ~20-minute characterized trip — doubling
  the real capacity barely changed the (still negligible) result. **Tesla
  Model 3's flagship-comparison 20-year number moved materially (14.35% →
  14.84% total loss, ~3.4% relative)** from the hot-soak fix alone, since
  it has an active battery coolant loop — don't cite the old Model3/Seattle
  figure. See `docs/ROADMAP.md`'s "Thermal / cooling physics" section for
  open (threshold re-derivation, hotter test climates).
  **Milestone D (BMW i3's DX-refrigerant abstraction) is done** (`docs/
  DESIGN_QUESTIONS.md` Q-0024): the i3's battery cooling is now a real
  `RefrigerantLoop`, not an approximated `resistance_to_ambient_k_per_w`.
  A resistance-cap correctness bug (an unbounded direct sink let a single
  trip implausibly overcool the pack) was found and fixed mid-
  implementation by reusing `ThermalNode.resistance_to_coolant_k_per_w` as
  a genuine rate limit. **i3-vs-Leaf battery comparisons are now
  publication-safe** — validated under four real climates (Guam, Seattle,
  Phoenix, Minneapolis; `examples/i3_multiclimate_validation.py`): the
  mechanism engages smoothly under real heat, stays exactly inactive under
  real cold, and produces a coherent i3-vs-Leaf picture (i3 clearly better
  in both hot climates, no artificial advantage in the cold one). See
  `docs/ROADMAP.md`'s "Thermal / cooling physics" section for the full
  numbers. **The i3 battery's `resistance_to_coolant_k_per_w` has since
  been re-derived** (0.3 K/W → 0.02 K/W, matching the tight real
  convention four other profiles already established for this field —
  see `docs/MILESTONES.md`'s BMW i3 compressor-failure entry for the
  full record) — this meaningfully moved that mistake-library entry's own
  headline numbers (20yr delta +17.66pp → +21.91pp; don't cite the old
  figure). **Not yet re-verified: whether the multi-climate validation
  numbers cited above (i3-vs-Leaf "publication-safe" claim) still hold
  exactly as stated under the new coefficient** — this fix was scoped to
  the mistake-library entry specifically; the qualitative conclusion
  (i3 better in hot climates, no cold advantage) should if anything
  strengthen given the tighter real coupling, but the exact cited numbers
  haven't been re-run.
- **Preconditioning IS implemented (Q-0026/Q-0027), with a real, honest,
  counter-to-expectation finding, not the Q-0004-deferred placeholder
  anymore.** The route/navigation blocker Q-0004 named turned out to be
  reducible (`FAST_CHARGE_SESSION` was already its own independent
  day-type category with no drive to chain from), so the real mechanism
  — a physically-stepped pre-session heater phase with a real SOC cost
  and a real thermal rise, gated on `ThermalArchitecture.
  battery_preconditioning` × `ChargingProfile.preconditioning_enabled` —
  now lives in `long_horizon._precondition_phase`/
  `characterize_fast_charge_session(preconditioned=...)`, feeding a real
  new `ThermalArchitecture.preconditioning_heater_power_w`/
  `preconditioning_target_temp_k` pair (only Kia Niro EV has both today —
  the i3's prior `battery_preconditioning: true` was found, while
  surveying this, to cite a real but DIFFERENT phenomenon — cold-weather
  wallbox pre-departure warming, not fast-charge arrival preconditioning
  — and was corrected to `false`; its real cold-departure capability
  stays unmodeled). **Superseded finding, don't cite**: this milestone
  originally found preconditioning net-negative for SOH — the heater's
  own SOC/cycling cost outweighing the charge-current benefit it claws
  back — but that held only because the aging model then had no
  cold-charging degradation term for preconditioning to protect against.
  See the next bullet (Q-0028): once that term landed, preconditioning
  flips to a real, substantial net benefit in the same verification
  setup. See `docs/MILESTONES.md`'s corresponding milestones for the full
  numbers and `docs/ROADMAP.md`'s "Fast charging / preconditioning"
  section for what's still a placeholder (heater power/duration point
  values) versus what's real (the mechanism itself, the Niro's target
  temp).
- **Cold-charging lithium-plating degradation IS implemented (Q-0028) —
  real missing physics, added on its own merits, not to fix
  preconditioning's story (that was a consequence, not the reason).**
  `aging.compute_aging_step`'s only prior temperature dependence for
  cycle aging (an Arrhenius term calibrated to SEI-growth-like behavior)
  made aging monotonically WORSE as temperature rises — correct for that
  mechanism, but wrong for lithium plating during charging specifically,
  which gets WORSE as temperature drops. A new, separate, charge-only
  additive term (gated on `applied_current_a < 0` and `cell_temp_k`
  below `BatterySpec.cold_charge_plating_onset_temp_k`) now models this,
  calibrated from real literature — Yang & Wang's peer-reviewed
  physics-based aging model (onset ~293.15K at the 1C reference C-rate)
  and Matadi et al.'s cited experimental rate (a real cell: 75% capacity
  loss in 50 cycles at 5°C) — one citation-hop removed and flagged as
  carrying a known EOL-threshold-mismatch imprecision, not treated as
  exact. **Checked directly, not assumed: no clean fleet-level anchor
  exists for this specifically** — Geotab's own 2026 reports explicitly
  disclaim insufficient cold-only fleet samples to isolate it, and Leaf
  fleet data is dominated by the well-known hot-climate signal instead;
  the user's own premise that documented Leaf cold-climate fleet data
  exists did not hold up under checking. **A load-bearing gap found and
  fixed mid-implementation**: `TripPhaseAging` had never carried signed
  current (only `abs()`), which is the only current information that
  survives into `simulate_long_horizon`'s Tier-2 replay — without fixing
  this, the new term would have been silently inert for every long-
  horizon/flagship number, exactly the numbers this milestone exists to
  change. Fixed via a new `TripPhaseAging.mean_current_a` field. See
  `docs/MILESTONES.md`'s corresponding milestone for the full numbers,
  including the real verification result reconciling this with
  preconditioning's own (superseded) net-negative finding above.
- **Motor mechanical-loss coefficients are placeholders, not a real
  efficiency map** — internally consistent (energy-balance tests hold)
  but not realistic in absolute magnitude yet.
- **Entropic heat is inert** until a real dOCV/dT is sourced for any
  profile — most consequential for fast-charge SOH-loss estimates.
- **The Transition seam has a first real, end-to-end wired path, track-only.**
  The Blue Bird Vision Electric profile is validated (real Tier-1
  characterization + 90-day and 20-year Tier-2 trajectories), a real
  `SeamTableRow`/`SeamTable` exists (`examples/generate_seam_table.py`),
  and `Transition/src/engine.ts` now updates each converted route's
  `batterySohPercent` monthly from that table — but with **no gameplay
  consequence wired to it yet** (no cash/uptime/DSCR effect), by explicit
  scope choice. Tank Wash's truck class still has no seam-table row, and
  `Route` has no direct link to its `VehicleClass` (the wiring assumes one
  vehicle class per fleet). See `docs/ROADMAP.md`'s "Transition seam"
  section and `docs/seam_reconnaissance.md`.
- **Fork branching v2** (Tier-2/long-horizon checkpoint resume) is
  deferred — only full-fidelity per-second forking exists today.
- **Calendar aging's linear-in-time formulation is a known extrapolation
  limit** past its calibration anchors' 5-10 year window — any multi-year
  SOH number should carry `docs/model_reference.md`'s "Validated time
  horizon" caveat.
