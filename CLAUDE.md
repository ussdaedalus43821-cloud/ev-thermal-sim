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
