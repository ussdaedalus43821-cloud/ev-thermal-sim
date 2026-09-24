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

## Design protocol

- Read `docs/DESIGN_QUESTIONS.md` at the start of every session.
- Before non-trivial changes, check whether the task's ambiguities are
  already logged there.
- If a decision is needed and isn't already answered, do NOT decide
  silently — append it to `docs/DESIGN_QUESTIONS.md` as a new `Q-NNNN`
  entry, following the format in that file's header.
- When a question is answered/ratified, fold the resolution into this
  file's "Known deferred work" log below (as already practiced), and
  remove or mark resolved the corresponding entry in
  `docs/DESIGN_QUESTIONS.md`.

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
  **Confirmed still true as of the spec-integration milestone, where it
  now has a real, visible consequence, not just a theoretical one:**
  `CoolingLoop.serves` is currently ignored by sim.py — all components
  connect to `cooling_loops[0]` regardless of topology. The Nissan Leaf
  profile's `serves: ["motor", "inverter"]` (battery deliberately excluded
  — passive air cooling, no active loop, per docs/research/profiles_v1.md)
  correctly encodes "no active battery cooling" as *data*, but sim.py's
  hardcoded topology will still connect the battery to whatever
  `cooling_loops[0]` is regardless. Leaf's air-cooled pack will not behave
  differently from a liquid-cooled one (e.g. Model 3's) until this is
  fixed — the single biggest thermal-management lesson this project can
  teach is currently not actually simulated correctly. Deliberately not
  fixed in the spec-integration milestone (explicitly out of scope, kept
  narrow on purpose) — topology-aware cooling is its own next milestone,
  with its own plan review, not a quiet extension of this one.
  **Fixing this turned out to depend on a second, separately-tracked gap
  (see the ambient-coupling entry below), so the full topology fix is
  split into three explicitly ordered milestones, none of which should be
  attempted together:**
  - **Milestone A (done):** `ThermalNode.resistance_to_ambient_k_per_w`
    added and wired in `thermal.py`; all six profiles populated. `serves`
    still NOT read by sim.py — this milestone alone does not change
    simulated behavior for any profile, since every node is still also
    connected to the shared `cooling_loops[0]` coolant node exactly as
    before. It exists so Milestone B doesn't have to invent an ambient-loss
    number under time pressure once a component actually becomes isolated.
  - **Milestone B (done):** `CoolingLoop.serves` is now authoritative.
    `sim._combined_thermal_network` builds one coolant `ThermalNode` per
    entry in `cooling_loops` (named via the new public `coolant_node_name`/
    `coolant_node_names` helpers, not one global `COOLANT_NODE_NAME`
    constant, which is removed), and edges each component only to the
    loop(s) whose `serves` actually names it. A component no loop serves
    gets no coolant edge at all — falls back to Milestone A's ambient path,
    or is adiabatic if that's also unset. `step_cooling` is called once per
    loop. `dashboard/app.py`, `examples/first_run.py`, and `test_sim.py`
    were all updated off the single-coolant-node assumption. **Planning
    this milestone surfaced a real, pre-existing bug**, not just a design
    decision: every profile's `CoolingLoop.serves` (and
    `tests/test_cooling.py`'s shared fixture) wrote `"battery"`, but
    `BatterySpec.thermal_node.name` is `"battery_pack"` — a silent string
    mismatch that would have made *every* profile's battery thermally
    isolated once `serves` became authoritative, including the four
    profiles whose batteries are genuinely actively liquid-cooled (Model 3,
    Bolt, e-tron, Niro). Fixed in all six YAMLs and the test fixture before
    landing the code change; `tests/test_profiles.py`'s
    `test_every_cooling_loop_serves_entry_matches_a_real_thermal_node_name`
    guards against this class of bug recurring silently.
    **Known compromise, accepted for this milestone:** `controller.py` is
    still single-loop-scoped — it only ever sees `cooling_loops[0]` and
    produces one `commanded_pump_duty`/`commanded_fan_duty` pair (see
    Milestone C below). `sim.advance_one_step` broadcasts that one pair to
    *every* loop's `step_cooling` call. For multi-loop profiles (Model 3,
    Bolt, e-tron), this means the motor/inverter loop is commanded off the
    same signal as the battery loop, even though a real vehicle controls
    them independently — the motor loop's pump can ramp up because the
    *battery* got hot, or vice versa. Physically wrong in detail, but
    strictly better than the alternative considered (only `cooling_loops[0]`
    gets commanded duty, every other loop sits at duty=0 forever) — that
    would mean e.g. Model 3's motor/inverter loop never pumps at all.
    Acceptable until Milestone C.
  - **Milestone C (done):** `controller.py`'s pump/fan duty computation is
    now genuinely per-loop. `ControllerStepResult.commanded_pump_duty`/
    `commanded_fan_duty` (and `SimStepRecord`/`SimTimeSeries`'s matching
    fields) are `dict[str, float]` keyed by `CoolingLoop.name`, one entry
    per loop, computed from only the components *that* loop's `serves`
    names, against *that* loop's own `thermostat_temp_k` — replacing
    Milestone B's single-broadcast compromise. `step_controller`'s
    `component_temps_k` mapping is built from data it already receives
    (each spec's own `thermal_node.name` paired with its already-passed
    temp float), so no new redundant parameter was needed for that half;
    `coolant_temps_k: dict[str, float]` (new) is keyed by `CoolingLoop.
    name`, not sim.py's internal `coolant_node_name()` convention — the
    controller's interface is loop-level and stays decoupled from how
    sim.py happens to name its coolant nodes internally.
    **A `CoolingLoop` with an empty `serves` is now a fifth `profile_loader.
    load_profile` failure mode (`ValueError`, alongside the four from the
    spec-integration milestone), not a runtime fallback**: an empty-serves
    loop would otherwise silently contribute a coolant node to the network
    with nothing connected to it, or force `step_controller` to guess a
    default duty for a loop that has no real thermal signal. Caught at the
    load boundary instead, so `controller.py`'s per-loop loop trusts
    `loop.serves` is never empty rather than defending against it. New
    behavioral test (`test_two_loop_profile_commands_genuinely_different_
    duty_per_loop` in `tests/test_profiles.py`) proves the actual point on
    a real profile: on the Model 3, a hot battery with a cool motor/
    inverter now commands a high duty on `battery_loop` while
    `motor_oil_loop` stays near its idle floor — something the prior
    broadcast compromise could not produce.
    `SimStepRecord.heat_rejected_w` did NOT need this treatment — it's a
    legitimate sum across loops (real total energy rejected), not a scalar
    standing in for per-loop values, so it stayed a single float, per
    Milestone B's original note.
  - **Milestone D (scheduled but not next):** refrigerant-loop
    abstraction. The schema has no way to represent an active
    refrigerant loop (the BMW i3's DX system, or a battery chiller
    circuit) as anything other than a `CoolingLoop` (glycol-shaped) or
    the node-level ambient path. The i3's profile currently
    approximates DX cooling by giving its battery an unrealistically
    low `resistance_to_ambient_k_per_w` (see that field's own note in
    `bmw_i3.yaml` and `docs/model_reference.md`) — a stand-in, not a
    real model of refrigerant thermodynamics. Do not use i3-vs-Leaf
    battery comparisons for publication until this lands. Deliberately
    deferred behind calendar/cycle aging + long-horizon sim mode
    (Milestone E, next): D closes a known approximation affecting one
    profile's battery-thermal accuracy, whereas aging + long-horizon
    capability is missing *entirely* and is where this project's actual
    teaching goal lives (the whole point is showing how heat management
    decisions compound over a vehicle's life — impossible to demonstrate
    at all without it). Not abandoned, just correctly ordered behind
    the bigger gap.
  - **Milestone E1 (done):** calendar + cycle battery aging, SOH feedback
    into capacity/resistance, and a short-horizon (not yet 20-30-year-
    performant — see E2 below) long-horizon sim mode. New module
    `aging.py`: `compute_aging_step` (calendar aging, Arrhenius-in-
    temperature x linear-in-SOC; cycle aging, Arrhenius-in-temperature x
    power-law-in-C-rate on throughput; combined additively) and
    `apply_soh_to_battery_spec` (returns a capacity/resistance-scaled copy
    of a `BatterySpec` for a given SOH, leaving the raw aging-rate
    coefficients themselves untouched). `sim.advance_one_step` calls
    `apply_soh_to_battery_spec` once per step and feeds the aged copy to
    everywhere that computes a capability ceiling or the actual electrical
    step (controller arbitration, `compute_battery_electrical_step`), while
    `compute_aging_step` itself is always called against the RAW unaged
    spec (aging-rate coefficients don't change with SOH) — see `aging.py`'s
    module docstring, "Coefficient provenance", for exactly how every
    coefficient was calibrated or assumed, and `docs/model_reference.md`'s
    "Aging model confidence and limits" section for the reader-facing
    summary, including the explicit calibrated-vs-extrapolated 20-40C
    envelope.
    **Shared aging coefficients across all six profiles are a deliberate
    experimental-design choice, not an oversight or a shortcut**: this
    project's teaching goal is to show how thermal architecture changes
    battery longevity outcomes, and giving each profile its own chemistry-
    specific aging coefficients would confound that comparison — any SOH
    difference between two profiles could then come from either thermal
    design or chemistry, muddying the exact lesson this simulator exists to
    teach. Holding coefficients constant makes thermal architecture the
    sole independent variable. **Deferred (future E3 or later, not
    scheduled): per-vehicle chemistry aging coefficients — deferred until
    thermal story is demonstrated in isolation. Real LMO (Leaf), NCA (Model
    3/Bolt), NMC (Niro), and Samsung cells (i3) age differently. Until then,
    all six vehicles use identical aging coefficients; vehicle-to-vehicle
    SOH differences are 100% attributable to thermal behavior.**
    Also landed this milestone: `SimState.parked_duration_s` and a fourth
    "extended parking" dt tier in `sim._choose_target_dt_s`, fixing a real
    hysteresis bug in the pre-existing "coolant near ambient" tier — any
    nonzero steady parasitic heat while parked can hold coolant at an
    equilibrium offset from ambient that never closes back inside
    `COOLANT_NEAR_AMBIENT_THRESHOLD_K`, permanently pinning dt at
    `PARKED_WARM_DT_S` for the rest of a long run. The new tier is gated on
    elapsed parked TIME instead (`EXTENDED_PARKING_SETTLING_S`, 3 hours),
    which can't get stuck the same way — it only ever increases while
    parked; `run_scenario` now builds the combined `ThermalNetwork` once
    and reuses it every step instead of rebuilding it (`ThermalNode.
    initial_temp_k` isn't read by `thermal.step_thermal_network`, which
    takes current temps as a separate argument, so the network's structure
    is genuinely static over a run); and `run_scenario` gained
    `record_interval_s` to decimate `RunResult.time_series` for long-
    horizon runs without decimating `final_state` or the energy integrals.
    **Finding surfaced during this milestone, not fixed here:** the
    extended-parking tier's benefit is currently capped by an unrelated,
    already-documented constraint — `_stabilize_dt_s`'s battery R1*C1
    Euler-stability check (see this file's own note on that below) clips
    ANY requested target dt, including the new tier's day-scale one, down
    to whatever's stable against the battery's own RC time constant. Every
    current profile's `r1_ohm`/`c1_farad` are still the shared
    TODO(spec-sourcing) placeholders (tau=45s), so in practice dt is capped
    at roughly 42s regardless of how long the vehicle has been parked —
    measured empirically at ~1,367 steps/sec, meaning a genuinely
    multi-year, mostly-parked run is still on the order of tens of minutes,
    not the deep long-horizon speedup the tier's name might suggest. This
    is why E1's own multi-day determinism test
    (`test_long_horizon_determinism_same_seed_gives_same_final_state` in
    `tests/test_sim.py`) is deliberately shrunk to a few days, not literal
    years, per the milestone's own "keep the wall-clock target loose, don't
    optimize the driving hot path" scope — and it's also a preview of why
    E2 (below) exists at all: a real 20-30-year run needs a fundamentally
    different approach, not just a coarser dt tier.
  - **Milestone E2 (deferred, not yet scheduled or planned in detail):** a
    two-tier simulation architecture proposed as the actual answer to
    long-horizon performance, once it's needed for Monte Carlo (500
    samples) and fork-branching milestones — full-fidelity per-second
    simulation cannot support either at a raw 20-30-year scale (~42
    min/run x 500 samples ~ 15 days, benchmarked during E1's planning).
    Tier 1 runs ~20-30 representative trip profiles (cold-start commute,
    hot-soak commute, fast-charge session, highway, city) at full fidelity;
    Tier 2 extrapolates each trip's aging-rate and thermal/SOC signature
    across the full horizon via day-type weighting, representing thermal
    dynamics as distributions rather than second-by-second traces —
    standard fleet-aging practice. Not designed in detail yet; noted here
    so it isn't lost, not scheduled next.
  `thermal.py` itself needed no changes for Milestones B or C — confirmed
  during Milestone A's planning, and again true after both B and C actually
  landed, that `step_thermal_network`/`node_time_constant_s` already
  operate generically over an arbitrary `ThermalNetwork` graph (no
  hardcoded node count or single-coolant-node assumption anywhere in that
  module); only `sim.py`'s network-building and `controller.py`'s duty
  logic were ever hardcoded to a single loop.
- **Ambient coupling is a node-level property
  (`ThermalNode.resistance_to_ambient_k_per_w`), not a network-level one
  (`ThermalNetwork.ambient_coupling`, now deprecated).** Added in the
  ambient-coupling milestone (Milestone A above), motivated directly by
  that milestone's planning: making `CoolingLoop.serves` authoritative
  (Milestone B) would leave a component no loop serves — the Leaf's and
  BMW i3's batteries, specifically — with *no sink at all* once nothing
  else provided one, i.e. a fully adiabatic node that heats without bound.
  That's a worse, less realistic failure mode than today's wrong-but-
  bounded behavior: real passive-air cooling does lose heat to ambient
  convection, just weakly. Rationale for node-level over network-level:
  the ambient path is a property of the node's own physical environment (a
  pack sitting in air vs. submerged in coolant), not a structural property
  of the network graph; per-node `Sourced` tagging also preserves
  confidence/source per node the way a bare network-level dict couldn't.
  `ThermalNetwork.ambient_coupling` was scaffolded before this was known
  and was never actually populated by any caller — quietly promoting it to
  load-bearing status without this decision would have been a footgun.
  Kept in the schema for now (not removed) to avoid breaking serialized
  `SimState`/fork-replay state; scheduled for removal once fork-replay
  tests confirm nothing depends on it. All six profiles' `resistance_to_
  ambient_k_per_w` values are `confidence: low`, `source: "placeholder —
  estimated from physical plausibility, not sourced"` — actively-cooled
  nodes (motor/inverter always, and battery for Model 3/Bolt/e-tron/Niro)
  use a shared weak-secondary-path placeholder (2.0 K/W) that's negligible
  next to their dominant coolant path; the Leaf's battery (genuinely no
  active cooling at all) targets a soak time constant tau=R*C on the order
  of "a few hours to a day" (0.25 K/W against its 270,000 J/K placeholder
  capacitance = 67,500s ~= 18.75h) — the shape (hours, not seconds) is what
  matters until real convective coefficients are sourced, not the absolute
  R value. **The BMW i3's battery is the one exception, and its value means
  something different: it's an explicit DX-refrigerant-cooling
  approximation (0.02 K/W, ~69min), not a passive-soak number** — added
  during Milestone B's planning when treating the i3 like the Leaf (fully
  ambient-only) was caught as physically wrong: the i3 has real active
  refrigerant cooling (research: "Target ~20C," and 93.77% SoH at 100k km
  proves it works), and modeling it as ambient-only would make it behave
  like an air-cooled Leaf, silently breaking the comparison project's core
  claim for that vehicle. See that field's own `note` in `bmw_i3.yaml` and
  the Milestone D entry above — this is a temporary stand-in the schema
  doesn't yet have a better way to express, not a real refrigerant-loop
  model. Do not use i3-vs-Leaf battery comparisons for publication until
  Milestone D lands.
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
- **The demo profile's motor loss coefficients (`examples/first_run.py` and
  `dashboard/app.py`'s duplicated `build_profile()`, NOT motor.py's own test
  fixture) were recalibrated away from the note above's implausible values,
  specifically because the dashboard milestone surfaced a genuine thermal
  runaway, not just an unrealistic-but-harmless number.** At this profile's
  ~30mph cruise (~365 rad/s), the original `mechanical_loss_coefficient_w_
  per_rad3_per_s3=0.0002`/`iron_loss_coefficient_w_per_rad_per_s=2.0` produced
  ~9.8kW + ~0.7kW of parasitic heat — heat that doesn't depend on applied
  torque at all (both loss terms are speed-only), so it persists even at
  zero commanded torque. Against this profile's ~8kW-at-best radiator, any
  run long enough to reach thermal steady state (the dashboard's 1800s trip
  option, but even a ~530s run at either 0°C or 25°C ambient) drove winding
  temp past `max_temp_k` and pinned it there, since cutting torque to zero
  doesn't reduce the dominant parasitic loss — producing a sustained
  `MOTOR_LIMIT_TRIGGERED` lockout and, because the torque ceiling then
  hunted at/near zero while parasitic heat kept the temp hovering at the
  boundary, a genuine thermal limit-cycle in vehicle speed (not a driver-PI
  gain instability — the PI itself was verified separately to settle
  cleanly and correctly freeze its integral via anti-windup once saturated;
  see `test_driver_pi_settles_within_30s_to_within_1kmh_at_dt_1s` in
  test_sim.py). Recalibrated to `iron_loss_coefficient_w_per_rad_per_s=0.4`/
  `mechanical_loss_coefficient_w_per_rad3_per_s3=3e-6` (~150W each at cruise,
  ~300W total parasitic heat — in line with a real passenger-car traction
  motor's bearing/windage/iron loss at moderate cruise, per real-world
  guidance, not motor.py's own hand-picked 200 rad/s test point), which
  reaches thermal steady state well below `derate_temp_k` at both 0°C and
  25°C ambient over a full 1800s run. Still `TODO(spec-sourcing)` placeholder
  values, not sourced motor data — just recalibrated for plausibility at the
  speed this specific demo profile is actually driven at, the same "a
  constant tuned for one context breaks down in another, so it gets its own
  documented override in the calling context" pattern already used for
  `sim.SIM_DRIVER_KP_NM_PER_MPS`/`SIM_DRIVER_KI_NM_PER_M`. motor.py's own
  test fixture (`tests/test_motor.py`) is unchanged and still uses the
  original 0.0002/2.0 values at its own 200 rad/s test point — this
  recalibration is scoped to the demo profile only, not to motor.py itself.
  **Confirmed fixed, but the retune likely overshot low: this profile's
  motor now reaches steady state around ~30°C at moderate cruise, versus a
  real traction motor's more typical 60-90°C at moderate cruise.** Flagging
  as a tuning note, not a new bug — the underlying issue (parasitic loss too
  large relative to radiator capacity) is fixed, and both the too-high
  original values and the too-low retuned values are `TODO(spec-sourcing)`
  placeholders either way, superseded once real coefficients are sourced.
  Not retuned further for now; if the demo profile's cruise temp needs to
  look more realistic before spec-sourcing lands, split the difference
  between the two documented data points here rather than guessing fresh.
- **Categorical descriptor fields (`chemistry`, `motor_type`, `topology`,
  `coolant_type`) are plain unwrapped `str`, not `Sourced[str]`, even though
  they're empirically-known-from-research facts CLAUDE.md's "every uncertain
  field is `Sourced`" rule would otherwise cover.** Surfaced during the
  profile-population milestone: new categorical fields added in that
  milestone (`BatterySpec.cooling_mechanism`, `MotorSpec.cooling_mechanism`,
  `ThermalArchitecture.heat_pump_type`) WERE wrapped in `Sourced`, per
  explicit instruction for new fields going forward — real confidence
  variance exists here (e.g. the Audi e-tron's cell format is reported as
  "prismatic (later models; pouch at launch)", a genuinely uncertain,
  vintage-dependent categorical fact worth tracking a source/confidence on).
  The four existing plain-`str` fields are inconsistent with this but were
  deliberately NOT retrofitted in that milestone — real churn (every
  existing profile fixture and test constructs these positionally) for no
  immediate payoff. Retrofit them to `Sourced[str]` in a later milestone
  when there's an actual reason to touch every call site anyway, rather
  than as a standalone change.
- **`BatterySpec.min_charge_temp_k` (added in the profile-population
  milestone) is schema-only so far — `battery.py` doesn't read it yet.**
  `available_charge_current_a` and `available_discharge_current_a` both
  still use the single `min_operating_temp_k` as the hard cutoff for BOTH
  directions (today's symmetric behavior, unchanged and still correct for
  every profile that leaves `min_charge_temp_k` unset). The new field exists
  because research surfaced a genuinely asymmetric case (BMW i3: charge
  cuts off at 0°C, discharge continues down to −25°C) that a single shared
  cutoff can't represent — but wiring it into `available_charge_current_a`
  is physics-layer work out of scope for a data/schema milestone. When a
  future milestone wires this in: `available_charge_current_a` should use
  `min_charge_temp_k.value if min_charge_temp_k is not None else
  min_operating_temp_k.value` as its cutoff; `available_discharge_current_a`
  is unaffected and keeps using `min_operating_temp_k` as-is.
- **`ThermalArchitecture.heat_pump_recovers_powertrain_waste_heat` (added in
  the profile-population milestone) is schema-only, descriptive metadata —
  not read by any physics module.** It exists to distinguish a bolt-on
  cabin-only heat pump from an integrated system that reclaims motor/
  inverter waste heat (Tesla's octovalve, Audi e-tron, Kia Niro's heat-pump
  trim) — a real, cited functional difference the research surfaced that
  `heat_pump_present`/`heat_pump_type` alone can't express. Wiring actual
  waste-heat recovery into the combined thermal network (`sim.py`'s
  `_combined_thermal_network`) is a future milestone, not this one.
- **Milestone E2 (long-horizon aging extrapolation, two-tier architecture)
  is in progress, split into explicitly ordered sub-milestones per this
  file's established pattern — none should be attempted out of order.**
  E2's premise: full-fidelity per-second simulation cannot support a real
  20-30-year horizon at Monte Carlo (500 samples) or fork-branching scale
  (E1's own dt-tier work only reached ~42s/step in practice, capped by the
  battery's placeholder R1*C1, not by the tier policy itself — see E1's
  entry below). Tier 1 characterizes a small set of representative trips at
  full fidelity; Tier 2 extrapolates their aging-rate signatures across the
  full horizon via day-type weighting, without re-running per-second physics
  for every occurrence.
  - **Milestone E2a (done):** schema only. Added `TripType` (a closed 5-
    member enum: `COLD_START_COMMUTE`, `HOT_SOAK_COMMUTE`, `HIGHWAY`,
    `CITY_STOP_AND_GO`, `FAST_CHARGE_SESSION` — deliberately excludes a
    "parked" member, since parked time is continuous background state
    between trip occurrences, not a discrete trip archetype), `TripPhaseAging`,
    `TripCharacterization`, `AnnualDayTypeMix`, `UsageProfile.day_type_mix`
    (defaults to empty, every existing profile/fixture unaffected),
    `LongHorizonCheckpoint`, `LongHorizonRunResult`. All new types on the
    aging/long-horizon side are plain (unwrapped) fields, not `Sourced` —
    they're simulation-computed *outputs*, matching this file's existing
    convention for `EnergyTotals`/`SimTimeSeries`/`AgingStepResult`, not
    empirically-uncertain inputs.
  - **Milestone E2b (done):** Tier-1 representative-trip characterization,
    in new module `long_horizon.py`. `ambient_temp_k` (two superposed
    cosines — annual + diurnal — fed only by `mean_annual_temp_k`,
    `seasonal_amplitude_k`, `daily_amplitude_k`; `altitude_m` and
    `solar_irradiance_w_per_m2` are NOT used by it) and
    `ambient_bucket_temps_k` (4 buckets per profile+climate, evenly spaced
    across that climate's own full annual+diurnal range — per-climate-
    derived, not a fixed grid, so a simulated day's actual ambient never
    needs extrapolation on the ambient axis specifically, a separate
    concern from `aging.py`'s own 20-40°C calibrated coefficient envelope).
    `characterize_trip` runs `sim.run_scenario` UNMODIFIED at one constant
    ambient bucket under an archetype-specific piecewise-linear speed trace,
    then compresses the result into per-phase (not whole-trip-averaged, to
    avoid understating aging under Jensen's-inequality-style convexity in
    both the calendar Arrhenius term and the cycle C-rate power law)
    aging-relevant summaries via `_phase_aging` (time-weighted, not
    sample-count-weighted, since adaptive dt makes sample spacing real
    physics, not a decimation artifact). Four archetypes implemented;
    `FAST_CHARGE_SESSION` raises `NotImplementedError` (see its own entry
    below).
    Two new `BatterySpec` fields, `cold_soak_offset_k`/`hot_soak_offset_k`
    (3.0K / 8.0K, both `confidence: low`, `source: "placeholder — to be
    sourced from real-world soak data"`, populated identically across all
    six profiles), set `COLD_START_COMMUTE`/`HOT_SOAK_COMMUTE`'s initial
    battery-node temperature as an offset from the characterization's own
    ambient bucket — deliberately small relative to cabin air's well-
    documented 20K+ solar-soak excess, since the pack is thermally massive
    and at least partly shielded in all six reference vehicles.
    **Deferred work this surfaced:**
    - `ambient_temp_k`'s annual-cosine phase (`ANNUAL_PEAK_DAY_OF_YEAR =
      200`, ~July 19) and diurnal-cosine phase (`DIURNAL_PEAK_HOUR = 15.0`,
      3pm local) are hardcoded Northern-Hemisphere constants — `Climate` has
      no hemisphere/timezone field, and `location_name` is free text (every
      real usage in this codebase is a generic string like `"Dashboard
      Run"`, not geocodable). Southern Hemisphere / inverted-seasonal
      climates are unsupported until `Climate` gains a hemisphere field.
    - `hot_soak_offset_k` is climate-invariant, but physically it should
      scale with `Climate.solar_irradiance_w_per_m2` (a pack in Niger at
      noon vs. Seattle at noon see very different solar loading). Deferred
      until real soak data exists to calibrate a solar-load-scaled version
      against.
    - `TripType.FAST_CHARGE_SESSION` characterization is deferred to
      Milestone E2e. It needs a real charge-mode addition to `sim.py`'s
      step loop (battery absorbing current rather than supplying it),
      which is a sim-loop change deserving its own plan review, not a
      bolt-on to `long_horizon.py`'s characterization runner. Sequenced
      behind E2b-E2d deliberately, not abandoned — fast-charge is central
      to this project's teaching story (it's what makes Leaf-in-Guam bad).
    - The commute trace's `cruise_and_stops` phase ([280s, 1140s])
      compresses steady cruise with three stoplight accel/decel bursts into
      one time-weighted average, understating the peak-C-rate contribution
      to cycle aging (same Jensen's-inequality concern as trip-level
      averaging, one level down). Accepted for E2b since the commute trace
      is low-power and the ramps are gentle, making the effect small at
      today's placeholder aging coefficients. Revisit only if real cycle-
      aging coefficients make it significant — do not sub-split into
      per-stoplight phases pre-emptively, since that would triple this
      archetype's characterization cost for a marginal accuracy win.
    - A climate whose annual mean falls outside the aging model's
      calibrated 20-40°C envelope (e.g. a high-altitude cold climate) has
      its long-horizon run begin its very first days outside that envelope,
      since Tier-2's day-0 pack temp is climate-derived ambient (see
      Milestone E2's own initial-conditions decision, not yet implemented
      as of E2b). Not a blocker — revisit if such a climate becomes a
      primary comparison target.
  - **Milestone E2c-0 (done):** fixed a genuine O(n²) performance bug
    surfaced while scoping E2c's validation harness, found via direct
    benchmarking (not estimation) against the real Nissan Leaf profile: a
    continuous multi-day `run_scenario` call's steps/wall-second throughput
    was measured collapsing from ~15,100 (1 day) to ~2,975 (7 days) before
    this fix, and a 90-day run exceeded a 120s bounded timeout. Root cause:
    `sim.advance_one_step` built `SimState.event_ids` as
    `[*state.event_ids, *new_event_ids]` every step — copying the ENTIRE
    cumulative event-id history into a new list every step, making a full
    run O(steps²). This directly contradicted `CausalEvent`'s own docstring
    ("referenced by ID rather than embedded in SimState, and stored in an
    external append-only log, so that forking a SimState stays cheap even
    across a 20-30 year, many-fork simulation history") — the intended
    design already said not to do this.
    **Fix was a deletion, not new infrastructure**: `SimState.event_ids`
    was removed entirely (not replaced with a bounded tail — that would
    mean inventing an unjustified magic N, and there's no identified use
    case for one). `RunResult.causal_events` already correctly plays the
    "external append-only log" role the docstring describes — it's built
    via `.extend()` every step (O(1) amortized) and is already consumed
    today by `dashboard/app.py`'s `_dedupe_consecutive_events`. Full causal
    history across a fork chain is reconstructed via the ALREADY-EXISTING
    `SimState.fork_lineage` (small, bounded by number of forks taken, not
    number of steps/events) plus each segment's own `RunResult.
    causal_events` — a caller stitches these together across a fork chain
    itself, since they already hold the parent run's `RunResult`. No new
    type, no `run_scenario` signature change. Re-benchmarked post-fix:
    throughput now holds flat at ~17,000-18,000 steps/wall-second from 1
    to 14 days (vs. the pre-fix collapse), confirming genuinely linear
    scaling — a 90-day reference run is now feasible in well under a
    minute, unblocking Milestone E2c's validation harness.
    `LongHorizonRunResult` deliberately carries no causal-event log of its
    own: Tier 2 replays pre-characterized `TripPhaseAging` signatures
    rather than deriving new events via `advance_one_step`, so there is no
    per-day/per-occurrence causal chain to attach — `characterize_trip`'s
    own internal `RunResult.causal_events` describes "what happened during
    the characterization run for this archetype," a different question
    from "why did SOH drop faster in year 7," which a replay-based Tier-2
    design cannot currently answer. Revisit only if a long-horizon causal-
    trace feature is explicitly wanted; not a trivial retrofit.
  - **Milestone E2c (done):** the Tier-2 extrapolation engine
    (`shuffle_day_type_schedule`, `relax_parked_temp_k`,
    `parked_calendar_aging`, `apply_trip_start_offset`,
    `_interpolate_characterization`, `characterize_all_trips`,
    `simulate_long_horizon`, all in `long_horizon.py`). No new schema types
    — everything needed (`LongHorizonCheckpoint`, `LongHorizonRunResult`,
    `TripCharacterization`) already existed from Milestone E2a.
    `characterize_all_trips(profile, scenario_template, n_buckets)` is a
    separate, explicitly-called function (not built internally by
    `simulate_long_horizon`, which requires it as a plain argument) —
    deliberately, so the expensive Tier-1 half (looping `characterize_trip`
    over 4 archetypes x 4 buckets) is cacheable/reusable across many
    `simulate_long_horizon` calls with different `day_type_mix`/`seed`/
    `duration_years`, which matters once Monte Carlo (500 samples) exists:
    without this split, the natural failure mode is re-running all 16
    Tier-1 characterizations inside a sampling loop, exactly the cost
    two-tier exists to avoid.
    Day-type sampling is a deterministic shuffle, not i.i.d. random draws:
    the exact multiset implied by `AnnualDayTypeMix.occurrences_per_year`
    (e.g. exactly 40 highway days if that's the stated count, not ~40 in
    expectation) has its order shuffled via the existing fork-safe
    `numpy.random.default_rng(seed)` convention, RNG stream advancing
    continuously across years (not reseeded per year). Deliberately NOT
    i.i.d. sampling — that would make validation flaky (annual counts
    would only match in expectation) and conflates "does Tier-2
    approximate Tier-1's math" with "did two random usage realizations
    happen to age similarly." i.i.d. sampling is a legitimate future Monte
    Carlo concern, not adopted here.
    **Parked-period pack temperature is NOT a single global closed-form
    solution against the sinusoidal ambient forcing term** (that would
    need an Arrhenius-of-sinusoid integral derived for a placeholder-
    coefficient model) — it's exact per-interval exponential relaxation,
    `T[i+1] = Ta(t_i) + (T[i]-Ta(t_i))*exp(-dt/tau)`, treating ambient as
    piecewise-constant within each `step_s` (default 3600s) interval. Exact
    given that approximation, much simpler to implement/test than the full
    closed form. `tau = battery.thermal_node.resistance_to_ambient_k_per_w
    .value * battery.thermal_node.capacitance_j_per_k.value` — the SAME
    node-level fields the ambient-coupling milestone (Milestone A, earlier
    in this file) added, no new lumped constant. If
    `resistance_to_ambient_k_per_w` is `None` (adiabatic, schema-valid for
    an edited counterfactual), pack temp holds constant rather than
    dividing by zero. Motor/inverter are NOT tracked during parked periods
    — `aging.py` has no motor/inverter aging model to consume their
    trajectory, `LongHorizonCheckpoint` has no field for it, and physically
    they re-equilibrate to near-ambient within minutes (far smaller thermal
    mass than the pack), making multi-hour-to-day tracking moot.
    **Trip-start chaining uses a linear offset correction
    (`apply_trip_start_offset`), not the characterized trip's phases as-is.**
    Every phase's `mean_cell_temp_k`/`peak_cell_temp_k` is shifted by a
    constant `(actual_chained_start_temp - archetype_assumed_start_temp)`
    at replay time, justified by `thermal.py`'s linear (explicit-Euler) RC
    network — a constant initial-condition offset propagates as a constant
    offset through a linear system. Approximate, not exact: R0's Arrhenius
    resistance term and cooling effectiveness both have real temperature
    dependence, so error grows with offset magnitude. A more exact
    alternative exists (characterize each trip type at several fixed
    pack-start offsets, interpolated, the same way ambient is bucketed) but
    multiplies characterization cost 4x — not adopted now; revisit only if
    validation shows offset error dominates.
    Every trip occurrence's ambient lookup uses a fixed
    `TRIP_HOUR_OF_DAY = 8.0` (08:00 local) — morning commute is the modal
    trip time, and a fixed hour keeps validation deterministic. Intra-day
    trip-time distribution is not modeled (a highway trip or a fast-charge
    session at a different hour gets the same 08:00 lookup as a commute).
    Revisit if trip-time-of-day variance is ever wanted. Day-0's initial
    pack temperature separately uses `_START_OF_DAY_HOUR = 12.0` (noon) —
    a better representative of "typical day" pack temp than 08:00 in most
    climates, since it's closer to the daily mean; cleanly separated from
    `TRIP_HOUR_OF_DAY` since the two answer different questions.
    `LongHorizonCheckpoint`s are recorded at DAILY cadence (~11,000 over a
    30-year run, well under 1MB total) — unlike `record_interval_s`
    decimation (which exists because per-second time series over decades
    is genuinely large), memory is not a real constraint at daily
    granularity, so the finest cadence requiring zero extra aggregation
    logic (Tier-2 already advances day-by-day) is also the right one.
    Maximizes fork-point granularity for a long-horizon run. Weekly/monthly
    dashboard views, if wanted later, are a downstream aggregation of the
    daily series, not a different Tier-2 output shape.
    **SOC is topped up instantaneously to `charging_profile.typical_end_soc`
    at the end of every trip day, held constant through the following
    parked period — a real approximation that needs its own deferred-work
    flag, not just a note.** Real behavior is: SOC depletes during the
    drive, sits at the depleted value until charging begins (often hours
    later), then charges back up. Because calendar aging has a strong SOC-
    sensitivity term (`calendar_aging_soc_sensitivity=1.0` means 80% SOC
    ages ~1.5x baseline, 40% ages ~0.9x), treating the overnight parked
    period as always-at-`typical_end_soc` overestimates overnight calendar
    aging by roughly 50-70% in typical usage, since it sees the high end of
    the SOC window rather than the average. **The two calibration anchors
    (Leaf/Guam, Model3/Seattle — see `aging.py`'s "Coefficient provenance")
    were both derived with this same snapping assumption already baked in
    (constant SOC for the anchor's whole duration). Changing this
    approximation now breaks the anchors — it requires re-calibrating the
    coefficients, not just swapping in better SOC dynamics.** Revisit
    alongside a full charger model, together, not separately.
    **Shared test fixture `_make_profile()` (in `test_sim.py`) is
    deliberately adiabatic** (no `resistance_to_ambient_k_per_w` on its
    battery node) — correct for the short-horizon tests it was built for,
    but produces genuine unbounded thermal runaway in any long-horizon
    context (confirmed: 302K -> 2544K over a 5-year `simulate_long_horizon`
    run against it as-is, vs. bounded 289-309K / 11% SOH loss over 5 years
    against the real Nissan Leaf profile, which has ambient coupling). Kept
    as-is rather than changed, since existing short-horizon tests depend on
    its exact behavior and changing it risks silent test drift. Added
    `_make_long_horizon_profile()` in `test_long_horizon.py` (the shared
    fixture plus a placeholder ambient-coupling patch) for any test
    requiring realistic long-horizon behavior — `_make_profile()` itself now
    carries an explicit docstring warning. Do not naively reuse
    `_make_profile()` for long-horizon work.
    Validation harness (`test_long_horizon_validation.py`, marked
    `@pytest.mark.slow`, excluded from the default `pytest` run via
    `pyproject.toml`'s `addopts`): `max(5% relative, 0.1% absolute)` on
    cumulative SOH loss between a Tier-2 run and a continuous full-fidelity
    Tier-1 reference run, same 60-day window and IDENTICAL day-type
    schedule/ambient lookups for both. Initial version (validated only
    against the synthetic test fixture, single once-per-day reference call)
    passed; superseded by Milestone E2d below, which found and fixed two
    real reference-generator fidelity gaps once run against real profiles.
    E2c-0 unblocked this validation existing at all: before that fix, a
    90-day continuous reference run exceeded a 120s bounded timeout, so no
    window affordable pre-fix could have produced a loss above the
    tolerance floor — the validation would have passed trivially regardless
    of whether Tier-2's approximation was any good.
  - **Milestone E2d (done): residual validation gap — real-profile
    coverage.** E2d was originally scoped (before the E2e fast-charge split
    existed) as "validation harness + tests," with the substantive
    requirement stated separately in the original Tier-2 plan: validate
    "for at least two profiles with meaningfully different thermal
    architectures... so the validation isn't accidentally only checking the
    case where ambient-coupling barely matters." E2c's harness delivered
    the harness and tests, but validated only against the synthetic
    `test_sim.py` fixture — never against any of the six real profiles.
    That gap is what E2d closed: `test_tier2_matches_tier1_reference_
    within_tolerance` is now `@pytest.mark.parametrize`d over the real
    Nissan Leaf (no battery coolant loop, ambient-only coupling) and Tesla
    Model 3 (battery in a multi-loop topology) profiles, chosen because
    they bracket the architectural extremes Tier-2 could plausibly be
    sensitive to — a real-profile-specific bug is most likely to surface at
    one of these two ends, not in the four intermediate topologies. Both
    profiles share the same placeholder aging coefficients (deliberately —
    chemistry is a controlled variable, per `aging.py`'s own docstring);
    this test validates topology sensitivity, not a chemistry claim.
    **Running against real profiles surfaced two genuine reference-
    generator fidelity gaps, not Tier-2 bugs** — both caught by direct
    diagnostic investigation before either was accepted, per this project's
    "surface deviations, verify before deciding" discipline:
    1. **SOC top-up was scoped to trip days only, but `aux_current_a` draws
       unconditionally every step regardless of driving state** (HVAC/
       accessories don't stop when parked, and `sim.py` has no charger
       model). A parked day with no top-up drained SOC continuously for the
       full 86400s, hitting `soc_floor` (0.05) on some stretches — confirmed
       by direct inspection. Since calendar aging has a real SOC-sensitivity
       term, this made the reference age slower than intended, for a reason
       unrelated to Tier-2's approximation quality. Fixed by re-patching SOC
       to `typical_end_soc` every `_PARKED_STEP_S` (hourly) interval, not
       just once per day — the within-day decline between patches still
       biased the LIVE calendar-aging integral (computed every step inside
       `advance_one_step`) even though the end-of-day snapshot looked fine.
    2. **Ambient was pinned constant for an entire 86400s day**, reusing
       `characterize_trip`'s own convention (appropriate for a 20-60 minute
       trip, too coarse stretched over 24h). Pinning at `TRIP_HOUR_OF_DAY`
       (08:00) systematically undersampled the daily cycle — 08:00 sits well
       below the daily mean given `DIURNAL_PEAK_HOUR=15.0` — producing a
       stable ~2K reference-vs-Tier-2 temperature bias, worst on the Leaf
       (no coolant path to damp it out) and barely visible on the Model 3
       (active coolant likely dominates over the ambient path). **Tier-2's
       `relax_parked_temp_k` was already doing the more physically correct
       thing** (hourly ambient variation during parked stretches) — the
       reference was the less accurate side of the comparison here, not
       Tier-2. Fixed by sub-stepping the reference's parked portions hourly
       too, matching `relax_parked_temp_k`'s own `step_s` granularity
       exactly (the trip portion itself still uses one constant ambient at
       `TRIP_HOUR_OF_DAY`, where that approximation is legitimately small).
    Diagnostic sequence, in case a similar discrepancy needs re-diagnosing
    later: day-by-day SOH-loss decomposition showed tier2 consistently
    ~1.6-1.7x tier1 on EVERY day including pure parked ones with no trip at
    all — ruling out trip-phase-averaging (which would only show up on trip
    days) and pointing at something present on every day, which is what led
    to the SOC-integration-granularity diagnosis. The ambient fix was
    verified in isolation first (closed the ~2K temp gap to <0.5K, confirmed
    both fixes were independently necessary) before being combined with the
    SOC fix. **Both fixes together produce a near-exact match**: Leaf
    tier1_loss=0.4255%, tier2_loss=0.4221% (diff=0.0034%); Model3
    tier1_loss=0.4344%, tier2_loss=0.4078% (diff=0.0266%) — both comfortably
    inside the 0.1% absolute floor. Runtime: both profiles together, 60 days
    each, ~22s under `pytest -m slow`.
    **The SOC fix independently reproduces this file's own earlier estimate
    almost exactly**: before the hourly-SOC fix (ambient fix alone), the
    reference showed calendar aging running ~60-70% below Tier-2's rate —
    matching the SOC-snapping deferred-work note's own prediction ("over-
    estimates overnight calendar aging by roughly 50-70%") independently, via
    a completely different code path. That's a second, unplanned form of
    cross-validation this milestone produced.
    **The validation harness is sensitive enough to detect a systematic ~2K
    bias and a ~60-70% aging-rate discrepancy over a 60-day window** — proof
    it discriminates real effects, not just that it happens to pass. If it
    ever reports a similar discrepancy in the future, this entry's
    diagnostic sequence (day-by-day decomposition, isolate-then-combine
    fixes, check whether the "reference" or Tier-2 is actually the less
    accurate side before assuming Tier-2 is wrong) is where to start.
  - **Milestone E2e (design decisions ratified via `docs/DESIGN_QUESTIONS.md`
    Q-0001 through Q-0005, not yet implemented):** fast-charge
    characterization, the last piece of Milestone E2's own sequence.
    **Charge-mode step logic (Q-0001): a separate `advance_charging_step`
    function beside `advance_one_step`, plus a parallel
    `characterize_fast_charge_session` beside `characterize_trip`** — NOT a
    mode flag threaded through `advance_one_step`, and NOT a branch inside
    `battery.step_battery`. Approved conditionally: heat-generation physics
    must not diverge between the charge and discharge paths (divergent
    charge/discharge heat models is "the exact failure mode that kills
    comparative sims"). This holds by construction: `advance_charging_step`
    calls `battery.compute_battery_electrical_step` directly — the exact
    same function `advance_one_step` already calls for discharge, which
    already computes `irreversible_heat_w`/`entropic_heat_w` identically
    for both current directions (proven, not new: already exercised today
    via regen braking's negative `battery_current_request_a`) — and the
    same `step_thermal_network` over the same combined network, with
    motor/inverter heat inputs at zero rather than a separate thermal path.
    **Charge power profile (Q-0002) — REFRAMED, the one decision that
    changes what E2e is.** Rejected: constant power + the existing voltage-
    limit expression's natural taper alone. That models the *battery's*
    physics but not the *BMS's* — real DC fast charging above ~80% SOC or
    below a temperature floor is limited by an explicit BMS-enforced
    `I_max(SOC, T_cell)` curve, which IS the thermal-protection mechanism,
    not a detail of it — it's specifically why a passively-cooled pack in a
    hot climate throttles hard and an actively-cooled, preconditioned one
    doesn't. Shipping E2e without it means E2e doesn't unlock the
    comparison it's on the roadmap for. Also rejected regardless: a
    standalone `FastChargeCurve` schema type — a static SOC-only curve is
    temperature-blind, backwards for a thermal simulator.
    **Ratified: constant power request PLUS a sourced `I_max(SOC, T_cell)`
    limit; the taper is implicit in that limit, not a separate mechanism.**
    Proposed shape (full design still to be finalized in the implementation
    plan, not locked here): a new SOC-indexed curve on `BatterySpec` (same
    `list[tuple[Sourced[float], Sourced[float]]]` shape as `ocv_curve_v`/
    `r0_curve_ohm`) giving the BMS's SOC-based fast-charge current ceiling —
    genuinely curved, not `max_charge_c_rate`'s flat cutoff — multiplied by
    the EXISTING temperature-based `_hot_derate_scale` (reusing
    `derate_temp_k`/`max_temp_k`, not inventing a second temperature curve),
    matching this codebase's established pattern of independent
    multiplicative derate factors rather than a true 2D lookup table.
    Scope call: the BMS limit is Milestone E2e itself, not deferred to a
    later E2e-2 — E2e is "the Leaf-in-Guam story," not just charge-mode
    plumbing. **Dependency this surfaces for Q-0001:**
    `advance_charging_step` has a third responsibility beyond "absorb
    current" and "run the thermal solve" — it must also apply this limit;
    the implementation plan must reflect that.
    **New `BatterySpec`/`ChargingProfile` fields (Q-0002/Q-0003):** the
    `I_max(SOC, T_cell)` curve above, plus `ChargingProfile.
    dc_charge_power_kw: Sourced[float]` (named without "fast" — redundant
    given "dc" already implies it), populated as a placeholder across all
    six profiles like every other placeholder-stage field. **Flagged, not
    blocking:** whether `dc_charge_power_kw` belongs on `ChargingProfile`
    at all, or should split between a charger abstraction and a per-vehicle
    max-acceptance-rate field — real effective charge rate is
    `min(charger.max_kw, vehicle.max_acceptance_kw)`, and `ChargingProfile`
    is currently the vehicle-usage-context abstraction, not a standalone
    charger model. Not addressed in E2e; revisit whoever adds a second
    charger type.
    **Preconditioning (Q-0004): deferred entirely. The initial-pack-temp-
    offset placeholder was explicitly considered and rejected, not just
    deprioritized.** That placeholder models preconditioning's *effect*
    without its *mechanism*, and the mechanism has a real energy cost that
    feeds back into SOC and range — exactly what this simulator exists to
    get right. A temp-offset placeholder hides that coupling, and
    downstream code/analyses would start depending on its presence, making
    it expensive to unwind once the real feature (preconditioning as
    thermal load + SOC draw + arrival-temp effect, itself contingent on a
    route/navigation concept this simulator doesn't have) actually lands as
    its own later milestone. If a "what if preconditioned?" answer is
    needed before then (e.g. for the mistake-library entry), it should be a
    one-off analysis script re-running the sim with a warmer initial pack
    temperature — an experiment, not a schema field. Every E2e fast-charge
    characterization is therefore equally pessimistic across all six
    profiles (no preconditioning benefit for anyone) — a defensible worst-
    case baseline, but the Leaf-vs-Model3 fast-charge comparison will not
    show its real differentiator until preconditioning's own milestone
    lands. This is the single biggest remaining gap in the fast-charge
    story, named as such deliberately, not buried.
    **Entropic heat (Q-0005): accepted as inert for E2e, same as
    everywhere else — but flagged with an actual concrete note, not a vague
    caveat.** See `docs/model_reference.md`'s "Aging model confidence and
    limits" section for the full note: ohmic heat scales with `I^2` while
    entropic heat scales linearly with `I`, so entropic heat's relative
    contribution shrinks at the high C-rates fast charging involves — real
    physics, defensible without vehicle-specific data — but the resulting
    SOH-loss bias magnitude is explicitly marked unknown/flagged, not
    estimated, since no real `dOCV/dT` data exists for any profile to
    ground a number.
