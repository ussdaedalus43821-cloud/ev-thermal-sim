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
  ask before adding it rather than guessing a plausible-sounding number or
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

- **Refrigerant chiller physics ARE implemented (Q-0021, plus two
  follow-ups: hot-soak coolant initialization, and a real-climate
  investigation).** `cooling.py`/`controller.py`/`sim.py`/`long_horizon.py`
  all have real, unit-tested chiller behavior — confirmed engaging at FULL
  capacity under a real, NOAA-sourced hot climate (Phoenix, AZ). The
  remaining blocker is now specific: `chiller_capacity_w` (5000W
  placeholder, already flagged as likely undersized) is too small relative
  to the pack's own thermal mass to matter within a single trip, not an
  engagement-timing problem. **Tesla Model 3's flagship-comparison 20-year
  number moved materially (14.35% → 14.84% total loss, ~3.4% relative)**
  from the hot-soak fix, since it has an active battery coolant loop —
  don't cite the old Model3/Seattle figure. See
  `docs/ROADMAP.md`'s "Thermal / cooling physics" section for what's still
  open (threshold re-derivation, hotter test climates).
  Milestone D (BMW i3's standalone DX-refrigerant abstraction) is
  unaffected and separate — still don't use i3-vs-Leaf battery comparisons
  for publication.
- **Preconditioning is deferred entirely, not approximated** (`docs/
  DESIGN_QUESTIONS.md` Q-0004) — every fast-charge characterization is
  equally pessimistic across all profiles; this is the single biggest
  remaining gap in the fast-charge story. See `docs/ROADMAP.md`'s
  "Fast charging / preconditioning" section.
- **Motor mechanical-loss coefficients are placeholders, not a real
  efficiency map** — internally consistent (energy-balance tests hold)
  but not realistic in absolute magnitude yet.
- **Entropic heat is inert** until a real dOCV/dT is sourced for any
  profile — most consequential for fast-charge SOH-loss estimates.
- **The Transition seam is partially built**: the first commercial-vehicle
  profile exists (`configs/profiles_commercial/`) and has now been
  validated the way the six passenger profiles were (real Tier-1
  characterization + a 90-day Tier-2 trajectory, both physically
  plausible), but Transition-side wiring doesn't exist yet. See
  `docs/ROADMAP.md`'s "Transition seam" section and
  `docs/seam_reconnaissance.md`.
- **Fork branching v2** (Tier-2/long-horizon checkpoint resume) is
  deferred — only full-fidelity per-second forking exists today.
- **Calendar aging's linear-in-time formulation is a known extrapolation
  limit** past its calibration anchors' 5-10 year window — any multi-year
  SOH number should carry `docs/model_reference.md`'s "Validated time
  horizon" caveat.
