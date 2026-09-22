# CLAUDE.md

## Project

Electric Drivetrain Thermal Simulator: a comparative thermal-management teaching
tool. It models six real manufacturer drivetrains (Nissan Leaf, Tesla Model 3,
Chevrolet Bolt, Audi e-tron, BMW i3, Kia Niro EV) plus user-editable
counterfactuals, over 20–30 year vehicle lifetimes, to teach why heat
management decisions compound over a vehicle's life. It supports Monte Carlo
uncertainty propagation, fork-based what-if branching from any simulated
moment, and causal traces that explain *why* every simulated event happened,
not just *what* happened.

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

## Known deferred work

- **Derating migrated to controller.py; component modules kept their own
  copy too (defense in depth, currently redundant).** `motor.py`,
  `inverter.py`, and `battery.py` each expose their derate logic as an
  "available" ceiling callable — `motor.available_torque_nm`,
  `inverter.available_current_a`,
  `battery.available_discharge_current_a`/`available_charge_current_a` —
  which is now the single source of truth for that component's limit.
  `controller.py`'s `step_controller` is the authoritative arbiter: it calls
  all of these, converts inverter/battery current ceilings to a common
  torque currency, takes the most restrictive one, and clamps the driver's
  request to it, logging one `CausalEvent` per component whose ceiling the
  request actually exceeds. Each component's own `step_X` function *also*
  still clamps its own request to its own ceiling internally (unchanged
  behavior, left in place per instruction rather than duplicating the
  callables) — so a request already arbitrated by `controller.py` gets
  clamped again, redundantly but harmlessly, inside `step_motor`/
  `step_inverter`/`step_battery`. Their own internal derate events are no
  longer the authoritative ones; `controller.py`'s are. Don't remove the
  component-level clamping without confirming nothing calls `step_motor`
  etc. directly without going through `controller.py` first.
- **Cross-domain torque/current/power conversion in controller.py is a
  simplification, not a real efficiency-aware model.** It uses
  `power_w = torque_nm * speed_rad_per_s` and `current_a = power_w /
  battery_voltage_v` in both directions, ignoring motor/inverter conversion
  losses entirely — a fully loss-aware version would need the actual
  operating-point efficiency, which is circular at this supervisory layer.
  Below `controller.MIN_SPEED_FOR_TORQUE_CONVERSION_RAD_PER_S`,
  current-based ceilings (inverter, battery) are treated as non-binding
  rather than converted, since the conversion blows up as speed→0; real
  stall-torque limiting needs a separate current-limit table, not modeled
  yet. `controller.py`'s cooling-command policy (`_duty_ramp`, pump/fan duty
  vs. temperature above thermostat) is likewise an explicit
  `TODO(tuning)` placeholder, not a calibrated policy.
- **`driver.py` is settled as the reactive PI controller ("the human
  pedal"), not trip generation.** The file was originally scaffolded for
  drive-cycle/trip *generation* (turning a `UsageProfile`'s distributions
  into a concrete speed-vs-time trace) — that's a different, separate
  concern from what's implemented now: a reactive PI controller that turns
  an already-known `target_speed_mps` into a torque request each step.
  Decision: keep `driver.py` as the PI controller; drive-cycle generation
  gets its own home later — likely `cycle.py` or a function inside
  `sim.py`, not this file. Don't build trip generation now; this note
  exists so the name overlap doesn't cause confusion or get "fixed" into
  the wrong file later.
- **`ambient_temp_k` is inert in motor/inverter/battery steps.**
  `motor.step_motor`, `inverter.step_inverter`, and `battery.step_battery`
  all accept `ambient_temp_k` for interface consistency with
  `thermal.step_thermal_network`, but it currently has no effect: none of
  `MotorSpec`, `InverterSpec`, or `BatterySpec` gives its `ThermalNode` an
  ambient conductance, so only the coolant path is modeled. Add ambient
  coupling for these nodes when they're wired into the full vehicle thermal
  network.
- **Entropic heat is inert until a real dOCV/dT is sourced.** Every
  `BatterySpec` test fixture so far sets `docv_dt_v_per_k=0.0`, which is a
  valid placeholder, not a modeling choice — it's standing in for chemistry
  data we don't have yet. `battery.step_battery` emits an
  `InertEntropicHeatWarning` (once per call site, via Python's default
  warning filter, not tracked state) whenever this is the case. Because
  real entropic heat is largest during fast charging, any run using a
  zero-dOCV/dT spec will under-predict thermal load specifically in that
  scenario. Source real dOCV/dT curves in the spec-sourcing milestone.
- **Cold-power derate deliberately applies to charge only, not discharge.**
  Two distinct cold-temperature effects exist in `battery.py`: R0's
  Arrhenius rise (raises resistance for both directions, shrinking the
  voltage-limited current) and an explicit `cold_derate_threshold_temp_k`
  multiplier modeling BMS-imposed cold-charge restriction (a real,
  charge-specific lithium-plating protection). The explicit multiplier is
  applied to charge only; applying it to discharge too would double-count
  the same underlying cold effect and produce an unrealistically severe
  combined derate (checked numerically: at -10°C in a voltage-limited
  scenario, stacking both would cut the allowed current to ~20% of the
  25°C value, vs. ~26% from R0 alone). Don't restore it to discharge
  without re-deriving the combined magnitude against real data.
- **Chiller path is not implemented in cooling.py.** `CoolingLoop` already
  has `chiller_present`/`chiller_capacity_w` fields, but `cooling.py` only
  models the passive radiator/thermostat/pump/fan path — a loop with
  `chiller_present=True` currently gets no extra heat rejection from it.
  Add active chiller heat removal (presumably gated by some control signal,
  once controller.py exists) as its own increment, not folded into the
  radiator's epsilon-NTU calculation.
- **The coolant loop is now closed, by sim.py — via a real coolant node, not
  cooling.py's own boundary-temp mechanism.** `motor.py`, `inverter.py`, and
  `battery.py` still each independently call `thermal.step_thermal_network`
  on an isolated single-node network with an externally-supplied
  `coolant_temp_k` boundary — that's unchanged, and is what
  `motor.compute_motor_heat` / `inverter.compute_inverter_heat` /
  `battery.compute_battery_electrical_step` exist to let sim.py bypass.
  sim.py instead builds ONE `ThermalNetwork` per step spanning motor+
  inverter+battery+a real coolant `ThermalNode` (capacitance derived from
  the loop's volume/density/specific-heat), connected by edges using
  `1/resistance_to_coolant_k_per_w` as conductance, with the radiator's
  `heat_rejected_w` applied as a sink on the coolant node — see
  `sim._combined_thermal_network` and `sim.py`'s module docstring for the
  full reasoning. `coolant_flow_l_per_min=0.0` is passed to that one
  combined call specifically to disable the per-node boundary mechanism, so
  heat only flows through the explicit edges (no double-counting between
  the two mechanisms).
- **Single-cooling-loop, single-motor simplification in sim.py.**
  `ManufacturerProfile.thermal_architecture.thermal_network` and
  `CoolingLoop.serves` are both ignored: sim.py hardcodes
  `profile.thermal_architecture.cooling_loops[0]` as THE loop connected to
  ALL three components, and `profile.motors[0]` as THE motor, rather than
  reading the schema's own (more general) topology description. Multi-loop
  (e.g. separate battery vs. powertrain loops) and dual-motor orchestration
  are deferred; when built, they replace this hardcoding, not extend it.
- **`available_torque_nm`/`available_current_a`/`available_discharge_current_a`
  converted to a common "torque currency" in controller.py ignores
  motor/inverter conversion losses — and so does the resulting
  `battery_current_demand_a`, which is NOT what should drive the actual
  battery step.** This was already noted for the arbitration ceilings when
  controller.py was built; sim.py's own integration surfaced the sharper
  version of it: using `battery_current_demand_a` (mechanical-power-only)
  as the actual `current_request_a` for
  `battery.compute_battery_electrical_step` would make motor/inverter
  losses appear from nowhere in the energy balance. sim.py instead derives
  the real battery current request from motor/inverter's actual computed
  electrical draw once it's known (`(motor_electrical_power_w +
  inverter_total_loss_w) / battery_voltage_v`, plus aux) — this is why
  battery.py's step comes after motor/inverter's in the sequence, not just
  because the spec listed it that way. Confirmed by the energy-balance test:
  battery's gross chemical output (OCV*I) balances against motor mechanical
  work + all three components' losses + aux draw to within ~0.5%.
- **`_stabilize_dt_s` must check the battery's RC-pair time constant too,
  separately from the thermal network's node time constants.** Discovered
  via integration testing: at a coarse (parked-tier) dt, the combined
  thermal network was stable but `battery.compute_battery_electrical_step`
  still raised `BatteryStabilityError`, because R1*C1 is a completely
  unrelated Euler-stability requirement enforced inside that function, not
  part of `ThermalNetwork` at all. sim.py's dt selection halves against
  `min(thermal node time constants, battery R1*C1)` together.
- **The adaptive dt tier check uses `max(current_speed, target_speed)`, not
  current speed alone.** Also discovered via integration testing: at t=0
  (stationary, target speed above the driving threshold), current-speed-only
  classification picked the coarse "parked" dt tier for the very first
  step — while the driver was about to command near-maximum torque — and
  vehicle-speed integration diverged wildly (reached 130+ m/s in one step)
  before the loop got a chance to react. This is a vehicle-dynamics accuracy
  problem, not a thermal-stability one, so `_stabilize_dt_s` wouldn't have
  caught it either.
- **sim.py passes its own PI gains to driver.py, not driver.py's defaults.**
  `driver.DEFAULT_KP_NM_PER_MPS`/`DEFAULT_KI_NM_PER_M` (800/40) are tuned
  against driver.py's own tests, which use dt=0.1s. At sim.py's dt=1.0s
  driving step those gains are unstable (a linearized loop-gain check comes
  out to roughly 5-6x the stability boundary for a typical fixture vehicle),
  confirmed empirically: the vehicle oscillated between the torque ceiling
  and its negative every step instead of converging. sim.py passes its own
  `SIM_DRIVER_KP_NM_PER_MPS`/`SIM_DRIVER_KI_NM_PER_M` (60/5) explicitly
  rather than changing driver.py's defaults, since those defaults are
  validated against a different dt regime and driver.py's own tests
  shouldn't be tied to sim.py's integration needs. **Driver PI gains are
  dt-dependent, full stop** — the loop-gain scales with dt (see
  `sim.SIM_DRIVER_KP_NM_PER_MPS`'s docstring for the formula), so any future
  controller reacting to speed error at a different step size (a real
  ADAS/autopilot speed-hold controller, a different adaptive-dt regime,
  etc.) needs its own gains re-derived or re-validated for its own dt, not
  a copy of either driver.py's or sim.py's current numbers.
- **The dt policy's "or charging" clause isn't wired up.** The tiered
  adaptive-dt policy given for this milestone was "driving OR charging: fine
  dt" — sim.py only checks vehicle speed, not battery charge direction,
  because there's no external-charger model yet to make "parked but
  charging" a real, distinguishable state this milestone. Wire this in once
  a charger model exists.
- **`CausalEvent.event_id` and `SimState.state_id` are deterministic, not
  random uuids.** Both were `uuid.uuid4()`-based before this milestone
  (`causal.make_causal_event`, and sim.py's own state construction). That
  broke sim.py's core determinism requirement (same seed + same inputs ->
  byte-identical output, including these ID fields) on every single run, so
  both were changed: `event_id` is now a sha256 hash of the event's own
  content (sim_time_s, event_type, affected_component, description), and
  `state_id` is `f"{scenario_id}-step-{step_count:06d}"`. Don't revert
  either to a random scheme without re-solving determinism some other way.
- **Motor mechanical-loss coefficient produces implausible absolute losses
  at realistic driveline speeds; replace with a real efficiency map in the
  spec-sourcing milestone.** motor.py's placeholder
  `mechanical_loss_coefficient_w_per_rad3_per_s3` was tuned (in the motor.py
  milestone) to give a plausible 85-96% efficiency at a hand-picked 200
  rad/s test operating point — but it was never validated against the speed
  a real driveline actually produces. sim.py's integration surfaced that
  gap: a 20 m/s cruise puts the shared test fixture's motor at roughly 545
  rad/s, where the same cubic-in-speed coefficient produces on the order of
  30+ kW of mechanical loss alone — implausibly large for a modest-power
  cruise. This is a spec-sourcing problem for motor.py, revealed by
  integration, not a sim.py bug: it doesn't break anything (no NaN/Inf), and
  sim.py's own tests check internal consistency, not absolute realism.
  **This is exactly why sim.py's energy-balance test asserts a relative
  error threshold (battery output vs. mechanical work + losses, within 2%),
  not an absolute-magnitude one** — the balance holds regardless of whether
  the loss coefficients are realistic, so the test stays valid before and
  after motor.py's placeholders are replaced with a real efficiency map.
  Don't rely on absolute loss/temperature magnitudes from a sim.py run
  until then.
