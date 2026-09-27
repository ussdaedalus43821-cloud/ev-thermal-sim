# Milestone History

Full milestone-by-milestone historical decision log — what was built,
decided, and why, in chronological order. Moved out of `CLAUDE.md` to
keep that file within its own 1-2 page limit; `CLAUDE.md` now carries only
the current, durable rules plus a short, curated list of the most
load-bearing open gaps. This file is the append-only record those rules
and gaps trace back to — the design protocol's "fold the resolution into
the known-deferred-work log" instruction now means folding it in here,
not into `CLAUDE.md` directly.

For what's still open (not yet built, scheduled for later, or left
undecided), see `docs/ROADMAP.md` instead — that file extracts the
forward-looking items from this log so they don't require reading the
whole history to find.

---

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
  - **Milestone E2e (done — design decisions ratified via `docs/
    DESIGN_QUESTIONS.md` Q-0001 through Q-0005, then implemented across
    four commits: schema, `battery.available_charge_current_a`'s BMS-curve
    extension, `sim.advance_charging_step`/`run_charging_session`, and
    `long_horizon.characterize_fast_charge_session`):** fast-charge
    characterization, the last piece of Milestone E2's own sequence.
    Milestone E2 (E2a through E2e) is now fully closed.
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
    **Implementation plan (ratified, not yet coded), with four review flags
    resolved before implementation, per the design protocol:**
    - **`battery.available_charge_current_a` gains a new ceiling term,
      integrated into its existing `min(...)`** — not a new clamp applied
      elsewhere. New `BatterySpec.charge_current_limit_curve:
      list[tuple[Sourced[float], Sourced[float]]]` (SOC -> a 0-1 scale
      factor on `max_charge_c_rate`, same shape/interpolation convention as
      `ocv_curve_v`/`r0_curve_ohm`, reusing the existing `_interpolate`
      helper): `soc_charge_limit_a = _interpolate(spec.charge_current_
      limit_curve, soc) * c_rate_limit_a`, then **`return min(voltage_
      limit_a, soc_charge_limit_a) * thermal_scale`** — `c_rate_limit_a`
      itself is DROPPED from the `min(...)` (review flag: since
      `charge_current_limit_curve` values are constrained to `<=1.0`,
      `soc_charge_limit_a <= c_rate_limit_a` always, making the flat term
      permanently dead; keeping it would be ambiguous — silently correct by
      accident rather than by design, and would silently ignore any future
      curve value `>1.0` instead of surfacing it as the data error it would
      be). The field's docstring must state the `<=1.0` constraint as an
      explicit invariant the curve is trusted to satisfy (same trust
      convention as every other unvalidated `Sourced` curve in this file --
      `ocv_curve_v`, `r0_curve_ohm` -- not a new validation mechanism).
      Deliberately named without "fast_charge" — this is a general BMS
      charge-current ceiling that applies to every charge path, including
      regen (a real BMS limits charge current at high SOC regardless of
      source, which is why EVs show reduced-regen warnings near full
      charge); scoping the name to fast-charge-only would mislead a future
      reader into thinking it's unreachable outside E2e.
      **Behavioral check performed before committing to this** (not
      assumed): read every regen/charge-direction test body directly. The
      two direct charge-direction unit tests (`test_charge_increases_soc_
      and_terminal_voltage_exceeds_ocv`, `test_charge_at_0c_is_limited_
      more_than_charge_at_25c`) and the controller regen sign-check
      (`test_battery_current_demand_matches_commanded_torque_direction`)
      all run at `soc=0.5`, below the curve's 60% plateau threshold — none
      assert a value this change would move. Full suite (default + slow)
      still gets re-run after implementation as final confirmation. Note
      in the eventual commit message: once live, driving downhill at high
      SOC will emit new `BATTERY_LIMIT_TRIGGERED` regen-limiting events
      that didn't fire before — correct behavior, not a regression, but a
      visible change to existing dashboard runs that shouldn't be mistaken
      for one.
      Field docstring must state, explicitly, three things (a third was
      added on review):
      1. **Separable approximation**: SOC and temperature contributions
         multiply (`soc_charge_limit_a` times the function's existing
         `thermal_scale` -- itself `hot_scale * cold_scale`, or 0.0 below
         `min_operating_temp_k` -- applied via the SAME final multiplication
         the function already does for its other two `min()` terms, not a
         new or separate multiplication step). Real BMS limits can be
         non-separable, particularly where the taper knee shifts with pack
         temperature. Revisit if sourced curves become available.
      2. **Identical across all six profiles by design, same reason as
         `aging.py`'s shared coefficients**: any Leaf-vs-Model3 fast-charge
         difference must emerge from thermal architecture (Leaf's
         `hot_scale` dropping sharply from no active battery cooling, so
         its *effective* charge ceiling throttles hard even though the
         *curve* is identical) rather than from a profile-specific curve
         shape. Giving the Leaf a steeper curve later would double-count
         the throttle effect its own thermal design already produces and
         break the causal attribution this comparison exists to
         demonstrate.
      3. **`<=1.0` is a trusted invariant, not runtime-validated** — see
         the dropped-`c_rate_limit_a` reasoning above.
      Placeholder curve, `TODO(spec-sourcing)` like every other coefficient,
      one fixed set of numbers, not a range: `(soc=0.0, scale=1.0)`,
      `(0.6, 1.0)`, `(0.95, 0.2)`, `(1.0, 0.2)` — full ceiling to 60% SOC,
      linear taper 60%->95% down to 20% of `max_charge_c_rate`, constant 20%
      ceiling 95%->100%.
    - **New `ChargingProfile.dc_charge_power_kw: Sourced[float]`** ("dc" 
      already implies fast charging; "fast" was redundant) — "Charger-rated
      power during a DC fast-charge session, in kW — distinct from
      `charge_power_kw`, which is the predominant (typically slower)
      charger type's power." Populated as a placeholder across all six
      profiles. Flagged, not addressed in E2e: whether charge rate belongs
      on `ChargingProfile` at all, or should split between a charger
      abstraction and a per-vehicle max-acceptance-rate field, since real
      effective charge rate is `min(charger.max_kw, vehicle.max_
      acceptance_kw)` — revisit whoever adds a second charger type.
      `charge_power_request_w` (referenced below) is explicitly
      `charging_profile.dc_charge_power_kw.value * 1000.0`.
    - **New `SimStepRecord.charging: bool`**, set `True` only inside
      `advance_charging_step` (default `False` everywhere else, via
      `advance_one_step` explicitly passing `False`, not a field default
      that could silently mean something different in an untouched call
      site). Review addition, not part of the original plan: reusing
      `SimStepRecord`/`SimTimeSeries` unchanged for charging is still the
      right call, but without this field a charging session and an idle
      park look identical in the resulting time series — `speed_mps=0.0`,
      `requested_torque_nm=0.0`, everything else quiet — which is a real
      usability gap for the dashboard surface this data will feed next,
      not just a data-modeling nicety. `mode: Literal["driving", "parked",
      "charging"]` was considered and rejected for now — more extensible,
      but more churn than this milestone needs; revisit if a fourth state
      is ever needed.
    - **`sim.advance_charging_step`**, peer to `advance_one_step`, reusing
      `SimStepRecord`/`SimState` unchanged (driving-specific fields --
      `speed_mps`, `requested_torque_nm`, etc. -- just stay 0.0/unchanged,
      same as they already do whenever `_is_driving` is false). Pipeline:
      (1) `charge_current_request_a = -charge_power_request_w /
      state.battery_voltage_v` (negative = charging, constant-power
      request, same conversion convention `advance_one_step` already uses
      elsewhere; `state.battery_voltage_v` is the step-START terminal
      voltage, same convention `voltage_limit_a` and every other per-step
      ceiling already uses -- there is no step-end value available yet);
      (2) `battery.compute_battery_electrical_step` — the SAME call
      `advance_one_step` makes, unchanged, which already routes through the
      newly-extended `available_charge_current_a` (itself called with the
      step-start `soc`, same convention) with no further code needed for
      the BMS limit to apply; (3) `aging.compute_aging_step` — charging
      ages the battery too (cycle aging from real throughput, calendar
      aging from real temp/soc), same call as driving; (4) `controller.
      step_controller` called with `driver_torque_request_nm=0.0`,
      `speed_rad_per_s=0.0` for cooling duty only — **verified before
      committing to this plan, not assumed**: read `step_controller`'s
      full body. Its cooling-duty block (`commanded_pump_duty`/
      `commanded_fan_duty`) depends only on `component_temps_k` (a
      parameter built from `motor_winding_temp_k`/`inverter_junction_
      temp_k`/`battery_cell_temp_k`, all passed in, not derived from
      arbitration), `loop.serves`, `loop.thermostat_temp_k`, `coolant_
      temps_k` (also a parameter), and `pump_aggressiveness` — nothing in
      that block reads `driver_torque_request_nm`, `commanded_torque_nm`,
      or any ceiling/arbitration result computed earlier in the function.
      Cooling duty is genuinely thermostat-driven and independent of the
      torque-arbitration path; a zero torque request produces correct,
      non-zero cooling response when a component is actually hot. The
      torque-arbitration output (`commanded_torque_nm`, `battery_current_
      demand_a`) is unused by `advance_charging_step`, which computes its
      own `charge_current_request_a` directly rather than reading `battery_
      current_demand_a`; (5) `step_cooling` per loop, then one combined
      `step_thermal_network` solve over the SAME network `advance_one_step`
      builds, with motor/inverter heat inputs at zero; (6) no vehicle-speed
      advance (`speed_mps` stays 0.0 always), no odometer change. dt: reuse
      `scenario.dt_policy.min_dt_s` throughout, stability-checked via the
      existing `_stabilize_dt_s` (reused unchanged) but WITHOUT `_choose_
      target_dt_s`'s tier-selection logic — charging has only one regime
      (actively charging), not a driving-vs-parked choice to make.
    - **`sim.run_charging_session`**, peer to `run_scenario`, looping
      `advance_charging_step` for a FIXED `duration_s` (not an open-ended
      "until target SOC reached" condition) — matches every other
      archetype's fixed-duration convention; if the placeholder taper
      curve means a session doesn't reach a realistic end SOC in that fixed
      duration, that's itself diagnostic information worth surfacing in
      characterization output, not something to silently paper over with
      an open-ended stopping condition.
    - **`long_horizon.characterize_fast_charge_session`**, peer to
      `characterize_trip`, calling `run_charging_session` at one constant
      pinned ambient bucket (same convention as `characterize_trip`),
      producing the SAME `TripCharacterization` shape so `simulate_long_
      horizon`/`apply_trip_start_offset` need zero changes to consume it.
      `characterize_all_trips` gets one added call for this function,
      merged into the same returned dict — `FAST_CHARGE_SESSION` is NOT
      added to `_ARCHETYPES`/`characterize_trip`'s dispatch table, since it
      needs its own charging-mode pipeline, not the driving one.
      **Arrival temp caveat (review flag, replacing an earlier overclaim):**
      `_archetype_assumed_start_temp_k`'s existing `else` branch returns
      plain `ambient_bucket_temp_k` for `FAST_CHARGE_SESSION` with no code
      change needed, but this is an APPROXIMATION, not something to claim
      as "correctly covering" the real case. A real fast-charge session
      arrives with the pack elevated by the preceding drive, not at
      ambient. The characterization's docstring must say so explicitly:
      *"Arrival pack temp for a fast-charge session is physically elevated
      by the preceding drive, not at ambient. This characterization
      assumes ambient-bucket start temp for consistency with every other
      archetype's linear-offset replay scheme; the real arrival-temp effect
      is absorbed by `apply_trip_start_offset` at replay time and is
      therefore approximate. Revisit if offset error is shown to dominate
      the fast-charge thermal signature specifically."*
      **Phase structure — two phases (`ramp`, `taper`), boundary at the SOC
      curve's 60% knee, with explicit edge-case handling (review flag: the
      plan was silent on this and a naive implementation would crash).**
      SOC is monotonically non-decreasing across a charging session (
      `advance_charging_step` always requests negative/charging current;
      the applied magnitude only ever shrinks, never flips sign, since
      every ceiling in `available_charge_current_a` is non-negative by
      construction) — so a 60%-crossing, if it exists, exists exactly once;
      the "starts and ends above the knee with a dip below" case the
      reviewer raised as hypothetical cannot occur here, confirmed by this
      reasoning, not just empirically. Two real edge cases remain: a
      session that starts at/above 60% SOC (no crossing; everything is
      really "taper"), and one whose fixed `duration_s` never reaches 60%
      SOC (no crossing; everything is really "ramp"). **`TripCharacterization.
      phases` must always have exactly 2 entries regardless — found while
      tracing the actual consumer, not just the two cases named above**:
      `_interpolate_characterization` zips `lo.phases`/`hi.phases` with
      `strict=True`, so if one ambient bucket's session crosses the knee and
      a different bucket's doesn't (plausible -- colder buckets charge more
      slowly), producing a variable-length phase list per bucket would crash
      bucket interpolation the first time two adjacent buckets disagree on
      phase count, not just on some single-session edge case. Resolution:
      always construct both phases. When a real crossing exists, both
      `_phase_aging` calls run unchanged over `[0, boundary_s)` and
      `[boundary_s, duration_s)`. When no crossing exists, the phase with no
      real duration is a DEGENERATE zero-duration phase built directly from
      the boundary sample's own instantaneous values (`duration_s=0.0`,
      `throughput_ah=0.0`, temp/soc/c_rate taken from that single sample) —
      not routed through `_phase_aging`, which raises on an empty window by
      design (`covered_s <= 0.0`) and should keep doing so for a genuine
      empty-time-series bug elsewhere, not be loosened to silently accept
      this specific degenerate case. **Pinning which sample, explicitly, so
      the two edge cases aren't asymmetric by accident:** when `ramp` is
      degenerate (session starts at/above 60% SOC), it's built from the
      `t=0` sample. When `taper` is degenerate (session never reaches 60%
      SOC in `duration_s`), it's built from the `t=duration_s` (final)
      sample.
      Session start/end SOC: reuses `charging_profile.typical_start_soc`/
      `typical_end_soc` (no new field) — flagged as a real simplification,
      not fully realistic (real fast-charge sessions often start from a
      more depleted SOC than a "typical daily trip" start), accepted to
      avoid inventing a field beyond what was explicitly scoped; revisit if
      it produces an implausible session shape once real numbers are seen.
- **Milestone E2e's first real output (Leaf-in-Guam vs. Model-3-in-Seattle,
  90-day SOH trajectories) surfaced two findings that needed investigation
  before being trusted, both now resolved by direct code execution, not
  speculation — and a framing correction to how this comparison is
  described.**
  1. **The cycle-aging "inversion" (Leaf aging slower than expected relative
     to Model 3 on the cycle term) is NOT a bug.** Hand-traced by calling
     `aging.compute_aging_step` directly with matched C-rate and
     temperature but each profile's own `capacity_nominal_ah`: `cycle_delta`
     scaled EXACTLY linearly with pack capacity (ratio 3.017, matching the
     Leaf/Model 3 capacity ratio to three decimal places), because
     `throughput_ah` in `compute_aging_step` is an absolute amp-hour
     quantity, never normalized by pack capacity. This is a genuine,
     previously-undocumented modeling gap, not an Arrhenius sign error or a
     calibration bug: cycle aging's absolute-throughput formulation makes
     pack size — not just thermal architecture — a live confounding
     variable in any cross-vehicle cycle-aging comparison. It does not
     affect calendar aging, which has no capacity or current term. See
     `aging.py`'s module docstring ("Shared coefficients across all six
     reference vehicles") for the full hand-traced evidence, including the
     partial-offset case (Leaf 5K hotter than Model 3 dropped the ratio to
     2.480 — real, but nowhere near enough to overcome the ~3x capacity
     gap). **Fixed (2026-09-24), in its own dedicated milestone, sequenced
     before Monte Carlo/fork/mistake-library per explicit instruction — not
     folded into anything else.** Documenting the false "100% attributable
     to thermal behavior" claim without fixing it was rejected as
     insufficient: the claim needed to become true, not just have its
     falseness noted. Two design questions were opened and ratified before
     any code changed — `docs/DESIGN_QUESTIONS.md` Q-0006 (normalize by
     nominal or current/SOH-scaled capacity? **Resolved: nominal**
     — `capacity_nominal_ah`, never the current SOH-scaled capacity,
     specifically to avoid introducing an uncalibrated positive-feedback
     mechanism, SOH -> EFC rate -> SOH, that nothing else in `aging.py`
     models or validates; this makes the resulting trajectory a bounded
     approximation, most accurate at SOH >= 0.7) and Q-0007 (what anchor
     does the re-derivation use, and in what units? **Resolved: the
     existing single cycle-aging anchor, re-expressed via a third assumed
     constant — a 75 kWh generic reference pack capacity, explicitly NOT
     the six profiles' mean/median, alongside the existing 370V/1C
     assumptions** — `100,000 kWh / 75 kWh = 1,333.33 EFC`, giving
     `cycle_aging_rate_per_efc = 0.18 / 1,333.33 = 1.350e-4`).
     Implementation: `cycle_aging_rate_per_ah` renamed to
     `cycle_aging_rate_per_efc` across `schema.py`, `aging.py`, all six
     profile YAMLs, and every test fixture; `compute_aging_step` now
     normalizes throughput into equivalent full cycles
     (`throughput_efc = throughput_ah / capacity_nominal_ah`) before
     applying the rate constant. Full test suite (224 tests) and
     `mypy --strict`/`ruff` pass; `test_long_horizon_validation.py -m slow`
     (Tier-2-vs-Tier-1 real-profile validation, Leaf and Model 3) still
     passes within its existing tolerance after the fix.
  2. **The "modest" ratio versus the two calibration anchors (~6%/year-class
     expectation) is NOT a calibration regression.** Both anchors were
     reproduced EXACTLY by running `compute_aging_step` under their own
     precise stated conditions (Leaf: 40°C/80% SOC/5yr → 0.7000 SOH exactly;
     Model 3: 20°C/60% SOC/10yr → 0.9000 SOH exactly). The anchors are
     idealized, single-condition, calendar-only benchmarks (constant
     temperature, constant SOC, zero current, held for the full multi-year
     duration) — they were never a claim about what a realistic mixed-usage
     trajectory (varying ambient, varying SOC, real driving/charging
     cycling) would show. Confirmed directly: the 90-day comparison's
     calendar-only loss matched a hand-calculation at the run's own average
     SOC (~0.65, since SOC oscillated ~0.5-0.8) far more closely than at the
     Leaf anchor's constant 80% SOC condition — the "modest" ratio is
     explained by comparing a realistic scenario against an idealized
     anchor, not by a broken coefficient. Don't treat a smaller-than-anchor
     ratio in a future run as evidence of a regression without first
     checking whether that run's actual average conditions match an
     anchor's stated conditions. `docs/model_reference.md`'s "Aging model
     confidence and limits" section and `aging.py`'s module docstring both
     now state this explicitly.
  3. **The "flagship comparison" framing needs correcting, and is corrected
     here rather than left implicit.** This 90-day Leaf-in-Guam vs.
     Model-3-in-Seattle comparison demonstrates the *mechanism* (thermal
     architecture producing different battery-temperature exposure, which
     the aging model converts to different SOH loss) — it does NOT yet
     demonstrate the full *compounding divergence* this project exists to
     teach, because two things are both required and neither is modeled
     yet: arrival-temperature elevation from realistic climate/usage
     patterns (fast-charge preconditioning is explicitly deferred — Q-0004
     above), and multi-year accumulation (90 days is far short of the
     20-30-year horizon this project's premise is built on, and Milestone
     E2's own Tier-1/Tier-2 architecture exists specifically to make that
     horizon tractable — see Milestone E2's own entries above). Both are on
     the roadmap: the preconditioning milestone, and running this same
     comparison at a genuine multi-year horizon once both the
     capacity-normalization question (finding 1) and the preconditioning
     gap are addressed. Do not present a short-horizon comparison run under
     today's model as "the compounding divergence" claim until then — it is
     evidence toward it, not a demonstration of it.
  **Corrected 90-day Leaf-in-Guam vs. Model-3-in-Seattle comparison, run
  after the EFC-normalization fix, reported alongside the pre-fix numbers
  under IDENTICAL scenario conditions (same script, only the aging formula
  toggled) — the corrected numbers replace the pre-fix ones in this
  project's record; the pre-fix ones are kept here only as the direct
  before/after evidence that the fix moved things in the expected
  direction:**
  | | Leaf/Guam total | Leaf/Guam cycle | Model3/Seattle total | Model3/Seattle cycle | cycle ratio (Leaf/Model3) |
  |---|---|---|---|---|---|
  | Pre-fix (Ah-direct) | 0.6295% | 0.0111% | 0.1243% | 0.0017% | 6.51 |
  | **Corrected (EFC)** | **0.6520%** | **0.0336%** | **0.1243%** | **0.0017%** | **19.63** |

  **Expected-direction check (per explicit instruction: "If it doesn't
  move in that direction, stop and surface") passed** — Leaf's cycle loss
  now exceeds Model 3's by a much wider margin than before the fix
  (19.63x vs. 6.51x), consistent with the smaller, hotter pack accruing
  more equivalent full cycles per unit of driving. Calendar loss and the
  Model 3's own cycle loss are unchanged (EFC normalization only touches
  cycle aging, and Model 3's `capacity_nominal_ah` — the EFC denominator —
  is close enough to the new 202.7 Ah reference constant that its own
  cycle numbers barely move).
  **Transparency note, and the process gap it exposed, now fixed:** this
  comparison's exact scenario parameters (climate amplitudes, day-type
  mix, charging profile) had to be reconstructed from scratch for this
  re-run — the original script that produced the earlier-reported "1.24x
  ratio, inverted cycle component" numbers was not preserved. This
  reconstruction is a good-faith equivalent (same climates, same profiles,
  same anchor-consistent day-type mix pattern), not a byte-identical
  replay, and its own pre-fix numbers (total ratio 5.06x, cycle ratio
  6.51x — already Leaf > Model3 under this specific reconstruction) don't
  reproduce the earlier "inverted" finding exactly. That's expected and
  doesn't weaken the fix's justification: the capacity confound was proven
  analytically and via a controlled hand-trace (finding 1 above)
  independent of any one scenario's specific current/temperature mix,
  which can push the pre-fix ratio above or below 1 depending on how much
  C-rate and temperature differences happen to offset the capacity effect
  in that specific run. The fix removes the confound either way. **What's
  actually verified is narrower than "the original inversion is fixed":**
  it's "under a reconstruction, the fix moves Leaf cycle aging in the
  expected direction by the expected magnitude" — real evidence, just
  scoped to this reconstruction, not the lost original run.
  **The underlying process gap — the sim is deterministic (same seed, same
  inputs -> byte-identical output) but the ad hoc scripts that configure
  and run a specific named comparison were never committed, so that
  guarantee didn't actually cover comparisons like this one — is fixed,
  not just noted.** `examples/flagship_comparison.py` is now the
  committed, reproducible runner for this comparison: every scenario
  parameter (profile paths, climate presets, day-type mix, charging
  profile, duration, seed) is an explicit, named argument or CLI flag, not
  an inline literal in a throwaway script. `uv run python
  examples/flagship_comparison.py` reproduces the corrected row of the
  table above exactly; `--duration-days`/`--seed`/`--profile-a`/
  `--profile-b` are exposed specifically because they're the axes this
  comparison is expected to be re-run along (multi-year horizon,
  preconditioning, other profile pairs). Any future comparison that ends
  up in this file's record should go into a script the same way, not be
  run ad hoc.
  **Monte Carlo, fork branching, and the mistake library's hold is now
  lifted** — findings 1, 2, and 3 are all resolved, finding 1's fix is
  implemented and tested, and the corrected comparison has been reviewed.
- **Milestone "Monte Carlo v1" (done — design decisions ratified via
  `docs/DESIGN_QUESTIONS.md` Q-0008 through Q-0011):** the first working
  Monte Carlo capability, scoped narrowly and deliberately per Q-0008 —
  sampling only `BatterySpec`'s seven aging-related `Sourced[float]` fields
  (`calendar_aging_rate_per_s`, `calendar_aging_activation_temp_k`,
  `calendar_aging_soc_sensitivity`, `cycle_aging_rate_per_efc`,
  `cycle_aging_activation_temp_k`, `cycle_aging_c_rate_sensitivity`,
  `resistance_growth_per_soh_loss`), reusing ONE Tier-1 characterization
  across every sample (verified none of these fields feed into Tier-1
  physics — only into `aging.compute_aging_step`, called during Tier-2
  replay). Thermal/electrical-spec and climate sampling — which would
  force re-running Tier-1 per sample, ~500x the cost — deferred to a later
  milestone.
  **New module `mc.py`:** `sample_lognormal` (Q-0009: log-normal, MEDIAN-
  centered at each field's `value` — not mean-centered; `mu = ln(median)`,
  no `sigma`-dependent correction), `CV_BY_CONFIDENCE` (the one fixed,
  project-wide spread mapping — LOW=0.50, MEDIUM=0.20, HIGH=0.05 — stated
  once and referenced from every sampled field, deliberately not decided
  per-field, matching Q-0009's explicit reasoning that per-field spreads
  would reintroduce the same confound class the EFC-normalization fix just
  removed), `sample_battery_spec` (Q-0010's minimal `battery.*`-only
  resolver — no general dotted-path walker), and `run_monte_carlo` (holds
  the day-type schedule seed — `scenario_template.random_seed` — fixed
  across every sample, confirmed by directly reading
  `simulate_long_horizon`'s body that `shuffle_day_type_schedule` is its
  ONLY internal RNG consumer; a separate, independent stream derived from
  `MCConfig.random_seed` via `numpy.random.SeedSequence.spawn` drives only
  the coefficient sampling).
  **New schema types:** `MCSampleResult` (per-sample provenance — which
  values were drawn — plus its full `soh_trajectory` and
  `endpoint_metrics`), `MCPercentileBand` (time-indexed p05/p50/p95),
  `MCResult` (Q-0011: percentile computation lives in `mc.py`, not
  `dashboard/` — it's statistics on raw output, not rendering; every
  consumer shares one implementation). `MCResult`'s docstring carries both
  of Q-0009's required caveats verbatim: the band is uncertainty around a
  placeholder coefficient value, not real battery chemistry; and it's
  conditional on one fixed day-type schedule, not a sample over schedule-
  order variance (a separate, deferred MC mode). Band is defined as
  p05/p50/p95 only, not a wider tail — at N=500 (this project's own target
  sample count), sampling error on p05 is roughly ±0.7% absolute, which
  doesn't support a defensible p01/p99 estimate.
  **A real bug was caught during implementation, exactly by the test the
  design review specified, not discovered later:** the first
  `sample_lognormal` draft computed `mu = ln(value) - sigma^2/2` (mean-
  centering), which made the sampler's empirical median measurably lower
  than the target `value` (confirmed: ~8.9 vs. a target of 10.0 at
  `CV=0.50`, an 11% shift, far outside a 3% tolerance) — contradicting
  both the module's own docstring claim and the whole point of the
  band's `p50` being comparable to a direct point-estimate run. Caught by
  `test_sample_lognormal_mean_and_cv_match_target_over_many_draws` and
  `test_mc_p50_approximates_flagship_point_estimate` (`tests/test_mc.py`)
  — the latter built exactly per instruction: same scenario, same schedule
  seed as `examples/flagship_comparison.py`, asserting the MC median
  tracks a direct `simulate_long_horizon` call within 5% relative at every
  checkpoint. Fixed by dropping the mean-correction (`mu = ln(median)`
  directly); both tests pass afterward. This is the reproducibility/
  verification discipline this project has run all session catching a
  real defect before it shipped, not a hypothetical benefit.
  **Reproducibility (per the standing convention `examples/
  flagship_comparison.py` established):** `examples/monte_carlo_leaf_guam.py`
  is the committed, reproducible runner for the first MC record below —
  `n_runs`/`mc_seed`/`schedule_seed`/`duration_days`/`profile` are all
  explicit CLI flags, reusing `flagship_comparison.py`'s own scenario
  builders directly (not re-declared) so the two scripts' schedule seeds
  can never silently drift apart. Must be run as `python -m examples.
  monte_carlo_leaf_guam`, not a bare script path — it cross-imports
  `examples.flagship_comparison`, which needs the repo root on `sys.path`;
  a bare `python examples/monte_carlo_leaf_guam.py` invocation puts
  `examples/` itself there instead and fails with `ModuleNotFoundError`
  (documented in the script's own usage docstring).
  **First recorded run** (Nissan Leaf / Guam climate, N=500,
  `mc_seed=999`, `schedule_seed=42`, same scenario as the flagship
  comparison, 90-day window):
  | day | p05 | p50 | p95 |
  |---|---|---|---|
  | 0 | 99.9763% | 99.9909% | 99.9959% |
  | 30 | 99.3764% | 99.7381% | 99.8745% |
  | 60 | 98.8450% | 99.5165% | 99.7700% |
  | 89 | 98.3592% | 99.3067% | 99.6715% |

  Per Q-0009's mandatory caveat: this band is sensitivity to placeholder
  aging-coefficient uncertainty under one fixed usage schedule, not a
  real-world confidence interval — do not present it as the latter until
  the underlying coefficients are sourced.
- **A first multi-year Monte Carlo run (Leaf/Guam, 20 years) was run as a
  pre-publication check, per this project's own "check the actual numbers
  before committing" discipline — and its numbers are deliberately NOT
  published here, in any form, reframed or otherwise.** The unperturbed
  point-estimate run alone predicts 40.9% SOH by year 20; the MC band's
  p05 hits literal 0% SOH before year 20. Investigated, not assumed to be
  a code bug: dumping every checkpoint's `battery_soc` across the full
  run showed `distinct SOC values over full 20yr run: [0.8]` — SOC is
  pinned at exactly `typical_end_soc` for the ENTIRE horizon, not just
  overnight. This is Milestone E2c's own already-documented "SOC-snapping"
  approximation (previously estimated there as "a 50-70% overnight
  calendar-aging overestimate"), made catastrophically visible by a
  realistic multi-year horizon and a trip-heavy day-type mix that keeps
  SOC pinned essentially 100% of the time: `calendar_delta = rate * dt`
  has no saturation term (purely linear in time), and
  `calendar_aging_soc_sensitivity=1.0` gives a permanent +30% rate at
  `soc=0.8` versus the `soc=0.5` baseline — compounded linearly for 20
  straight years instead of the fraction of real time a vehicle actually
  spends at high SOC. **Explicitly rejected: publishing this table with a
  caveat.** An artifact labeled "20-year Leaf-in-Guam SOH" gets cited by
  future readers regardless of its caption — the `Sourced`/caveat
  discipline this project already runs on exists precisely because
  caveats don't travel with a number once it's quoted elsewhere. This is
  evidence the tool's own SOC handling is currently fabricating part of
  the compounding-divergence effect, not evidence toward demonstrating it,
  and it does not get published while that's true.
  **Priority reorder, explicit and deliberate:** this fix now outranks
  the preconditioning milestone (Q-0004's deferred work, referenced
  earlier in this file). Preconditioning affects roughly the fraction of
  days that include a fast-charge session, once per session, and its
  omission makes Leaf-in-Guam look LESS bad than reality. The SOC-
  snapping gap affects every hour of every day and makes Leaf-in-Guam
  look MORE bad than reality — of the two, it's the one actively
  distorting the headline claim right now, not just leaving it
  incomplete.
  **Scope: this is a fix to `long_horizon.py`'s SOC handling, not to
  `mc.py`.** Monte Carlo v1 is correct as it stands — it faithfully
  extrapolated whatever `simulate_long_horizon` gave it; the bug is
  upstream. Two design questions this fix raises are opened as
  `docs/DESIGN_QUESTIONS.md` Q-0012 (parked-SOC model scope — a
  lightweight piecewise-linear decay-and-daily-reset model, not a full
  charge-event model) and Q-0013 (how to treat the two calendar-aging
  anchors once SOC dynamics change — re-derive the coefficients, or
  re-express the anchors as constant-SOC self-consistency checks the new
  model must still reproduce under a literally-constant-SOC scenario).
  Both are ratified-pending, not yet decided.
  **The 90-day flagship comparison (`examples/flagship_comparison.py`,
  documented earlier in this file) is unaffected and stands as-is.** Not
  because SOC-snapping is harmless at 90 days — it isn't — but because at
  90 days the bias is a roughly-30%-elevated multiplier on a small
  absolute number, and the comparison is comparative under an IDENTICAL
  bias applied to both vehicles. The mechanism claim (thermal architecture
  produces different SOH outcomes) is intact at that horizon; the
  compounding-over-decades claim was never made there in the first place.
  **Nothing gets published at multi-year horizons — no MC record, no
  Leaf-vs-Model3 long-horizon comparison — until Q-0012/Q-0013 are
  ratified and the fix lands.** Fork branching and the mistake library
  remain correctly deferred behind the compounding-divergence question,
  and this fix is now squarely on that same path.
- **Milestone "parked-SOC model" (done — design decisions ratified via
  `docs/DESIGN_QUESTIONS.md` Q-0012/Q-0013, plus three new questions
  opened and left unresolved by design, Q-0014/Q-0015/Q-0016):** replaces
  the SOC-snapping approximation that produced the previous entry's
  implausible 20-year numbers.
  **Model:** `simulate_long_horizon` now tracks SOC as a real value through
  each 24-hour cycle instead of snapping it to `typical_end_soc`
  instantaneously after every trip. A trip applies its own actual SOC
  delta (`characterization.final_soc - charging_profile.typical_start_soc`
  — Tier-1's own already-computed result, not re-derived); parked periods
  decay linearly via a new `_parked_soc_trajectory` function (mirroring
  `relax_parked_temp_k`'s own step_s/sampling convention exactly, so the
  two series stay index-aligned for `parked_calendar_aging`'s quadrature,
  which now takes a real `soc_samples` series instead of one constant
  scalar); one charge event per day, at a new `ChargingProfile.
  charge_reset_hour: Sourced[float]` field (value: 22.0, i.e. late evening
  — a day's trip is simulated starting at the day boundary in this model,
  so realistic overnight charging completing shortly before the NEXT
  day's trip falls late in THIS day's window), resets SOC to
  `typical_end_soc`. No charge-session physics (power caps, taper,
  thermal coupling) anywhere in this — only the daily trajectory's shape,
  which is what calendar aging's SOC-sensitivity term actually consumes.
  **A real finding, caught by the hand-computation this milestone's own
  design review required before any code was written:** literally reusing
  `sim.py`'s existing `aux_current_a` (`hvac_power_w + accessory_load_w`)
  as the parked decay rate is a category error, not a simplification.
  `hvac_power_w`'s field docstring is "typical cabin HVAC electrical
  draw" — a driving/occupied-cabin load; `accessory_load_w`'s is
  "baseline 12V/accessory electrical draw" — already the schema's own
  name for standby drain. Reusing the full sum produced ~5.08%/hour decay
  for the real Nissan Leaf profile — draining a full charge in ~16 hours,
  a NEW degenerate near-permanent-near-zero-SOC failure mode, arguably
  worse than the one this milestone exists to fix. `accessory_load_w`
  alone gives ~0.92%/hour — physically sane. New public function
  `battery.ocv_v` (extracted from the existing `_ocv_and_r0_ohm`'s OCV
  term, no duplicated logic) supplies the pack voltage for the
  power-to-current conversion, evaluated once at `typical_end_soc`/
  `reference_temp_k` (not recomputed hourly — `docv_dt_v_per_k=0.0` for
  every profile today, so temperature dependence is already inert
  everywhere in this project).
  **Pre-implementation hand-verification** (required before writing any
  Tier-2 code, per this project's standing discipline): a standalone
  recurrence calculation using the real Leaf/Guam scenario, real Tier-1
  trip deltas, and the real shuffled day-type schedule gave a 1-year
  time-weighted average SOC of ~0.648 (min 0.24, max 0.80) — below an
  initial ~0.68-0.72 estimate, but that estimate was acknowledged as a
  rough first-principles guess, not a calibrated target, and 0.648 was
  accepted as the model's honest output, not something to tune toward a
  target.
  **Q-0013 (anchor treatment): resolved as Option 2, coefficients
  unchanged.** `calendar_aging_rate_per_s`/`calendar_aging_activation_
  temp_k` are NOT re-derived — the anchors' 80%/60% SOC values are
  calibration conditions, not proxies for real fleet SOC trajectories, and
  re-deriving against a changing SOC model has no principled stopping
  point. The two anchor-reproduction tests in `tests/test_aging.py` call
  `compute_aging_step` directly with constant `soc`/`cell_temp_k`, never
  touching `simulate_long_horizon`'s trajectory at all — confirmed
  unaffected by the full suite passing unchanged. New deferred-work item,
  not a gap to fix: "anchor calibration sensitivity to assumed
  constant-SOC conditions" — realistic-trajectory predictions carry this
  systematic uncertainty until real fleet SOC distributions justify
  re-deriving the coefficients.
  **Three new design questions opened and deliberately left unresolved**
  (`docs/DESIGN_QUESTIONS.md` Q-0014/Q-0015/Q-0016): what `typical_start_
  soc` actually means now that it's a reference point rather than an
  enforced daily value (current implemented behavior documented as
  matching "Option 2, descriptive not prescriptive" from that discussion,
  not deliberated as definitively correct); how `FAST_CHARGE_SESSION`
  days should interact with the daily charge-reset event (currently no
  special-casing — a fast-charge day's session and the day's normal reset
  both apply, redundant but harmless); and that `test_long_horizon_
  validation.py`'s reference generator still uses the OLD snapping
  assumption and needs its own follow-up fix (currently still PASSES, but
  only because both real-profile losses are small enough at its 60-day
  window to sit under the 0.1% absolute tolerance floor — relative error
  grew from ~0.8-4% historically to 15.64%/10.14% post-fix; a longer
  window or different seed could push this past the floor and fail; not
  blocking this milestone, since the fix itself is validated by direct
  hand-computation and the checks below, not by this harness).
  **Validation, per the required sequencing (flagship comparison, then
  20-year point estimate, before any multi-year MC):**
  1. **Flagship 90-day comparison re-run — comparative structure survives,
     absolute numbers dropped, as expected:**
     | | Leaf/Guam total | Leaf/Guam calendar | Model3/Seattle total | total ratio |
     |---|---|---|---|---|
     | Pre-SOC-fix (constant 0.8) | 0.6520% | 0.6184% | 0.1243% | 5.245 |
     | **Post-SOC-fix** | **0.5828%** | **0.5492%** | **0.1194%** | **4.883** |

     Both vehicles age less (expected — time-weighted average SOC ~0.648
     is below the old constant 0.8), and the comparative structure holds
     (ratio moved from 5.245 to 4.883, not collapsed or inverted). Cycle
     loss is unaffected by this fix (0.0336%/0.0017%, unchanged — the
     parked-SOC model only touches calendar aging).
  2. **20-year unperturbed Leaf/Guam point estimate — plausible in
     direction and magnitude, but BELOW the pre-stated "55-70% credible"
     expectation, reported honestly rather than adjusted toward it:**
     47.279% SOH at year 20 (vs. the pre-fix model's 40.910% — moved in
     the right direction, by a real amount, but not into the stated
     credible band). SOC now genuinely varies day-to-day (confirmed by
     direct trajectory dump: distinct end-of-day SOC values across the
     full 20-year run span roughly 0.7815-0.7861, no longer pinned at a
     single constant), so the SOC-snapping severity finding that
     triggered this milestone is conclusively resolved. **The most likely
     explanation for landing below the credible estimate is the
     separately-real, already-anticipated concern about calendar aging's
     purely-linear-in-time formulation (no saturation term) extrapolated
     roughly 2-4x past the calibration anchors' own 5-10 year window** —
     see `docs/model_reference.md`'s new "Validated time horizon" note.
     This is NOT treated as a bug to chase or a result to tune toward the
     estimate — the estimate was a rough guess made before running the
     model, not a target, and forcing the number into range would be
     exactly the "tune the model to the desired narrative" failure mode
     this project's rigor has avoided throughout. **Full 500-sample
     multi-year Monte Carlo is intentionally NOT run or published as part
     of this milestone** — the point-estimate check came back outside the
     stated credible range, and per the explicit gate this milestone was
     scoped under, that result gets surfaced for review before spending a
     500-sample run on it, not treated as "close enough, proceed."
  3. **MC v1's 90-day record re-run, replacing the previous entry's
     numbers (N=500, same mc_seed/schedule_seed, only the underlying SOC
     model changed):**
     | day | p05 (old → new) | p50 (old → new) | p95 (old → new) |
     |---|---|---|---|
     | 0 | 99.9763% → 99.9845% | 99.9909% → 99.9937% | 99.9959% → 99.9973% |
     | 30 | 99.3764% → 99.4609% | 99.7381% → 99.7708% | 99.8745% → 99.8889% |
     | 60 | 98.8450% → 99.0068% | 99.5165% → 99.5733% | 99.7700% → 99.7948% |
     | 89 | 98.3592% → 98.5980% | 99.3067% → 99.3860% | 99.6715% → 99.7055% |

     Every value moved slightly higher (less degradation), consistent
     with the flagship comparison's own direction and the ~0.648 vs. 0.8
     average-SOC explanation. `examples/monte_carlo_leaf_guam.py` was
     generalized to accept `--duration-years` (from a hardcoded 1) as
     part of the multi-year investigation this milestone grew out of.
  All prior sections of this file describing the pre-fix SOC-snapping
  behavior, its EFC-table numbers, and the earlier 90-day MC record are
  kept as historical record (explicitly, not silently superseded) — this
  entry's numbers are what's current.
  **Gate cleared (2026-09-25), per `docs/DESIGN_QUESTIONS.md` Q-0017:** the
  47.3%-vs-"55-70% credible" gap above was reviewed and accepted as a
  documented extrapolation limit (calendar aging's linear-in-time
  formulation, no saturation term, extrapolated 2-4x past the calibration
  anchors' 5-10 year window), not a bug — supported by the anchors still
  reproducing exactly under their own stated conditions, the fix having
  moved the estimate in the right direction by a real amount, and the
  mechanism being understood rather than mysterious. The model was NOT
  tuned to land inside the pre-run guess. **This unblocks the full
  500-sample multi-year Monte Carlo milestone**, which this gate had been
  holding; the linear-in-time/anchor-extrapolation limit itself carries
  forward as a deferred aging-model fidelity item, not as a reason to
  withhold running or publishing multi-year results going forward. Any
  multi-year SOH number should still carry `docs/model_reference.md`'s
  "Validated time horizon" caveat when presented.
- **Milestone "Multi-year Monte Carlo v1" (done, 2026-09-25):** the first
  genuine multi-year Monte Carlo record, run after Q-0017 cleared the gate
  above. No new sampling code was needed — `mc.py`/`examples/
  monte_carlo_leaf_guam.py` already supported arbitrary `duration_years`;
  this milestone is the reviewed record itself plus the investigation it
  surfaced, not a new capability.
  **Record (Nissan Leaf / Guam, N=500, mc_seed=999, schedule_seed=42, 20
  years — reproduce via `uv run python -m examples.monte_carlo_leaf_guam
  --duration-years 20`):**
  | day (≈year) | p05 | p50 | p95 |
  |---|---|---|---|
  | 0 (0) | 99.9845% | 99.9937% | 99.9973% |
  | 1825 (5) | 65.8973% | 86.0666% | 93.2431% |
  | 3650 (10) | 31.8158% | 72.1386% | 86.4907% |
  | 5475 (15) | 0.0000% | 58.2135% | 79.7321% |
  | 7299 (20) | 0.0000% | 44.2954% | 72.9846% |

  final_soh across 500 samples: min=0.0000%, max=83.1218%.
  **A second, unplanned investigation was required before this table could
  be recorded**: the p50 at year 20 (44.2954%) sat ~6.3% relative below
  the already-reviewed unperturbed point estimate at the same horizon
  (47.279% SOH, the number Q-0017 just cleared), which is exactly the
  threshold the existing `test_mc_p50_approximates_flagship_point_estimate`
  test enforces at 90 days — worth investigating before treating either
  number as ready to publish, not smoothing over. Two candidate
  explanations were distinguished by evidence, per explicit instruction,
  not by reasoning alone: (1) horizon-dependent non-linearity — the gap
  should grow smoothly and monotonically with horizon; (2) a parked-SOC-
  model interaction artifact — the gap should appear abruptly at long
  horizons, since the 90-day p50 test was never re-validated specifically
  against the parked-SOC-model fix. **Confirmed #1, by two independent
  pieces of evidence, both gathered via `examples/
  mc_horizon_gap_investigation.py`
  (`uv run python -m examples.mc_horizon_gap_investigation`):**
  | horizon | point estimate | MC p50 | gap (relative) |
  |---|---|---|---|
  | 90-day | 99.4172% | 99.3860% | 0.031% |
  | 1-year | 97.3647% | 97.2173% | 0.151% |
  | 5-year | 86.8207% | 86.0743% | 0.860% |
  | 10-year | 73.6411% | 72.1464% | 2.030% |
  | 20-year | 47.2791% | 44.2954% | 6.311% |

  (1) The gap grows smoothly and monotonically with horizon, with no
  discontinuity where a parked-SOC-specific effect would show up. (2) The
  existing 90-day test (`tests/test_mc.py::
  test_mc_p50_approximates_flagship_point_estimate`, N=200, checked at
  every one of 90 daily checkpoints) still PASSES its 5%-relative
  tolerance today, post-parked-SOC-fix — ruling out "the test is stale,"
  which would be #2's signature. A third finding, beyond what was asked
  for: the gap is already nonzero (if small) at 90 days and 1 year, where
  no sample has come anywhere near the SOH floor — proving the underlying
  mechanism doesn't depend on floor-clamping at all; the floor-clamping
  visible in the `p05=0.0000%` cells above (years 15 and 20) is that same
  mechanism reaching its own limit at high cumulative loss, not a second,
  separate effect.
  **Mechanism** (evidence-consistent, explicitly not a formal derivation):
  both `calendar_delta`/`cycle_delta` (`aging.py`) are Arrhenius-
  exponential in a sampled coefficient, summed additively across every
  step of a multi-year run. Integrating a convex, monotonic function of a
  randomly-sampled input over many compounding steps is a textbook Jensen's-
  inequality setup at the median: `sample_lognormal`'s median-unbiased
  INPUT does not imply a median-unbiased OUTPUT once that input is run
  through a nonlinear map and compounded over time. The direction is
  informative, not incidental: `p50` comes in below the point estimate at
  every horizon checked, because "ages faster" is unbounded/exponential
  while "ages slower" is floored at `max(0.0, ...)` — producing a
  left-skewed output distribution, consistent with everything measured
  above.
  **This is now documented everywhere the numbers travel, per explicit
  instruction that the caveat must travel with the numbers, not just live
  in one place:** `mc.py`'s module docstring (full mechanism + evidence,
  new "p50 vs. the point estimate" section), `MCResult`'s docstring in
  `schema.py` (short version, points to `mc.py`), `examples/
  flagship_comparison.py`'s module docstring (explicit warning that its
  own table is a point estimate, not a median, since that's the script
  most likely to get an MC band shown alongside it later without this
  caveat attached), and `docs/model_reference.md`'s new "Monte Carlo p50
  vs. the point estimate" section (reader-facing summary).
  **Practical consequence, stated explicitly per instruction:** at a 20-
  year horizon, "the Leaf reaches ~47% SOH" (point estimate) and "the Leaf
  reaches ~44% SOH" (MC median) are different claims about the same model,
  and only the second describes what happens more often than not under the
  model's own stated coefficient uncertainty. Neither the 20-year table
  above nor the point estimate should be presented as interchangeable, or
  as "the median/typical case," without this caveat attached.
  **What's now unblocked:** the multi-year Monte Carlo gate this milestone
  existed to clear is closed. Fork branching and the mistake library — the
  two roadmap items that were waiting behind the compounding-divergence
  question — are next, per the sequencing already established when that
  question was first raised.
- **Milestone "Fork branching v1" (done, 2026-09-25) — full-fidelity
  (Tier-1-style) forking only.** Before this milestone, everything fork-
  related was either schema-only or an unused stub: `ForkPoint`
  (`schema.py`) round-tripped through JSON but was never constructed by
  production code; `SimState.fork_lineage` existed but nothing ever
  appended to it; `src/ev_thermal/fork.py` was a docstring-only stub. What
  DID already work, and needed no changes: `SimState` is genuinely
  fork-safe (serializable `rng_state`, tested round-trip including RNG
  state), `driver_integral_error_state` is already threaded so a resumed
  run doesn't restart the PI controller's integral from zero, and
  `sim.run_scenario(initial_state=...)` already resumes a full-fidelity
  run from any saved `SimState` (already tested,
  `test_fork_replay_from_saved_state_is_identical`). This milestone is
  wiring those existing primitives together, not inventing new ones.
  **Scoped into two milestones up front, deliberately not one**: this is
  v1, full-fidelity/per-second forking only. Tier-2/long-horizon
  (`long_horizon.simulate_long_horizon`) checkpoint-resume is a separate,
  deferred "Fork branching v2" — `LongHorizonCheckpoint` doesn't carry
  `rng_state` or enough schedule-position information to resume from, and
  `simulate_long_horizon` always regenerates its RNG and shuffled
  day-type schedule from `seed`, starting at day 0. Bundling v2 into v1
  risked repeating the Milestone-D-before-E scope mistake this project
  already corrected once; v2 gets its own plan review when it's next.
  **`ForkPoint.overrides`'s semantics, ratified via `docs/
  DESIGN_QUESTIONS.md` Q-0018**: a dotted path of 2 or 3 segments,
  `<top_level>.<leaf>` or `<top_level>.<sub_object>.<leaf>`, where
  `top_level` is one of `Scenario`'s four top-level objects (`profile` →
  `manufacturer_profile`, `climate`, `usage_profile`, `charging_profile`)
  and the final segment must resolve to a leaf value (a `Sourced[T]`'s
  `.value`, or a plain scalar) — never a list, dict, or further-nested
  model. No arbitrary-depth walking, no list/dict indexing. Both the
  profile axis and the climate/usage/charging axis are load-bearing: the
  first mistake-library entry ("Leaf in Guam") needs a climate fork, and a
  mid-run "vehicle moved to a different climate at year 10" fork needs to
  be an override applied at an arbitrary point, not a freshly-built
  scenario — ruling out both a battery-only (MC-v1-style) resolver and a
  profile-only one. The depth bound keeps validation to four known
  top-level schemas rather than an open-ended attribute walker.
  **`src/ev_thermal/fork.py`** (previously a stub) now has: `apply_override`/
  `apply_overrides` (the Q-0018 resolver, immutable — every `model_copy`
  returns a new object, parent untouched), `create_fork_point` (builds a
  `ForkPoint` with a deterministic content-hash `fork_id`, matching
  `causal.make_causal_event`'s already-established convention — a
  process-random `uuid4` would break fork-replay's exact-reproducibility
  premise the same way it would have for `CausalEvent.event_id`), and
  `branch` (applies a `ForkPoint`'s overrides to build a child `Scenario`,
  appends `fork_point.fork_id` to `fork_lineage` — the first code that
  ever populates it — and returns a child `SimState` ready to pass to
  `sim.run_scenario(initial_state=...)`).
  **A real correctness issue was caught before it shipped, not assumed
  away**: `SimState.state_id` is `f"{scenario_id}-step-{step_count:06d}"`,
  and `sim.py` always carries `state.scenario_id` forward unchanged across
  steps, never re-deriving it from whatever `Scenario` a later call
  happens to pass. Left alone, a forked branch and its parent's own
  un-forked continuation would produce IDENTICAL `state_id`s at the same
  `step_count` — a real collision, since `state_id`/`fork_lineage` are
  exactly how a caller is supposed to tell branches apart. `branch()`
  fixes this by giving the child a fork-namespaced `scenario_id`
  (`f"{parent_scenario_id}-fork-{fork_id}"`) and rewriting the returned
  state's own `state_id` to match — verified directly by
  `test_fork_of_a_fork_accumulates_lineage_and_diverges_state_ids`, which
  forks twice and confirms all three branches' final `state_id`s are
  distinct.
  **Found, flagged, not touched**: `Scenario.overrides` (`schema.py`) is a
  separate, pre-existing, still-unused field with narrower documented
  scope ("on top of `manufacturer_profile`" only) than what `ForkPoint.
  overrides` now supports. Not unified with the new resolver in this
  milestone — doing so would mean changing that field's own documented
  semantics, a second Q-NNNN-worthy decision this milestone didn't need to
  make to ship v1. Left exactly as it was; a future milestone can decide
  whether to wire it up, deprecate it in favor of the fork mechanism, or
  leave it alone.
  **Tests** (`tests/test_fork.py`, 9 total): one acceptance test per
  top-level object (`profile`/`climate`/`usage_profile`/
  `charging_profile`), one rejection test for the Q-0018 depth constraint
  (too many segments, a non-leaf field, an unknown top-level prefix), one
  `apply_overrides`-applies-every-entry test, and three behavioral
  acceptance tests: **genuine divergence** (forks mid-run with a climate
  override, proves the forked branch's peak node temps actually exceed the
  un-forked continuation's via the SAME already-validated hot-vs-cool
  mechanism `test_hot_ambient_produces_higher_peak_component_temps_than_
  cool` exercises — not a new physical claim, a proof that forking reaches
  it correctly), **non-mutation** (parent `Scenario`/`SimState` unchanged
  after a fork is created and continued), and **multi-generation lineage**
  (fork of a fork; `fork_lineage` accumulates root→leaf in order; all
  three branches' `state_id`s stay distinct). Full suite (251 tests),
  `mypy --strict`, and `ruff` all pass.
  **What's now unblocked:** the fork-branching engine exists. The mistake
  library — MistakeCase's `failure_profile`/`counterfactual_profile`
  pairing under a shared `Scenario` — is next; whether it actually uses
  `fork.py`'s mechanism (branching a shared baseline at a fork point) or
  stays two independently-constructed full runs is an open question for
  that milestone's own scoping, not decided here. Fork branching v2
  (Tier-2/long-horizon resume) remains deferred, its own future milestone.
- **Milestone "Tier-2 parked-period coolant-loop coupling" (done, 2026-09-25
  — design ratified via `docs/DESIGN_QUESTIONS.md` Q-0020):** surfaced
  while building the mistake library's first entry (below), not planned in
  advance. `simulate_long_horizon`'s parked-period calendar-aging
  relaxation (`relax_parked_temp_k`) routed exclusively through
  `resistance_to_ambient_k_per_w` — never through any `CoolingLoop` serving
  the battery, even when one exists (Milestone E2c's original design).
  Checked directly across all six real profiles: **four of six (Audi
  e-tron, Chevrolet Bolt, Kia Niro EV, Tesla Model 3) already have an
  active battery-serving `CoolingLoop`** with only a weak
  `resistance_to_ambient_k_per_w=2.0` fallback, so this fix changes real,
  already-published behavior for those four vehicles, not just a new
  counterfactual — including `examples/flagship_comparison.py`'s numbers
  and `tests/test_long_horizon_validation.py`'s E2d harness (Leaf + Model
  3). Scoped as its own milestone, not folded into the mistake library,
  once the blast radius was shown to and confirmed by the user.
  **Physics model**: `relax_parked_temp_k` gained four new OPTIONAL
  parameters (`capacitance_j_per_k`, `coolant_resistance_k_per_w`,
  `thermostat_temp_k`, `thermostat_full_open_temp_k`) — omitted at every
  pre-existing call site, so behavior everywhere else is byte-identical to
  before. When given, each step recomputes a thermostat-gated duty
  (`clamp((pack_temp_k - thermostat_temp_k) / (thermostat_full_open_temp_k
  - thermostat_temp_k), 0, 1)`, reusing the SAME linear ramp `cooling.py`'s
  real per-second model already uses, not a new placeholder or a hard
  on/off switch) and combines the direct ambient path with a duty-weighted
  coolant path (`resistance_to_coolant_k_per_w + 1/radiator_ua_w_per_k`,
  series, quasi-steady coolant node) as parallel conductances into that
  step's effective tau. New `_pump_soc_decrement` helper charges the pump's
  `pump_power_w` (scaled by the same duty) against SOC while gated on —
  per Q-0020's resolution, modeling the thermal benefit without this cost
  would repeat the exact shortcut Q-0004 already rejected for
  preconditioning. Only counts intervals at/after the day's charge-reset
  event, mirroring `_parked_soc_trajectory`'s own reset-step convention
  (intervals before the reset get wiped by it regardless of their own
  drain). `fan_power_w` is NOT charged — flagged simplification, parked
  coolant circulation approximated as pump-only.
  **A further finding, surfaced by the fix rather than resolved by it: a
  radiator-only coolant loop structurally cannot beat ambient temperature.**
  Re-running the mistake library's Leaf case after this fix landed showed
  NO change. Direct diagnosis (dumping the `pack_cell_temp_k` trajectory)
  showed neither the failure nor counterfactual profile ever exceeds ~306K
  over a full year in the Guam scenario — well below even a realistic
  thermostat threshold (checked against Tesla Model 3's own REAL researched
  battery-loop thermostat, 318.15K/45°C, higher than the counterfactual's
  308.15K, ruling out "the threshold was set too aggressively" as the
  explanation). Root cause: a radiator-based coolant loop can only reject
  heat TOWARD ambient, never below it, and the failure profile's pack
  already tracks within ~1K of Guam's ambient via the pre-existing ambient
  path — leaving little "excess heat above ambient" for any radiator loop
  to remove. The real-world hot-climate degradation this project's own
  research describes is driven mainly by SUSTAINED HIGH AMBIENT
  temperature itself (calendar aging's Arrhenius sensitivity to absolute
  cell temp, not to temperature above ambient), which only an active
  refrigerant CHILLER can address (can cool below ambient) — and
  `cooling.py`'s chiller path is explicitly unimplemented, a PRE-EXISTING
  deferred-work item from before this session (`CoolingLoop.
  chiller_present`/`chiller_capacity_w` exist in the schema but do
  nothing), not something this milestone introduced or is scoped to fix.
  **Decision: accept the small delta, document the physical limit, do not
  implement the chiller path as part of closing this milestone.** Chiller
  implementation would be a materially bigger, separately-scoped physics
  addition (affects the full per-second model, not just Tier-2) deserving
  its own plan review.
  **Re-validation, and the result was itself informative, not just a
  formality**: `examples/flagship_comparison.py`'s published 90-day numbers
  were re-run and came back UNCHANGED (Leaf/Guam 0.5828%, Model3/Seattle
  0.1194%, ratio 4.8827 — matching the existing record within rounding).
  This is the SAME physical-limit mechanism showing up a second,
  independent way: Seattle is milder than Guam, so if the thermostat gate
  barely engages in Guam, it engages even less in Seattle — Model 3's real
  battery-loop thermostat essentially never triggers in either climate
  under this scenario's conditions. No numbers in this file needed
  correcting. `tests/test_long_horizon_validation.py`'s E2d harness
  (`pytest -m slow`) still passes for both Leaf and Model 3 after the fix.
  **New tests** (`tests/test_long_horizon.py`): hand-computed single-step
  and duty-ramp tests for the parallel-resistance combination, a test
  proving `duty=0` below threshold reproduces the pre-fix function exactly,
  and tests for `_pump_soc_decrement`'s reset-boundary exclusion. Full
  suite (256 at the time), `mypy --strict`, and `ruff` all pass.
- **Milestone "Mistake library v1" (done, 2026-09-25 — design ratified via
  `docs/DESIGN_QUESTIONS.md` Q-0019):** the first working `MistakeCase`
  runner. `MistakeCase` itself was schema-only before this milestone — no
  runner, no content, no dashboard surface (still true: dashboard
  comparison views remain out of scope, unchanged from fork v1's own
  note).
  **Architecture — settled by `MistakeCase`'s own docstring, not a new
  decision**: "the only variable is the design decision being taught" rules
  out both `fork.py`'s mid-run branching (explores what-ifs from a live
  moment, not the point here) and pairing two genuinely different real
  vehicles (reintroduces the multi-variable confound this project has
  eliminated elsewhere — EFC normalization, shared aging coefficients,
  shared day-type mix). A `MistakeCase` is two independent
  `simulate_long_horizon` runs sharing everything (climate/usage/charging,
  via `Scenario.manufacturer_profile`'s nullability, which exists
  specifically for this) except one edited sub-tree of the profile. New
  `src/ev_thermal/mistake.py`: `run_mistake_case(case, day_type_mix,
  duration_years, seed) -> MistakeCaseResult` — pure orchestration, calls
  `characterize_all_trips`/`simulate_long_horizon` unmodified, once per
  profile, the same functions `examples/flagship_comparison.py` already
  calls, now formalized into a reusable core function. New
  `MistakeCaseResult` schema type (both full `LongHorizonRunResult`s plus
  the computed SOH delta) — computed once in core rather than left to every
  caller to re-derive, matching the precedent `mc.MCResult` already
  established for percentile computation (Q-0011).
  **First entry's content, ratified via Q-0019**: "Nissan Leaf: passive-air
  battery cooling vs. active liquid cooling." `counterfactual_profile` is
  the real `nissan_leaf.yaml` profile with exactly two changes — one added
  `CoolingLoop` serving `["battery_pack"]`, and
  `resistance_to_coolant_k_per_w` tightened from 0.5 to 0.02 (Tesla Model
  3's own real placeholder value for the same field, not invented fresh).
  Everything else stays byte-identical to the real Leaf. Constructed
  programmatically in `examples/mistake_library_leaf_battery_cooling.py`,
  not committed as a seventh YAML file, to keep it visibly a hypothetical
  derived from the real profile rather than a new "reference vehicle."
  **The published result is small, and that's the honest finding, not a
  bug to chase further**: `soh_delta = +0.0628` percentage points over 5
  years (failure 86.8207% SOH, counterfactual 86.8835% SOH; `uv run python
  -m examples.mistake_library_leaf_battery_cooling`). Building this case is
  what surfaced the parked-period coolant-coupling gap above; fixing that
  gap changed nothing for THIS specific case, because of the physical
  limit documented in that milestone's own entry (a radiator-only loop
  can't cool below ambient, and this scenario's real driver of degradation
  is sustained ambient temperature, not transient driving heat — that
  needs a chiller, a separate, larger, explicitly deferred piece of this
  simulator). Per the user's explicit decision: publish this number as-is,
  do not force a bigger one by expanding the counterfactual's scope beyond
  what Q-0019 ratified, and do not implement the chiller path to chase a
  more dramatic result. The lesson this case teaches is real and correctly
  directioned (active cooling never makes things worse, `soh_delta >= 0`
  always) — it's just smaller, in this simulator's current model, than the
  real-world "~20% loss after 12 months" citation might suggest, and that
  gap is now precisely attributed to a named, specific, already-flagged
  missing capability rather than left mysterious.
  **Tests** (`tests/test_mistake.py`): `run_mistake_case` produces
  internally consistent results (`soh_delta` matches the two final SOH
  values), and the counterfactual never ages worse than the failure case
  (`soh_delta >= 0`, the one property this case's lesson actually depends
  on holding regardless of magnitude). Full suite (258 tests), `mypy
  --strict`, and `ruff` all pass.
  **Deferred, not addressed here**: whether the mistake library's OWN
  content format should eventually move to YAML (matching
  `configs/profiles/*.yaml`'s convention) once there's more than one entry
  — not decided, since a single Python-constructed example doesn't yet
  justify a new content-loading pipeline. Real refrigerant-chiller physics
  in `cooling.py` (see the milestone above) is the single biggest lever
  for making a future hot-climate MistakeCase's delta match real-world
  magnitudes.
- **Milestone "Transition seam — gap 3 closed, first commercial-vehicle
  profile" (done, 2026-09-25):** the first concrete step toward the
  ev-thermal-sim ↔ Transition seam described in `docs/seam_reconnaissance.md`,
  following that memo's own gap-closure assessment (§6): close the
  cheapest gap (climate data) immediately, and scope the expensive gap
  (shared vehicle classes) to one real, well-documented commercial vehicle
  rather than both of Transition's vehicle classes at once. Per explicit
  instruction, this was executed without opening any `docs/DESIGN_
  QUESTIONS.md` entries — the profile-sourcing methodology was already
  established by the six passenger-EV profiles, and no genuinely novel
  design question (e.g. a thermal architecture the schema couldn't
  express) came up.
  **Gap 3 (Transition, real climate data):** `ClimateZone`
  (`Transition/src/types.ts`) gained three real fields —
  `meanAnnualTempF`/`seasonalAmplitudeF`/`dailyAmplitudeF` — populated with
  real NOAA 1991-2020 climate normals (hand-derived from published monthly
  high/low normals, not a single looked-up figure) for both presets:
  Columbus, OH (School Bus Depot / AEP Ohio) and Cedar Rapids, IA (Tank
  Wash / Alliant Energy). Stored in Fahrenheit, matching the source data
  and Transition's own audience, with the F→K conversion formula (and the
  "a temperature DIFFERENCE converts by ×5/9 only, no offset" caveat for
  the two amplitude fields) documented in the field's own doc comment for
  whoever eventually wires this into ev-thermal-sim's kelvin-based
  `Climate` schema. Not yet consumed by any Transition mechanics — same
  "added as data only" pattern already established for `targetElectrification
  RateByYear20`. **Could not be verified two ways this session had no
  access to**: Transition has no git repository (confirmed via `git
  status` before touching anything), so nothing was literally "committed"
  there, only written to disk; and this environment has no `node`/`npx`
  installed, so `tsc --noEmit` could not be run to confirm the new fields
  type-check, though they're simple, syntactically-unambiguous interface/
  object-literal additions.
  **Gap 1, scoped to one vehicle (ev-thermal-sim, first commercial-vehicle
  profile):** new `configs/profiles_commercial/` directory — deliberately
  NOT `configs/profiles/`, and NOT a "seventh" entry in `tests/
  test_profiles.py`'s `PROFILE_STEMS`. That file's own module docstring
  ("the six populated reference-vehicle profiles") and every "shared
  across all six profiles by design" note throughout this project
  (aging-coefficient sharing, the flagship comparison, the Monte Carlo
  record) are all scoped specifically to the six-passenger-EV comparison;
  quietly making a commercial vehicle its unannounced seventh member would
  either falsify those "six" claims or silently fold an uncontrolled new
  vehicle class into a comparison whose whole methodological point is
  controlling for everything except thermal architecture. New parallel
  track instead: `docs/research/commercial_profiles_v1.md` (its own
  versioned research document, same discipline as `profiles_v1.md` but
  explicitly not an addition to it) and `tests/test_profiles_commercial.py`
  (mirrors `test_profiles.py`'s checks — sourced-field completeness,
  gross≥usable kWh, positive motor specs, derate<max temp, cooling-loop
  serves-name matching, a 60s scenario smoke test — duplicated rather than
  imported, so the two tracks stay decoupled).
  **First entry: Blue Bird Vision Electric (194 kWh pack generation,
  2024+)**, a Type C electric school bus — chosen over the Lion Electric
  Lion C because Blue Bird's own dated (2025-03-17) spec sheet and its
  propulsion supplier's (Cummins/Accelera) separate BP97E battery-module
  datasheet together gave real, disclosed figures the Lion C's public
  documentation didn't (motor peak power/torque, pack voltage, operating
  temperature range, C-rate limits) — a materially stronger starting point
  for this project's per-field `Sourced[T]` methodology. Real, sourced
  data used directly: 194kWh pack (170.7kWh usable, 88% per Blue Bird's
  own figure), NMC/graphite chemistry, 620V nominal (583-689V range),
  −40°C to 50°C operating range, peak/continuous charge and discharge
  C-rates (all derived from the Accelera BP97E datasheet), 234.9kW/3254Nm
  peak motor output (315hp/2400ft-lbs per Blue Bird's spec sheet, TM4 SUMO
  motor family, exact variant undisclosed), liquid cooling (50/50 water/
  ethylene glycol) with an active refrigerant chiller shared across
  battery+motor+inverter via the Cummins PowerDrive 7000's single Thermal
  Management Module, and preconditioning capability (per Blue Bird's own
  marketing copy). Full details and every citation in `docs/research/
  commercial_profiles_v1.md`.
  **This is the first `chiller_present: true` profile in this project** —
  every one of the six passenger-EV profiles has `chiller_present: false`
  everywhere, so this profile is the first real vehicle CLAUDE.md's
  existing "chiller path is not implemented in cooling.py" deferred-work
  note (see the parked-period coolant-loop coupling milestone, above)
  actually affects. `chiller_capacity_w` is a placeholder (no numeric
  figure disclosed anywhere found) and currently has zero physics effect
  regardless, since `cooling.py`'s chiller path doesn't exist yet.
  **Two categories of field needed genuine re-derivation, not verbatim
  reuse of the shared six-profile placeholder — found and fixed before
  this landed, not left as latent bugs:**
  1. Vehicle-dynamics fields (`curb_mass_kg`, `frontal_area_m2`,
     `drag_coefficient`, `wheel_radius_m`, motor `mass_kg`) where the
     passenger-EV placeholder would be physically implausible for a
     33,000lb-GVWR bus (e.g. `curb_mass_kg: 1800.0` would make the
     simulated vehicle accelerate like a sports car) — each re-estimated
     from this vehicle's own real disclosed geometry/GVWR/tire size or
     generic published heavy-vehicle engineering figures, confidence: low,
     derivation shown in the YAML itself.
  2. The battery `ocv_curve_v`, which had to be linearly rescaled to this
     pack's real 583-689V range rather than reused verbatim from the
     shared 300-395V curve — verbatim reuse would have placed the pack's
     own documented `min_terminal_voltage_v` (583V) ABOVE the OCV curve's
     maximum (395V), an internal contradiction within the same profile,
     not just an unresearched placeholder. New regression test in `tests/
     test_profiles_commercial.py` (`test_battery_min_voltage_below_ocv_
     curve_max_and_max_voltage_above_ocv_curve_min`) guards against this
     class of bug recurring for this or any future commercial profile.
  Every other un-researched field (equivalent-circuit internals, all
  seven aging-rate coefficients, the charge-current-limit curve,
  control-strategy placeholders, most `AuxLoads` fields) is the SAME
  shared, non-vehicle-specific placeholder value used across all six
  passenger profiles, reused verbatim — consistent with, not a deviation
  from, the existing six profiles' own methodology.
  **Flagged explicitly, not silently inherited: the seven aging-rate
  coefficients were calibrated against two PASSENGER-EV fleet anchors
  (Leaf/Guam, Model3/Seattle) and have never been validated against any
  commercial/heavy-duty vehicle's chemistry or duty cycle.** Reusing them
  here follows this project's established "shared coefficients isolate
  thermal architecture as the sole variable" convention, but that
  convention's validity was established for six vehicles with broadly
  similar passenger-EV chemistries and duty cycles — extending it to a
  heavy-duty NMC/graphite pack on a school-bus duty cycle is untested.
  Every one of this profile's aging-coefficient fields says so directly in
  its own `source`/`note`. Do not treat this profile's SOH trajectory as
  validated the way the six passenger profiles' are, and do not use it in
  a published comparison without re-examining this gap first.
  **Validation:** `uv run pytest tests/test_profiles_commercial.py` (9
  tests, including a 60s `run_scenario` smoke test) passes; full suite
  (267 tests), `mypy --strict`, and `ruff` all pass unchanged.
  **What's still open, per `docs/seam_reconnaissance.md`'s own gap
  ranking:** gap 2 (Transition route duty-cycle detail) remains
  deliberately untouched — its payoff was already identified as gated
  behind gap 1, and it's revisited as part of this profile's actual
  integration into Transition, not populated speculatively ahead of that.
  Transition-side wiring (a `manufacturerProfileKey`-style field on
  `VehicleClass` pointing at this new profile, and the precomputed
  SOH-delta table interface itself, per the memo's §5 shape) is not part
  of this milestone — this milestone is the profile existing and being
  real, not yet the seam being consumed. The second Transition vehicle
  class (tank-wash truck) is explicitly deferred, per the original
  decision, until the seam is proven on the primary (school bus) case.
  See `docs/seam_reconnaissance.md`'s own §7 for whether the originally-
  proposed interface shape (§5) still holds now that gap 3 is closed and a
  real commercial profile exists.
- **Milestone "Blue Bird Vision Electric validation" (done, 2026-09-26):**
  closes the "smoke-tested only" gap the commercial-profile milestone left
  open — per explicit instruction, validated the same way the six
  passenger profiles were before anything built on top of it, not just
  the schema/60s-smoke-test bar `tests/test_profiles_commercial.py`
  already enforced.
  **New `examples/validate_blue_bird_vision_electric.py`** (the
  committed, reproducible runner, per this project's standing
  reproducibility convention): builds a real `Climate` (NOAA Columbus, OH
  normals — the same figures used for the Transition seam's gap-3
  closure, converted to kelvin, plus Columbus's real elevation and a
  placeholder solar-irradiance figure matching every other profile's
  convention), `UsageProfile` (annual mileage and one-way trip mean/std
  derived from Transition's real `SCHOOL_BUS_DEPOT_PRESET` route data —
  vehicle-count-weighted across its four real routes, not invented), and
  `ChargingProfile` (11kW L2 matching Transition's real per-vehicle
  charging-demand figure, `dc_fast_charge_fraction=0.0` since a
  depot-charged fleet has no real DC-fast-charge usage pattern) — none of
  which existed for this profile before, per `docs/ROADMAP.md`'s own
  "Transition seam" note. A `day_type_mix` (60/60/60 COLD_START_COMMUTE/
  HOT_SOAK_COMMUTE/CITY_STOP_AND_GO, no HIGHWAY, no FAST_CHARGE_SESSION)
  splits Transition's real ~180-school-days/year figure evenly — a
  reasonable, explicitly-flagged assumption for a first validation pass,
  not a researched usage claim.
  Three checks, matching how the six passenger profiles were actually
  validated (not just their own schema tests): (1) a 1800s single-fidelity
  `run_scenario` thermal-steady-state check — the exact class of check
  that originally caught the demo profile's thermal-runaway bug; (2) a
  real Tier-1 characterization (`characterize_all_trips`) across every
  implemented archetype; (3) a real 90-day Tier-2 `simulate_long_horizon`
  trajectory, cross-checked in order of magnitude against the passenger-
  fleet flagship record.
  **A real, pre-existing bug was found and fixed, affecting all seven
  profiles (six passenger + Blue Bird), not created by this milestone —
  the validation run's own causal-event log contradicted itself, which is
  what surfaced it.** `motor.py`/`inverter.py`'s `DERATE_TRIGGERED` causal
  event fired whenever a torque/current request was clamped, unconditionally
  attributing the cause to "winding/junction temp exceeded derate
  threshold" — even when the actual temperature was nowhere near that
  threshold and the real binding constraint was simply the flat
  `peak_torque_nm`/`peak_current_a` rating (the derate curve's own `scale`
  factor was exactly 1.0, meaning it wasn't the derate curve that bound at
  all). Caught directly: the Step-A validation run logged "Inverter
  junction temp 297.3K exceeded derate threshold 398.1K" — 297.3K is not
  greater than 398.1K, a literal, checkable contradiction in the event's
  own text. `battery.py` was checked and does NOT have this bug — its
  event generation already correctly gates each specific message
  (cold-derate, hot-derate, SOC-floor) behind that mechanism's own scale
  factor actually being `<1.0`, the correct pattern. `controller.py`'s own
  (authoritative) arbitration-layer events were also checked and are
  unaffected — they report the ceiling and the component's current
  temperature as context without asserting temperature caused the clamp,
  so they were never wrong, just less specific.
  **Fix**: both `motor.compute_motor_heat` and `inverter.compute_inverter_
  heat` now compute the thermal derate scale factor explicitly (still via
  the same `available_torque_nm`/`available_current_a` single-source-of-
  truth ceiling functions — not duplicated logic, just also inspecting
  *why* that ceiling landed where it did) and branch: `thermal_scale<1.0`
  keeps the existing `DERATE_TRIGGERED` event/message; `thermal_scale==1.0`
  (peak rating alone is binding) now emits a new, distinctly-named
  `TORQUE_LIMIT_TRIGGERED`/`CURRENT_LIMIT_TRIGGERED` event with an accurate
  description. No test asserted on the old message text (checked before
  changing it); full suite (267 tests), `mypy --strict`, and `ruff` all
  pass after the fix. This bug was invisible on the six passenger
  profiles not because they don't have peak-rating ceilings, but because
  none of their existing tests/examples happened to inspect the causal
  event *text* closely enough at a moment where a flat rating (not the
  derate curve) was the actual binding constraint — the same "found only
  by actually trying it" pattern this project's validation discipline
  keeps surfacing.
  **A second, profile-specific finding, same category as the vehicle-
  dynamics fields the original commercial-profile milestone already had
  to re-derive**: `inverter.peak_current_a` had been left at the shared
  six-profile passenger-EV placeholder (500.0A) under the "no basis to
  adjust" reasoning applied to most un-researched inverter fields — this
  validation run proved that reasoning wrong for this specific field.
  A plain 1800s cruise at constant speed produced real torque requests
  implying up to 871A during the low-speed portion of an ordinary
  ramp-up (a 33,000lb bus needs real current to move; this motor's real
  peak rating, 234.9kW/3254Nm, is far above any of the six passenger
  profiles'), continuously clamping current for the run's first ~40
  seconds — a gross implausibility of exactly the kind this validation
  pass exists to catch, not a subtle one. Re-estimated at 1000.0A (~15%
  headroom above the observed peak request), same "domain-appropriate
  estimate, not verbatim placeholder reuse" treatment already applied to
  this profile's `curb_mass_kg`/motor `mass_kg`/etc. — confirmed by
  re-running the validation script after the change: limit/derate events
  during the 1800s check dropped from 15 to 0, node temperatures were
  unchanged (40.2°C/42.4°C/26.1°C/33.2°C motor/inverter/battery/coolant),
  and the 90-day SOH trajectory moved only trivially (0.5292% → 0.5306%
  total loss) — confirming the fix changed only the spurious clamping,
  not the underlying thermal/aging physics.
  **Results** (both the causal-event fix and the `peak_current_a` fix
  applied): Step A — thermal steady state reached quickly and stays well
  inside every derate threshold (motor 40.2°C, inverter 42.4°C, battery
  26.1°C, coolant 33.2°C), zero limit/derate events at a plain 30mph
  cruise, energy balance holds (positive implied mechanical work). Step B
  — all 4 implemented archetypes (plus fast-charge characterization)
  produce finite, ambient-sensitive peak cell temperatures across all 4
  climate buckets. Step C — 90-day SOH trajectory is finite and
  monotonically non-increasing; total loss (0.5306%, calendar-dominated at
  0.5172%) sits in the same order of magnitude as the passenger-fleet
  flagship record (Leaf/Guam 0.5828%, Model3/Seattle 0.1194% over the same
  90 days) — plausible, not identical, exactly as expected for a
  different vehicle class under a different (Columbus, OH) climate.
  **What this does and does NOT establish**: this profile now clears the
  same physical-plausibility bar the six passenger profiles cleared before
  anything was built on top of them (finite, non-pathological, thermally
  bounded, order-of-magnitude-sane). It does NOT mean this profile's
  `UsageProfile`/`ChargingProfile` are canonical or ready for a published
  Transition-seam comparison — they were built for this validation pass
  specifically (see `docs/ROADMAP.md`'s "Transition seam" section), with
  `typical_start_soc`/`typical_end_soc` explicitly reusing the flagship
  script's own convention rather than being tuned to this vehicle's real
  ~150-mile range, and the `day_type_mix`'s 60/60/60 split being a
  reasonable assumption, not a researched usage claim. It also does NOT
  mean the seven aging-rate coefficients are validated for this vehicle
  class — that gap (flagged in the original commercial-profile milestone)
  is unaffected by this validation pass and remains open.
- **Milestone "Chiller path implementation" (done, 2026-09-25, per
  `docs/DESIGN_QUESTIONS.md` Q-0021):** closes the single most-repeated
  open item in this project's history — `CoolingLoop.chiller_present`/
  `chiller_capacity_w` finally do something. Full plan reviewed and
  approved before implementation (Q-0021); built exactly as scoped there.
  **What was built:**
  1. **Schema**: `CoolingLoop.chiller_power_w` (electrical draw at full
     duty — `chiller_capacity_w` alone only says how much heat it can
     remove, not what it costs). `CoolingStepResult.chiller_heat_removed_w`/
     `chiller_power_w`. `ControllerStepResult.commanded_chiller_duty`.
     `EnergyTotals.chiller_rejected_j`/`cooling_parasitic_j`.
  2. **`cooling.py`**: `step_cooling` gains `chiller_duty`. Chiller heat
     removal is a separate additive term, not folded into the radiator's
     epsilon-NTU calculation — the one mechanism that can pull coolant
     BELOW ambient, gated only on nonzero coolant flow (not routed through
     the thermostat bypass valve, since a real chiller sits on its own
     branch of the TMM).
  3. **`controller.py`**: `commanded_chiller_duty` per loop is a genuine
     SECOND STAGE — ramps against `thermostat_full_open_temp_k` (where the
     radiator/fan path is already saturated), not a scaled copy of fan
     duty, per Q-0021's approved design.
  4. **`sim.py` — a pre-existing gap fixed as an in-scope prerequisite**:
     scoping this surfaced that `pump_power_w`/`fan_power_w` were already
     computed by `step_cooling` but never charged to the battery or
     tracked in `EnergyTotals` in the full per-second model —
     `battery_current_request_a` was computed BEFORE `step_cooling` ran.
     Fixed by moving cooling ahead of the battery step in both
     `advance_one_step` and `advance_charging_step` (step_cooling depends
     only on `controller_result` and start-of-step state, so this changes
     no physics, just sequencing) and folding pump+fan+chiller draw into
     the battery's actual current request — closing the exact "models the
     effect without the mechanism" gap Q-0004/Q-0020 already rejected for
     this shape of problem. `advance_charging_step` nets cooling parasitic
     draw against the charger's power budget too (real fast-charging runs
     active thermal management) but deliberately does NOT extend to
     aux/HVAC draw during charging, a separate, pre-existing, untouched
     simplification (new `docs/ROADMAP.md` item).
  5. **`long_horizon.py`**: `relax_parked_temp_k` gained a chiller-aware
     term for Tier-2's parked model. NOT another parallel conductance like
     Q-0020's coolant-path addition (a conductance can only speed
     convergence toward ambient, never past it) — modeled as a constant
     duty-scaled heat-extraction rate that shifts the interval's
     relaxation TARGET itself (`ambient - Q/G`), matching cooling.py's own
     constant-heat-sink treatment. New `_chiller_soc_decrement` mirrors
     `_pump_soc_decrement`'s energy-cost-consistency treatment exactly.
     **A design question resolved during implementation, not before**:
     since `thermostat_full_open_temp_k > thermostat_temp_k` always, the
     coolant conductance is already saturated (duty=1.0) by the time the
     chiller's own duty becomes positive — a G=0-while-chiller-active edge
     case is mathematically unreachable given this ordering, confirmed
     analytically and by direct testing, so no special-case handling for
     it was added (would have been dead code).
  **Validation**: full suite (282 tests, up from 269), the two
  `@pytest.mark.slow` cases, `mypy --strict`, and `ruff` all pass. New
  targeted tests: `test_cooling.py` (chiller below-ambient removal,
  no-flow gating, linear duty scaling), `test_controller.py` (second-stage
  gating against `thermostat_full_open_temp_k`, zero for
  `chiller_present=False`), `test_long_horizon.py` (chiller settles below
  ambient directionally + one hand-computed single-step closed-form
  check), `test_sim.py` (end-to-end: chiller engages and cools below
  ambient in a full-fidelity run; the existing energy-balance test was
  tightened to include `cooling_parasitic_w`, since it was passing "by
  accident" without it once that draw became real).
  **Re-validation of previously-published numbers**: `flagship_comparison.py`
  (90-day and 20-year, neither passenger profile has a chiller) moved by
  <0.02 percentage points in every case — confirms the pump/fan
  energy-accounting fix, while a real correctness improvement, doesn't
  invalidate any existing published passenger-fleet number. The mistake
  library's Leaf case similarly moved by <0.001pp (+0.0628pp → +0.0626pp)
  — expected, since neither its failure nor counterfactual profile has a
  chiller either.
  **The Blue Bird profile's own numbers** (the only `chiller_present:
  true` profile): 90-day total loss 0.5306% → 0.5309%; 20-year 57.3854% →
  57.3981% — both changes are noise-level, NOT the meaningful reduction
  the chiller was built to provide. `chiller_power_w: 2000.0` (placeholder,
  derived from `chiller_capacity_w` at an assumed automotive-refrigerant
  COP of ~2.5, documented in the YAML) was added so the chiller's own
  draw is real, not free.
  **The actual finding, surfaced rather than chased toward a bigger
  number**: direct investigation (comparing chiller-on vs. chiller-off
  Tier-1 characterizations for the SAME trip, and separately under a
  synthetic Guam-like climate for Blue Bird) confirmed the chiller
  mechanism itself is correct — it engages and produces real below-ambient
  cooling in a direct `sim.py`-level test with an artificially hot
  ambient — but it does NOT engage in ANY currently-tested real scenario,
  including Blue Bird under a Guam-like climate, because the coolant
  loop's own temperature never crosses the shared placeholder
  `thermostat_full_open_temp_k` threshold (40°C, the same value used by
  every actively-cooled profile). Two compounding reasons, both confirmed
  directly rather than assumed: (1) Tier-1's hot-soak initial condition
  (`characterize_trip`) only patches the BATTERY node's starting
  temperature, not the coolant node's — so even a trip that starts with a
  44°C-hot-soaked cell begins with coolant still near ambient, and the
  trip's own duration (1200s for `HOT_SOAK_COMMUTE`) isn't long enough for
  that heat to raise coolant past 40°C (measured coolant range: 35.8-38.3°C
  for a 36°C-ambient-bucket hot-soak trip, never crossing the threshold).
  (2) Under sustained parked conditions (Tier-2), Q-0020's own earlier
  finding already established that a radiator-coupled pack under Guam-like
  ambient tracks within ~1K of ambient (~30-33°C) — nowhere near 40°C
  either. **Not tuned away**: the threshold, the hot-soak initialization
  convention, and the climate scenarios tested are all pre-existing,
  independently-motivated choices, not adjusted to produce this result.
  **What this milestone does and does NOT establish**: the chiller path is
  now real, physically correct, and unit-tested machinery — no longer the
  "does nothing" placeholder CLAUDE.md described. It does NOT yet visibly
  change the BMW i3-vs-Leaf caveat (out of scope — Milestone D, a
  different schema abstraction) or make Blue Bird's published numbers
  meaningfully less conservative under any climate tested so far. Whether
  that requires a lower/re-derived `thermostat_full_open_temp_k` (currently
  an unsourced shared placeholder, same status it always had), a coolant-
  node hot-soak initialization fix, or a genuinely hotter test climate than
  Guam's own mild seasonal amplitude is an open question for whoever next
  wants the chiller to matter for a specific comparison — not decided or
  guessed at here.
- **Milestone "Blue Bird Vision Electric — 20-year point estimate" (done,
  2026-09-25):** the follow-on the prior milestone deliberately left open —
  that pass validated physical plausibility over 90 days only. This
  milestone runs the same already-validated `Climate`/`UsageProfile`/
  `ChargingProfile` (unchanged; see the prior milestone's entry above) out
  to a full 20-year unperturbed Tier-2 point estimate, the same class of
  check already done for the Nissan Leaf/Guam reference case (see the
  "20-year unperturbed Leaf/Guam point estimate" entry above), so this
  profile's numbers can be sanity-checked against a real anchor rather than
  judged in isolation.
  **Mechanical change**: `examples/validate_blue_bird_vision_electric.py`'s
  `DURATION_DAYS` module constant became `DEFAULT_DURATION_DAYS` plus a
  `--duration-days` CLI flag on Step C only (Steps A/B are
  duration-independent), mirroring `examples/flagship_comparison.py`'s
  existing convention rather than inventing a new one. Reproduce via
  `uv run python examples/validate_blue_bird_vision_electric.py
  --duration-days 7300` — 7300 (20 x `long_horizon.DAYS_PER_YEAR`, the
  module's own integer 365-day year), not 7305 (20 x 365.25): the script's
  `duration_years = ceil(duration_days / 365.25)` sizes the Tier-2 schedule
  in units of `long_horizon.DAYS_PER_YEAR`, so 7305 requested against a
  `duration_years=20` schedule (only 7300 days long) throws `IndexError` —
  caught immediately by actually running it, not a latent bug left
  unfixed. If a future duration request lands exactly on this boundary
  again, round down to a `DAYS_PER_YEAR`-multiple, not up.
  Re-ran the existing 90-day check first to confirm no regression from the
  CLI change: numbers reproduced exactly (0.5306% total loss, 0.5172%
  calendar, 0.0134% cycle), byte-for-byte matching the prior milestone's
  recorded values.
  **20-year (7300-day) result:** 57.3854% total SOH loss (42.6146% SOH
  remaining), calendar-dominated (56.1353% calendar, 1.2501% cycle) —
  finite, monotonically non-increasing, not degenerate (nowhere near 0% or
  100%), not a suspiciously round number.
  **Cross-check against the real Leaf/Guam 20-year anchor, run for direct
  comparison rather than assumed
  (`uv run python examples/flagship_comparison.py --duration-days 7300`):**
  | | total | calendar | cycle |
  |---|---|---|---|
  | nissan_leaf/Guam | 52.7209% | 49.2861% | 3.4348% |
  | tesla_model3/Seattle | 14.3439% | 14.1514% | 0.1926% |
  | blue_bird/Columbus | 57.3854% | 56.1353% | 1.2501% |

  Same order of magnitude as the passenger fleet's own 20-year numbers, as
  expected — but the pre-stated expectation going into this run (see the
  jump-start brief this milestone was scoped under) was specifically
  "expect SLOWER calendar aging than Leaf/Guam" (Columbus's 11.89 C mean is
  dramatically milder than Guam's near-constant ~30 C, and this profile has
  real active liquid cooling with a chiller-capable loop vs. the Leaf's
  passive air-cooled pack) and "likely MORE cycle aging... given the duty
  cycle." **Both directions came back backwards**: Blue Bird's calendar
  loss (56.14%) is higher than Leaf/Guam's (49.29%), and its cycle loss
  (1.25%) is lower, not higher, than Leaf/Guam's (3.43%). Per this
  project's explicit instruction not to chase a bigger number or tune
  toward a "correct-looking" result, this is reported as the finding
  itself, not adjusted or explained away.
  **Most likely mechanism, evidence-grounded but not a formal derivation**
  (same epistemic standing as the MC p50-vs-point-estimate mechanism
  above, which this shares real structure with): SOC is not the
  differentiator — both scenarios use the same 0.5/0.8 start/end SOC
  convention (`flagship_comparison.py`'s `seattle_climate`/`guam_climate`
  callers and this profile's `school_bus_charging_profile` both do), and
  Blue Bird's own checkpoint SOC (~0.798, both day 0 and day 7299) sits in
  the same range as Leaf/Guam's already-documented ~0.78-0.79 trajectory.
  The real difference is climate SHAPE, not level: Guam's seasonal
  amplitude is 2.0 K and daily amplitude 4.0 K (near-constant ~30 C
  year-round); Columbus's are 12.5 K and 5.11 K respectively (Step B's own
  ambient buckets span -5.7 C to 29.5 C — i.e. Columbus's HOT extreme
  reaches roughly the same absolute temperature as Guam's near-constant
  level, it just also has a cold half Guam never sees). `aging.py`'s
  calendar term is `exp(calendar_aging_activation_temp_k * (1/reference -
  1/T))`, an Arrhenius exponential — convex in T — evaluated per-step and
  summed, exactly the same convexity structure already identified as the
  root cause of the MC p50-vs-point-estimate gap growing with horizon
  (this file's own "Multi-year Monte Carlo v1" entry). Under that
  convexity, a climate whose hot half reaches Guam-like temperatures while
  its cold half drops well below it does NOT average out to "milder than
  Guam" in aging-rate terms — the hot days contribute disproportionately
  more rate than the cold days subtract, a real physical effect (real
  cells do age faster in climates with hot summers even at a lower annual
  mean), potentially compounded by the calendar-aging anchors themselves
  never having been calibrated against ANY climate with an amplitude this
  large (Guam: 2K seasonal; Seattle: 8K; both anchors were constant-
  temperature single-condition benchmarks with zero amplitude at all —
  see `aging.py`'s own "Coefficient provenance" section). This is a
  sharper, climate-amplitude-specific manifestation of the already-known
  "calendar aging's linear-in-time formulation is a known extrapolation
  limit past its calibration anchors' 5-10 year window" gap
  (`docs/ROADMAP.md`'s "Aging model fidelity" section, Q-0017), not a new
  independent bug — but concretely worse than that gap's previously-known
  shape, since it's now shown to bite even a MILDER-mean, better-cooled
  vehicle harder than a hotter, passively-cooled one, purely from seasonal
  swing width. **Not investigated further or fixed in this milestone** —
  per explicit instruction, surfacing this is the deliverable; re-deriving
  or bounding the aging coefficients against a realistic seasonal-amplitude
  range is real future work, added to `docs/ROADMAP.md`.
  The cycle-aging shortfall (1.25% vs. Leaf/Guam's 3.43%, the opposite of
  the "more cycle aging given the duty cycle" expectation) was not
  separately investigated — plausible unforced explanation: Blue Bird's
  day_type_mix has no `HIGHWAY` or `FAST_CHARGE_SESSION` occurrences (see
  the prior milestone's entry), both meaningfully higher-C-rate archetypes
  than the three it does use, so the "given the duty cycle" premise itself
  may not hold the way it was stated going in — flagged, not chased.
  **What this does and does NOT establish**: confirms this profile's
  long-horizon trajectory is finite and non-degenerate out to 20 years,
  the same bar the Leaf/Guam anchor cleared. It does NOT make this
  profile's 20-year number more trustworthy than the 90-day one for
  publication — if anything it surfaces a NEW reason for caution specific
  to large-seasonal-amplitude climates, on top of the already-open
  aging-coefficient-not-validated-for-this-chemistry gap and the
  radiator-only/no-chiller-physics conservatism gap. All three caveats
  now travel together with any Blue Bird SOH number cited beyond 90 days.
- **Milestone "Q-0016 resolution — Tier-2 validation reference generator
  cadence fix" (done, 2026-09-25):** the deferred, non-blocking follow-up
  `docs/DESIGN_QUESTIONS.md` Q-0016 flagged when the parked-SOC model
  milestone landed — `test_long_horizon_validation.py`'s
  `_generate_tier1_reference_soh` still patched SOC to `typical_end_soc`
  every hour (the pre-Q-0012 constant-SOC-snapping assumption), while
  `simulate_long_horizon` had already moved to a real per-hour-decay,
  one-reset-per-day model. Picked up as a well-scoped, non-architecture-
  touching piece of technical debt (decided and documented per the
  autonomy grant, not escalated — fixing a stale test reference to match
  its own already-shipped system-under-test doesn't change any term's
  meaning or schema semantics).
  **Fix:** `_generate_tier1_reference_soh` now applies exactly one
  instantaneous SOC reset per day, snapped to the parked sub-step boundary
  crossing `charging_profile.charge_reset_hour` — the same reset condition
  `_parked_soc_trajectory` uses (`t_before < charge_event_s <= t_after`),
  not an independently re-derived approximation of it. Between resets, SOC
  is whatever `sim.py`'s own real per-second physics produces, not
  artificially held constant.
  **Confirmed, not assumed: Q-0016's own prediction that this fix alone
  would not close the gap.** A second, independent residual — `sim.py`
  applies its full `aux_current_a` (`hvac_power_w + accessory_load_w`)
  unconditionally while parked, while Tier-2's `_parked_soc_trajectory`
  decays SOC using `accessory_load_w` alone — remained, measured directly
  rather than estimated, same 60-day/seed=123 window both profiles were
  already validated at:
  | profile | tier1_loss | tier2_loss | abs diff | relative |
  |---|---|---|---|---|
  | Leaf | 0.276% | 0.399% | 0.122% | 44.2% |
  | Model 3 | 0.355% | 0.390% | 0.035% | 9.9% |

  Direction is mechanism-consistent: the reference's now-real (lower,
  sawtooth) average parked SOC ages calendar-wise slower than Tier-2's own
  assumed trajectory, so `tier1_loss < tier2_loss` in both cases — exactly
  what the aux-draw residual predicts, not an unexplained discrepancy.
  **Test's absolute floor widened from 0.001 to 0.002** to accommodate this
  known, mechanistically-understood residual with margin, documented in
  both the test's own docstring (with the measured numbers on record) and
  the tolerance line's own comment — not picked to make this specific run
  pass. Full suite (267 tests), the two `@pytest.mark.slow` cases run
  explicitly, `mypy --strict`, and `ruff` all pass; the five pre-existing
  `mypy --strict` errors surfaced when checking this file's own import
  graph (`test_cooling.py`/`test_motor.py`/`test_inverter.py`/
  `test_battery.py`/`test_controller.py`) were confirmed pre-existing on
  `main` via `git stash`, not introduced by this change.
  **What's still open:** closing the aux-draw residual for real means
  making `sim.py`'s aux-draw driving-state-aware, a real core-physics
  change out of scope for a test-harness fix — added to `docs/ROADMAP.md`'s
  "Charging model gaps" section as its own deferred item, not attempted
  opportunistically here.
- **Milestone "Wire in `BatterySpec.min_charge_temp_k`" (done,
  2026-09-25):** closes the ROADMAP's own "schema-only fields not yet read"
  item for this field — the profile-population milestone added it
  specifically to represent the BMW i3's real, sourced asymmetric range
  (research: "charge 0-60C, discharge -25-60C") but left it unread,
  `available_charge_current_a` using the single `min_operating_temp_k`
  (the DISCHARGE-side cutoff, -25C/248.15K for the i3) for both directions
  in the meantime. Picked up as an already-fully-specified, non-
  architecture-touching fix (the exact wiring was already written down in
  `docs/ROADMAP.md`'s own entry), decided and documented per the autonomy
  grant rather than escalated.
  **Fix**: new private `battery._min_temp_cutoff_k(spec, is_discharge)` —
  discharge always returns `min_operating_temp_k`; charge returns
  `min_charge_temp_k` when set, else falls back to `min_operating_temp_k`
  (today's symmetric behavior for every profile except the i3, unchanged).
  Wired into `available_charge_current_a`'s hard-cutoff term and into
  `compute_battery_electrical_step`'s `below_min_operating_temp`/causal-
  event logic — the causal event's own description now names whichever
  field actually bound (`min_charge_temp_k` vs. `min_operating_temp_k`),
  the same "don't mislabel the actual binding constraint" discipline the
  Blue Bird validation milestone's `DERATE_TRIGGERED` fix already
  established for motor/inverter.
  **Confirmed by direct numeric check, not assumed**: the i3's
  `available_charge_current_a` is now 0.0A at and below 0C (273.15K, the
  charge-side cutoff) and non-zero above it, while
  `available_discharge_current_a` remains non-zero all the way down to
  -24C, unaffected — cutting off 25K higher for charge than discharge,
  matching the real asymmetric range this field exists to represent. No
  other profile is affected (`min_charge_temp_k: null` for all six other
  YAMLs) and no previously-published number used the i3 as a default
  profile (`bmw_i3.yaml` only ever appeared as example `--profile-a`/
  `--profile-b` CLI-flag usage text, never a default run) — this is a
  purely additive correctness fix, not a revision to any recorded result.
  New regression tests in `tests/test_battery.py`
  (`test_min_charge_temp_k_none_keeps_symmetric_cutoff_with_min_operating_
  temp_k`, `test_min_charge_temp_k_set_cuts_off_charge_higher_than_
  discharge`). Full suite (269 tests), `mypy --strict`, `ruff` all pass;
  the one pre-existing `mypy --strict` error in `tests/test_battery.py`'s
  own `_step` helper (missing return annotation) was confirmed present on
  `main` before this change, not introduced by it.
- **Milestone "Hot-soak coolant initialization fix" (done, 2026-09-25, a
  direct follow-up to the chiller-path milestone above, per user
  request):** the chiller milestone's own finding was that the chiller
  never engages in ANY currently-tested scenario because the coolant loop
  never crosses the engagement threshold — this milestone tests the first
  of the three candidate causes that milestone identified (a coolant-node
  hot-soak initialization gap), without touching the other two (the
  threshold value itself, or the test climates).
  **The gap**: `characterize_trip`'s hot/cold-soak initial condition
  (`BatterySpec.hot_soak_offset_k`/`cold_soak_offset_k`) patched only the
  battery node's own initial temperature — a HOT_SOAK_COMMUTE trip started
  with a pre-heated cell but its coolant loop still at the pinned
  climate's bare ambient, so a coolant-gated mechanism (the chiller's own
  second-stage duty, or the radiator/fan ramp) had no way to see the soak
  condition during a short trip's own duration.
  **Fix**: new `long_horizon._soak_patched_temps_k` helper (extracted from
  `characterize_trip` for direct testability) also patches any coolant
  node(s) serving the battery to the SAME `initial_pack_temp_k` value —
  no new offset invented, since the coolant loop's thermal mass is far
  smaller than the pack's, so assuming it has equilibrated toward the
  pack's own soaked temperature during a long parked soak is the more
  defensible approximation. For the "none" soak case this is a no-op
  (`initial_pack_temp_k` already equals ambient), so one code path
  handles all three cases. Motor/inverter are deliberately untouched,
  matching `apply_trip_start_offset`'s own established precedent that
  they aren't part of the tracked soak condition at all.
  **Confirmed the mechanism now activates, by direct measurement, not
  assumption**: for Blue Bird's `HOT_SOAK_COMMUTE` archetype at its
  hottest (36C) ambient bucket, per-step time-series inspection showed
  `commanded_chiller_duty` reaching 0.267 (previously exactly 0.0 at every
  step, confirmed in the chiller milestone's own investigation) and
  `chiller_heat_removed_w` reaching 1333W before decaying as the coolant
  cooled within the trip's own 1200s duration.
  **The actual re-validation result — reported as measured, not the
  direction expected going in**: total SOH loss INCREASED slightly rather
  than decreased, for both Blue Bird (90-day: 0.5309% -> 0.5328%; 20-year:
  57.3981% -> 57.9778%) and, more substantially, Tesla Model 3 in the
  flagship comparison (90-day: 0.1194% -> 0.1209%; 20-year: 14.3506% ->
  14.8429%, a real, non-noise-level ~3.4% relative move on a previously-
  published number, not the passenger-fleet comparison's structural ratio,
  which held: 3.6747 -> 3.5528). Nissan Leaf/Guam was unaffected in both
  runs (its battery is passively air-cooled, no coolant loop serves it, so
  this fix is a no-op for it). The mistake library's Leaf-vs-counterfactual
  case moved in the same direction: soh_delta dropped from +0.0626pp to
  +0.0543pp.
  **Why the direction is correct, not a red flag**: before this fix, every
  actively-cooled profile's coolant was UNREALISTICALLY COOL during a
  hot-soak trip's own duration (still at bare ambient rather than reflecting
  the soak), which was itself quietly giving those profiles an unearned
  cooling assist relative to a hot-soaked vehicle's real coolant state.
  Fixing that (making coolant correctly start hot) increases aging by more
  than the newly-added chiller can claw back during a single short trip —
  the two fixes are correct in combination even though their NET effect on
  this specific archetype is a small increase, not the decrease a naive
  "we added a chiller" intuition would predict. Not tuned toward either
  direction; reported as measured.
  **What this does and does NOT establish**: confirms the chiller
  mechanism can now engage under at least one real (not synthetic) tested
  condition, closing one of the three candidate causes the chiller
  milestone identified. It does NOT mean the chiller's effect is large
  enough to matter for any published comparison yet (Blue Bird's own
  numbers moved by less than 0.02pp / 0.6pp respectively — the coolant
  cools back down within the trip's own short duration faster than the
  chiller can meaningfully offset the now-correctly-modeled hot start).
  Whether a genuinely hotter test climate or a re-derived engagement
  threshold would change that further remains exactly as open as the
  chiller milestone left it.
  **A flawed first version of the end-to-end regression test was caught
  and rewritten before landing, not after**: the obvious assertion
  (`characterization.phases[0].mean_cell_temp_k > ambient_bucket_k`) was
  checked directly against a deliberately-reverted copy of the fix and
  found to PASS regardless — it's trivially true from the battery-only
  patch alone, so it never actually exercised the coolant side of the fix
  at all. Replaced with a self-contained comparison
  (`test_hot_soak_coolant_patch_lets_chiller_engage_where_battery_only_
  patch_would_not`) that builds both a battery-only-patched state and a
  battery-and-coolant-patched state from the same base and asserts the
  chiller engages in the latter only — a real regression check, not a
  restatement of an already-guaranteed fact.
  New tests: `test_soak_patched_temps_k_patches_battery_and_its_serving_
  coolant_loop`, `test_soak_patched_temps_k_ignores_a_loop_that_does_not_
  serve_the_battery`, `test_hot_soak_coolant_patch_lets_chiller_engage_
  where_battery_only_patch_would_not`. Full suite (285 tests, up from
  282), both `@pytest.mark.slow` cases, `mypy --strict`, `ruff` all pass.
- **Milestone "Hot climate chiller investigation" (done, 2026-09-25, the
  second of the three candidate causes the chiller milestone identified —
  a genuinely hotter test climate than Guam's mild seasonal amplitude —
  tried directly, at the user's request):** new
  `examples/chiller_hot_climate_investigation.py`, the committed,
  reproducible runner.
  **Real, sourced climate, not invented**: NOAA NCEI 1991-2020 normals for
  Phoenix Sky Harbor Intl AP, AZ (station USW00023183), same derivation
  convention already used for Blue Bird's own Columbus, OH climate —
  annual mean 75.6°F, hottest month (July) mean 95.5°F, coolest month
  (January) mean 56.8°F, average monthly diurnal range 22.95°F. Genuinely
  different SHAPE from the Guam-like test climate, not just a hotter
  version of it: Phoenix's annual mean (24.2°C) is actually LOWER than
  Guam's (30°C, Phoenix has real winters), but its amplitude is far
  larger (seasonal 10.75K, daily 12.75K vs. Guam's 2K/4K), so its implied
  peak ambient (47.7°C) is well above both Guam's (36°C) and the chiller's
  40°C engagement threshold, where Guam's peak never got close.
  **Result, in two parts, found by actually running it rather than
  reasoning about it in the abstract:**
  1. **The chiller DOES engage, dramatically, confirmed by direct
     time-series inspection**: `commanded_chiller_duty` reaches 1.0 (full
     5000W draw) throughout Blue Bird's `HOT_SOAK_COMMUTE` archetype at
     the hottest (47.7°C) ambient bucket — closing this candidate cause
     with a real, positive answer, not another null result like Guam's.
  2. **But even running at full capacity for the entire ~1200s trip, the
     chiller only shaves 0.03-0.35K off the battery's own mean cell
     temperature** (measured phase by phase: warm_up 0.025K,
     cruise_and_stops 0.187K, final_decel 0.350K) — because
     `chiller_capacity_w` (5000W, a placeholder already flagged in Blue
     Bird's own YAML as "likely undersized for a bus-size battery pack,"
     see the original commercial-profile milestone) is small relative to
     the pack's own thermal capacitance (990,000 J/K): even a full
     6,000,000J of chiller work over the trip's duration is a small
     fraction of what it would take to move a thermal mass that large by
     a meaningful amount in 20 minutes. The resulting 1-year Tier-2
     aggregate SOH effect is correspondingly negligible: 0.00027818
     percentage points absolute (3.51090196% total loss with the chiller
     vs. 3.51118014% without), 0.007923% relative.
  **This is a materially more specific finding than "not tested hot
  enough," and closes the "hotter test climate" candidate cause with a
  real, evidence-based answer rather than leaving it untried**: the
  remaining blocker on making the chiller matter for a published
  comparison is `chiller_capacity_w`'s own magnitude, not chiller
  engagement timing, the coolant/threshold interaction (both already
  fixed and confirmed working), or climate choice. Re-deriving that
  placeholder against a real bus-scale automotive chiller spec (not
  attempted here — no such source was found during the original profile
  research either) is the natural next candidate cause, not yet tried.
  Not tuned toward a bigger effect at any point in this investigation —
  the negligible result is reported exactly as measured.
  Full suite (285 tests), `mypy --strict`, `ruff` all pass unchanged (this
  milestone added one new example script, no source or test changes).
- **Milestone "Source real chiller_capacity_w data for a Type C bus"
  (done, 2026-09-25, the third and final candidate cause from the chiller
  milestone, tried at the user's explicit request):** closes the one
  remaining open item from the hot-climate investigation above — the
  5000W `chiller_capacity_w` placeholder was never re-derived against any
  real source, just carried as an unmotivated "automotive-scale" guess.
  **What was checked, not found**: the PowerDrive 7000's OWN chiller
  capacity remains undisclosed by Cummins/Accelera/Blue Bird in every
  source directly checked this milestone — the BP97E official spec sheet
  PDF (accelerazero.com, confirms "H2O and Ethylene Glycol, 50:50"
  cooling with no chiller number), the PowerDrive 7000 system-diagram
  poster PDF (stnonline.com, confirms the Thermal Management Module's
  existence and role, no capacity), the Cummins PD7000 warranty bulletin
  PDF (no thermal specs at all), and Blue Bird's own Vision Electric spec
  sheet (centralstatesbus.com, a dealer-hosted copy — adds one new detail,
  "Roof or skirt mounted HV self contained A/C" as an OPTIONAL feature
  distinct from the standard TMM, but still no chiller kW figure). This
  confirms the original research's "no numeric capacity was disclosed
  anywhere found" finding rather than overturning it — the gap is real,
  not an earlier research shortfall.
  **What was found instead, real and cited, not invented**: two
  independent commercial battery-thermal-management product datasheets
  from TKT/Guchen (busthermo.com/guchen.com, a real BTMS supplier to BYD,
  Yutong, Golden Dragon, and TATA Motors per their own materials) and
  Modine (modine.com, a major automotive thermal-systems OEM supplier):
  1. TKT's "11-12M Electric Bus BTMS" (Model 10CW) — 8-10kW cooling
     capacity, explicitly marketed for buses in Blue Bird's own Vision's
     exact length class (Type C, ~35-40ft / 10.7-12.2m), at a DC voltage
     range (400-750V) that spans this pack's real 583-689V operating
     range.
  2. Modine's A-CON BTMS — 3-10kW for commercial EVs generally, a second,
     independent real source corroborating the same upper bound.
  3. Guchen's separate 8kW BTMS unit datasheet directly discloses BOTH
     cooling capacity (8.0kW) and rated electrical input power (≤3.5kW)
     for the same real product — giving a calculated COP (8.0/3.5 ≈ 2.29)
     from two directly-disclosed numbers, not a manufacturer-labeled "COP"
     figure, used to derive `chiller_power_w` from the revised
     `chiller_capacity_w` instead of repeating the old assumed-COP guess.
  **Revised** (`configs/profiles_commercial/blue_bird_vision_electric.yaml`):
  `chiller_capacity_w` 5000W → 10000W (TKT's 10CW model's upper bound, the
  best available size-class match found); `chiller_power_w` 2000W → 4375W
  (10000W / 2.286 COP). Both remain `confidence: low` and explicitly
  flagged as a real, comparable COMMERCIAL PRODUCT spec, not Blue Bird's
  own OEM PowerDrive 7000 chiller — the same "generic published
  heavy-vehicle engineering figures" treatment already given to
  `curb_mass_kg`/`frontal_area_m2`/etc. in the original commercial-profile
  milestone, applied here to a real vendor datasheet instead of a textbook
  figure specifically because a real, size-class-matched product exists
  and a generic engineering handbook figure for "electric bus chiller
  capacity" does not.
  **Re-validated, not just swapped in**: `examples/
  chiller_hot_climate_investigation.py` re-run with the new value —
  doubling the real capacity grew the per-phase cooling effect by roughly
  the same factor (final_decel: 0.35K → 0.50K) but the 1-year Tier-2
  aggregate remained negligible either way (0.00028pp/0.0079% relative at
  5000W → 0.00039pp/0.0111% relative at 10000W) — three to four orders of
  magnitude too small to move any published comparison at either value.
  Blue Bird's own published 90-day/20-year numbers (under its real
  Columbus, OH climate, which doesn't get hot enough to meaningfully
  engage the chiller either way) moved by a noise-level amount (20-year:
  57.9778% → 57.9756%, 0.002pp). **This is a stronger, more robust version
  of the hot-climate investigation's own finding, not a new one**: since
  doubling a now-REAL capacity number barely changed the negligible
  result, the blocker was never really about the specific wattage —
  it's a genuine TIMESCALE mismatch between the pack's large thermal mass
  and a single ~20-minute characterized trip, which re-deriving
  `chiller_capacity_w` further cannot fix. All three candidate causes the
  original chiller milestone identified (engagement timing, climate
  choice, chiller magnitude) are now closed with real, evidence-based
  answers — none silently decided, none tuned toward a bigger effect.
  **One more check, done while writing up `docs/ROADMAP.md`'s own entry,
  not left as an unverified assumption**: is the negligible 1-year Tier-2
  result missing a bigger effect from the PARKED-period chiller mechanism
  specifically (`relax_parked_temp_k`'s own chiller term, separate from
  the per-trip one just discussed)? No — checked directly by simulating a
  full 24-hour parked day starting at the chiller's own 40°C threshold
  under Phoenix's real peak-summer ambient cycle: the pack temperature
  never sustains meaningfully above the threshold even over the full day
  (caps essentially at 40.0°C, chiller-on and chiller-off trajectories
  converge to the same value), so the parked path shows the same
  structural ceiling as the trip path, not an untapped lever. The 1-year
  aggregate already reflects both paths together, correctly.
  Full suite (285 tests), both `@pytest.mark.slow` cases, `mypy --strict`,
  `ruff` all pass unchanged.
- **Milestone "Promote Blue Bird's UsageProfile/ChargingProfile to
  canonical" (done, 2026-09-25, `docs/DESIGN_QUESTIONS.md` Q-0022, step 1
  of the seam-table plan the user laid out directly):** closes
  `docs/ROADMAP.md`'s own flagged gap — this profile's usage/charging data
  was "constructed for this validation pass, not yet formally adopted...
  for a published Transition-seam comparison." Two choices needed
  ratifying before promotion, not silent adoption; both resolved by the
  user directly (Option 1 in both cases, each with an added requirement —
  full record in Q-0022).
  **SOC convention**: `typical_end_soc` 0.8 → 1.0 (a real depot-charged
  fleet with no opportunity charging charges to full overnight);
  `typical_start_soc` 0.5 → 1.0-70/150 ≈ 0.5333 (end-of-route SOC after
  the real ~70mi/day usage against this vehicle's real ~150mi range) —
  both now derived from Blue Bird's own disclosed numbers, not reused from
  `flagship_comparison.py`'s passenger-car convention. **Extrapolation
  caveat, carried in the field's own `source` note, not left implicit**:
  `typical_end_soc=1.0` sits outside `aging.py`'s calendar-aging
  calibration range (the two fleet anchors were derived under constant
  80%/60% SOC, never 100%) — same class of caveat as the already-
  established 20-year time-horizon one.
  **day_type_mix methodology**: replaced the even 60/60/60 split with a
  LIVE computation (`school_bus_day_type_mix`, not a hardcoded number) —
  classifies every calendar day in a real US public-school academic-year
  window (Aug 15 – May 31) against Columbus's own already-sourced climate,
  using `ambient_temp_k`'s existing sinusoidal model at each archetype's
  more physically relevant hour (`TRIP_HOUR_OF_DAY`/8:00 morning for the
  cold proxy, `DIURNAL_PEAK_HOUR`/15:00 afternoon for the hot proxy)
  against round thresholds (cold <5°C, hot >25°C), scaled to Transition's
  real 180-day annual total. **Result: cold=79, hot=16,
  neutral(`CITY_STOP_AND_GO`)=85** — genuinely different from the old even
  split, not a cosmetic reshuffle. **Checked, not assumed, per the
  ratification's own added requirements**: (1) this does NOT land "heavy"
  on `HOT_SOAK_COMMUTE` (16/180, actually less than the old 60/180); (2)
  the COLD-leaning classification (79/180) already sees real Columbus
  winter mornings as cold as ~4.5°C, below `aging.py`'s 20-40°C calibrated
  envelope — a pre-existing exposure (the old split already had 60 such
  days), now flagged explicitly rather than left implicit; (3) verified by
  construction that this reclassification only changes the SPLIT among
  the real 180 trip days — the 180-trip/~185-parked split itself is
  unchanged, and Tier-2's parked-day calendar aging already independently
  uses each real day's own seasonal ambient regardless of this split.
  **Re-validated, not just swapped in**: 90-day total SOH loss 0.5328% →
  0.6861% (calendar-dominated, consistent with the higher average SOC now
  correctly ageing faster per `calendar_aging_soc_sensitivity`); 20-year
  57.9756% → 74.0392% (SOH 42.0% → 26.0% remaining) — a substantial,
  expected move in the correct direction, now compounding TWO documented
  extrapolations (20-year time horizon 2-4x past the 5-10yr calibration
  window, AND SOC=1.0 outside the 60-80% calibration range) rather than
  one. Still finite and monotonically non-increasing, not degenerate.
  `examples/chiller_hot_climate_investigation.py` re-run for consistency:
  its own chiller-effect finding (the actual subject of that
  investigation) is unchanged in magnitude (0.00048pp/0.0116% relative,
  vs. 0.00039pp/0.0111% before this profile-data change) — confirming
  that finding was robust to this unrelated canonical-data promotion, as
  expected, since it's about the chiller's own thermal mechanism, not the
  charging convention.
  Full suite (285 tests), both `@pytest.mark.slow` cases, `mypy --strict`,
  `ruff` all pass unchanged. **What's next, per the user's own plan**:
  close the two `Climate` fields on Transition's own `ClimateZone` (a
  cross-repo, mechanical step), then the actual table-generation script —
  neither done in this milestone.
- **Milestone "Close Transition's ClimateZone gap" (done, 2026-09-25, step
  2 of the seam-table plan, a cross-repo change in the sibling `Transition`
  repository):** closes `docs/seam_reconnaissance.md` §7's own residual
  finding — gap 3 (the three temperature fields) was closed earlier, but
  a real `Scenario` for the seam still couldn't be built without
  `altitude_m`/`solar_irradiance_w_per_m2` too, which `ClimateZone` never
  had. Genuinely mechanical, as flagged: no design question needed.
  **`Transition/src/types.ts`**: `ClimateZone` gained `altitudeFt`
  (matching the file's existing Fahrenheit/imperial convention — convert
  at the seam boundary via `altitudeM = altitudeFt * 0.3048`, same pattern
  the three temperature fields already use) and `solarIrradianceWPerM2`
  (explicitly documented as NOT the same quantity as the existing
  `solarYieldFactor`, an annual generation-yield figure for solar-array
  sizing, not peak irradiance for cabin heat-gain modeling).
  **`Transition/src/constants.ts`**: both real presets populated —
  `altitudeFt: 889` (Columbus, OH, the School Bus Depot preset's real
  location) and `altitudeFt: 810` (Cedar Rapids, IA, the Tank Wash
  preset's real location), both real per-location lookups, not
  placeholders. `solarIrradianceWPerM2: 250` for both — a shared
  placeholder, matching every ev-thermal-sim reference profile's own
  convention for this field, per `seam_reconnaissance.md` §7's own
  guidance that a first version doesn't need a real per-location figure.
  Added as data only, not yet consumed by any mechanics there — same
  precedent as `day_type_mix`'s own empty default already established in
  this codebase.
  **Verification, honestly reported**: this environment has no Node.js/
  npm/`tsc` available, so the TypeScript changes could NOT be compiler-
  checked — verified instead by careful manual review (both edits are
  small, additive, primitive-typed fields with no generics/unions
  involved) and by confirming, via `grep`, that `constants.ts`'s two
  preset objects are the ONLY places in the entire `Transition` codebase
  that construct a `ClimateZone`, so no other construction site needed
  updating for the two new required fields. Committed in the `Transition`
  repository (`7e9d26b`) but NOT pushed — that repo already had one
  earlier, unrelated commit ahead of `origin/main` before this session
  touched it, and pushing both together wasn't this session's call to
  make without asking.
- **Milestone "Generate a first Transition-seam table row" (done,
  2026-09-25, step 3 of the seam-table plan):** the actual deliverable the
  first two steps built toward — new `SeamTableRow`/`SeamTable` pydantic
  types (`schema.py`, matching `MCResult`'s own established style for
  output-shaped types) and `examples/generate_seam_table.py`, which
  produces ONE row (Blue Bird Vision Electric / Columbus, OH / a single
  fixed `duty_cycle_archetype`), not a comprehensive matrix, per explicit
  scope. Reuses `validate_blue_bird_vision_electric.py`'s own canonical
  builders directly — guaranteed to reflect the SAME canonical data
  (Q-0022) that profile's own validation record describes, not a
  silently-drifted copy.
  **Methodology decision, made and documented, not left ambiguous**:
  `docs/seam_reconnaissance.md` §5 originally proposed "median + spread,
  from MCResult," but this row uses a single unperturbed
  `simulate_long_horizon` point estimate instead
  (`methodology="point_estimate"`) — running a full 500-sample Monte Carlo
  for a first proof-of-shape row wasn't the scope the user's own plan
  described ("runs characterize_all_trips + simulate_long_horizon," not
  `run_monte_carlo`). `SeamTableRow.methodology` exists specifically so a
  future MC-backed row is a real, distinguishable upgrade, not a silent
  swap — and the row's own `caveats` field says explicitly, in the JSON
  artifact itself, not to read a point estimate as a median (mc.py's own
  "p50 vs. the point estimate" finding).
  **A real design gap found and documented, not silently worked around**:
  Transition's `ClimateZone.key` (e.g. `"5A"`) is an ASHRAE zone
  classification, not a unique per-location identifier — both currently-
  defined presets (Columbus, OH and Cedar Rapids, IA) share the same key.
  Keying the seam table by that field would collide. `SeamTableRow.
  climate_zone` uses a descriptive per-location key (`"columbus_oh"`)
  instead, with this exact collision documented in the field's own
  description — a real, previously-unidentified gap for whoever does
  Transition-side wiring next, not decided here.
  **The monthly-aggregation methodology**: Tier-2's checkpoints are daily,
  Transition ticks monthly — `_monthly_soh_deltas_pct` down-samples by
  taking the daily checkpoint closest to each calendar month's end
  (`round(month * 365/12)` days in), computing each month's INCREMENTAL
  (not cumulative) loss, matching how `monthlySohDelta(...)` would be
  consumed once per tick. New unit tests hand-verify this against a
  synthetic checkpoint trajectory, including the boundary case where a
  run is shorter than the requested table horizon (clamps to the last
  available checkpoint instead of raising `IndexError`).
  **Result, run end-to-end, not just schema-designed**: the generated
  row's cumulative loss over 20 years (74.0392%) matches — exactly, to
  the same 4 decimal places — the already-validated, independently-
  computed 20-year Blue Bird figure from `validate_blue_bird_vision_
  electric.py --duration-days 7300` (Q-0022's own milestone). Confirms
  internal consistency between the two code paths, not assumed. All five
  of this row's own caveats (point-estimate-not-median, SOC=1.0
  extrapolation, 20-year time-horizon extrapolation, unvalidated
  commercial-chemistry aging coefficients, and Columbus's climate not
  engaging the chiller) travel WITH the JSON artifact itself, in the
  row's own `caveats` field — not left to live only in this repo's own
  docs, per the project's established "the caveat must travel with the
  number" discipline.
  New tests: `test_seam_table_row_round_trip`/`test_seam_table_round_trip`
  (`tests/test_schema.py`), and `tests/test_generate_seam_table.py`
  (3 fast aggregation-logic tests, 1 `@pytest.mark.slow` end-to-end smoke
  test). Full suite (293 tests, up from 287; 290 fast + 3 slow), `mypy
  --strict`, `ruff` all pass.
  **What's still open, per the user's own plan**: step 5, Transition-side
  wiring (a `manufacturerProfileKey`-style field on `VehicleClass`, and
  actually consuming this table) — its own milestone, not started here.
  The `climate_zone` keying collision found above is a real, concrete
  question that milestone will need to resolve, not optional polish.
- **Milestone "Transition-side wiring" (done, 2026-09-26, step 5 of the
  seam-table plan, closing this session's Transition-seam work):** wires
  the seam table generated by step 3 into `Transition` itself, scoped by
  the user's own explicit choice — "track only, no new consequence yet" —
  over an alternative that would have made `batterySohPercent` gate a new
  event or cost. `examples/generate_seam_table.py` was re-run fresh
  (same seed=42, same 74.0392% cumulative loss as before — deterministic,
  not re-tuned) and its JSON output turned into `Transition/src/
  seamTable.ts` by a throwaway script, not hand-transcribed, specifically
  to avoid a transcription error in the 240-value `monthlySohDeltaPct`
  array. Embedded as a plain TS object literal (matching `constants.ts`'s
  existing convention) rather than a JSON import, since `Transition`'s
  `tsconfig.json` has no `resolveJsonModule` and enabling it couldn't be
  compiler-verified in this environment anyway (see below).
  Added `VehicleClass.manufacturerProfileKey?: string` and
  `Route.batterySohPercent: number | null` to `Transition/src/types.ts`;
  set `manufacturerProfileKey: "blue_bird_vision_electric"` on School Bus
  Depot's one `VehicleClass` in `constants.ts` (Tank Wash's truck class
  gets none — no seam-table row exists for it yet, so its routes'
  `batterySohPercent` stays permanently `null`); added
  `updateRouteBatterySohPercent()` to `engine.ts`, called from
  `rollEvents()`'s existing per-route loop (reusing the
  `monthsSinceConversion` it already computes for `ev_drivetrain_failure`,
  rather than a second loop) — decrements from the matched row's
  `monthlySohDeltaPct`, clamping to the array's last cumulative value past
  its 240-month horizon rather than extrapolating; displayed on converted
  routes in `overviewTab.ts`'s `renderRouteRow()`.
  **A real schema gap surfaced, not silently worked around**: `Route` has
  no direct reference to which `VehicleClass` it belongs to (`fleet.
  vehicleClasses` and `fleet.routes` are separate sibling arrays) — the
  lookup uses `fleet.vehicleClasses[0]` for every route in a fleet,
  documented as a v1 simplification in `manufacturerProfileKey`'s own doc
  comment. This is correct today only because both existing presets have
  exactly one `VehicleClass`; a genuinely multi-class fleet would need
  routes to carry their own class reference, a real follow-up this
  milestone deliberately didn't build (out of scope for "wire in the
  table," not an oversight).
  **Verification, honestly reported, same limitation as the ClimateZone
  milestone above**: this environment still has no Node.js/npm/`tsc` —
  verified instead by careful manual review of every changed file plus
  `grep` confirming the only two `Route`-literal construction sites in the
  whole `Transition` codebase (both presets in `constants.ts`) were both
  updated for the new required field, and that `createNewGame()` already
  `structuredClone()`s the preset object, so the new mutable
  `batterySohPercent` field carries no shared-reference risk across game
  instances. Committed in the `Transition` repository (`0c6c5c2`) but NOT
  pushed yet — same "ahead of `origin/main`, not this session's call to
  make without asking" pattern as the prior `Transition` milestone.
  This closes out the user's 5-step seam-table plan for this session — the
  seam now has one real, end-to-end-verified data path from ev-thermal-sim
  physics through to a Transition UI element, with every remaining gap
  (the `Route`↔`VehicleClass` link, any gameplay consequence for
  `batterySohPercent`, a real per-route climate/duty-cycle match instead
  of a single fixed row) named explicitly rather than quietly assumed
  away.
- **Milestone "Mistake library's second entry" (done, 2026-09-26,
  `docs/DESIGN_QUESTIONS.md` Q-0023):** "Combining battery and powertrain
  cooling loops without upsizing for the added heat load (Audi e-tron)" —
  the mistake library's second real `MistakeCase`, unblocking Milestone D
  per the user's own stated sequencing ("Milestone D when the mistake
  library gets its second real entry").
  **A real constraint found while scoping, not assumed**: `MistakeCase`
  fixes climate/usage/charging via one shared `scenario` — only
  `ManufacturerProfile` fields can differ between `failure_profile` and
  `counterfactual_profile`. This ruled out every candidate lesson drawn
  directly from the six real vehicles' own documented failure stories in
  `docs/research/profiles_v1.md`: Chevrolet Bolt's real failure was a cell-
  manufacturing defect, not a thermal-architecture decision; BMW i3's "Cold
  Loop of Death" needs a DX-refrigerant schema this project doesn't have
  (Milestone D itself), and i3 is separately off-limits for battery
  comparisons until Milestone D lands; Kia Niro EV's real "integration, not
  hardware" story depends on preconditioning (deferred, Q-0004) and a
  cabin-vs-battery coolant-priority mechanism `cooling.py` doesn't model.
  See Q-0023 for the full elimination.
  **What made this entry possible**: confirmed directly in `sim.py`/
  `cooling.py` (not assumed) that `CoolingLoop.serves` already puts every
  component it lists on ONE shared coolant `ThermalNode` — a loop serving
  `["battery_pack", "motor", "inverter"]` genuinely lets motor/inverter
  heat raise the shared coolant temperature that then couples back into
  the battery via its own `resistance_to_coolant_k_per_w` edge. No new
  physics needed — this is the real mechanism Kia Niro's own "shared loop"
  research story gestures at, just without the unbuilt cabin-priority
  piece.
  **Construction** (Q-0023): `counterfactual_profile` is the real,
  unmodified Audi e-tron (`configs/profiles/audi_etron.yaml`) — its actual
  `battery_loop` and `powertrain_loop` stay genuinely separate, matching
  research ("four independent circuits... simplified here to two, still
  separate"). `failure_profile` merges both into ONE loop that reuses
  `powertrain_loop`'s own real hydraulic/radiator numbers AND its higher
  thermostat setpoint (308.15K/313.15K, tuned for the powertrain, not the
  battery's own tighter real 298.15K/308.15K target) verbatim — modeling a
  manufacturer that combined the two circuits to save cost/parts count but
  never resized or re-tuned the result for the battery's added heat load.
  e-tron was picked over Chevrolet Bolt specifically because e-tron's two
  real loops already share one coolant type (`"50/50 water-glycol"`) —
  Bolt's real loops use genuinely different coolant (`DEX-COOL` vs.
  `50/50 water-glycol`), which would make "the manufacturer combined them"
  physically incoherent. This inverts Q-0019's Leaf-entry role assignment
  (there, the real vehicle was `failure_profile`; here the real e-tron IS
  the good design, so it's `counterfactual_profile`) — flagged explicitly
  in both Q-0023 and the script's own docstring, not a silent
  inconsistency. Every component's own `resistance_to_coolant_k_per_w`/
  `resistance_to_ambient_k_per_w` stays byte-identical between both
  profiles — only which coolant node(s) each connects to differs, an even
  purer single-variable isolation than the Leaf entry achieved.
  **Run under a MILD climate on purpose** (Seattle-like, reused from
  `flagship_comparison.py`, not the Leaf entry's hot Guam climate) — this
  failure mode is duty-cycle-driven (sustained driving heats the shared
  loop via the motor/inverter), not climate-driven, and showing a real
  effect without needing extreme heat makes that distinction directly
  rather than asserting it.
  **Result, run end-to-end and checked, not assumed to be plausible**:
  at the default 5-year horizon, failure=93.5149% vs.
  counterfactual=96.3333% (soh_delta=+2.8184pp); a 20-year point estimate
  (own caveat: past `aging.py`'s 5-10yr calibration window, same Q-0017
  limit every multi-year number here carries) shows failure=73.9438% vs.
  counterfactual=85.3106% (soh_delta=+11.3668pp) — both directions correct
  (the real separate-loop design ages better) and both a real, non-trivial
  magnitude, unlike the Leaf entry's own near-negligible delta (Q-0020).
  Checked why, not just accepted: mean pack-cell temperature over a 1-year
  run is ~293.4K (failure) vs. ~287.0K (counterfactual) — a sustained
  ~6.4K gap from continuous exposure to powertrain waste heat, well below
  either profile's `derate_temp_k`/`max_temp_k` (no derating triggered in
  either run) — a persistent, compounding perturbation, not a bounded
  per-trip effect the way the chiller investigation's finding was,
  explaining the larger delta honestly rather than treating it as
  suspicious.
  `severity="warning"`, not `"critical"` like the Leaf entry — a
  deliberate distinction: this failure mode requires a design/integration
  oversight (an otherwise real, working architecture choice — shared loops
  are a legitimate real-world pattern, per Kia Niro's own best-in-class
  real degradation — done without accounting for the added heat load),
  not the Leaf case's outright absence of any active thermal management,
  even though this entry's SIMULATED delta happens to be larger.
  New tests: `test_etron_shared_loop_case_produces_both_results_and_a_
  consistent_delta`, `test_etron_shared_loop_case_counterfactual_never_
  ages_worse_than_failure` (`tests/test_mistake.py`) — the latter asserts
  a real non-negligible delta (`> 0.001`), not just non-negative, since
  this entry's whole point (unlike the Leaf entry's near-zero one) is that
  the effect is real and worth teaching. Full suite (295 tests, up from
  293; 292 fast + 3 slow), `mypy --strict`, `ruff` all pass.
  **What's still open**: Milestone D (BMW i3's DX-refrigerant schema
  abstraction) is now unblocked per the user's own sequencing — its own
  future milestone, not started here.
- **Milestone D — BMW i3 DX-refrigerant loop abstraction (done, 2026-09-26,
  `docs/DESIGN_QUESTIONS.md` Q-0024):** plan-reviewed before implementation
  (per the standing schema/architecture plan-review expectation), then
  built and validated in this session. The i3's real battery cooling
  (direct-expansion R134a refrigerant) is no longer approximated via an
  unrealistically tight `resistance_to_ambient_k_per_w` — it's a real,
  controllable, below-ambient-capable mechanism.
  **New**: `RefrigerantLoop` schema type (`src/ev_thermal/refrigerant.py`
  physics module, `controller.py`'s `commanded_refrigerant_duty`, wired
  into both `sim.py` per-second call sites and `long_horizon.py`'s Tier-2
  parked-period model) — structurally parallel to `CoolingLoop` but with no
  glycol/radiator/pump/fan fields, since a DX loop has no separate coolant
  thermal mass and applies its heat removal as a direct sink on its served
  component(s) instead of a coolant-node edge.
  **Sourced, not invented**: `capacity_w`/`power_w` (3500W/1140W) come from
  a real, paired datasheet for a comparable automotive HV electric scroll
  compressor using the same refrigerant (Guchen GC20A320) — same "real
  comparable product, not the exact OEM part" methodology Blue Bird's
  `chiller_capacity_w` already established (Q-0021).
  **A real correctness bug found and fixed mid-implementation, not
  glossed over**: the first version applied `capacity_w * duty` as an
  UNBOUNDED direct sink, which — checked by actually running the real i3
  profile, not assumed safe by analogy to the chiller — let a single
  ~20-minute characterized trip cool the pack ~14-20K, compounding across
  `simulate_long_horizon`'s day-to-day carryover into a full year settling
  at an implausible -4°C to -19°C (activation_temp_k is 20°C). Root cause:
  no resistance rate-limited the transfer, unlike every other cooling path
  in this codebase. Fixed by reusing `ThermalNode.resistance_to_coolant_
  k_per_w` (already present on every component, already noted "unused"
  on the i3's battery pending exactly this) as a genuine cap:
  `heat_removed_w = min(capacity_share_w * duty, (component_temp_k -
  activation_temp_k) / resistance_to_coolant_k_per_w)`. This also required
  rebuilding `relax_parked_temp_k`'s Tier-2 refrigerant branch from a
  (wrong) "borrow the ambient path's own weak conductance" target-shift
  trick into a genuine two-conductance weighted-average target — the
  ambient path's weakness had nothing to do with how fast a refrigerant
  loop can actually move heat, so reusing it as the mechanism's rate limit
  was the real bug, not a step-size problem. See Q-0024 for the full
  before/after reasoning and both options considered.
  **Verified end-to-end after the fix, real numbers, not assumed
  plausible**: a 1-hour Tier-1 run at 45°C ambient holds the pack near
  25°C (barely moves — the resistance-capped transfer rate at that
  differential is tiny, exactly the "resistance is the binding constraint"
  finding); a 1-year Tier-2 run under Guam settles 22.8-25.0°C (final
  SOH 98.50%) and under Seattle 5.0-21.7°C (final SOH 99.23%) — both
  physically plausible ranges tracking their climates, both directions
  correct (hotter climate ages faster). Confirmed the resistance term
  dominates in practice (a 20K differential over the real ~0.3 K/W
  resistance implies ~67W, far below the 3500W nominal capacity) — the
  capacity cap is a real but rarely-binding backstop, not chased into a
  fully rigorous two-regime Tier-2 treatment (a documented simplification).
  **A second, smaller finding, flagged not silently fixed**: the i3
  battery's own `resistance_to_coolant_k_per_w` (0.3 K/W, set before this
  field was ever read by anything) is noticeably weaker than the same
  field's value on other actively-cooled profiles (~0.015-0.05) — now that
  it's genuinely load-bearing, its magnitude deserves real scrutiny, not
  performed here (see `docs/ROADMAP.md`).
  New tests: `tests/test_refrigerant.py` (NEW, 7 tests covering the
  resistance/capacity cap interplay), plus `RefrigerantLoop` round-trip
  (`test_schema.py`), `commanded_refrigerant_duty` ramp shape
  (`test_controller.py`), an i3-topology end-to-end Tier-1 test
  (`test_sim.py`), and 6 Tier-2 `relax_parked_temp_k`/SOC-decrement tests
  (`test_long_horizon.py`). Full suite (314 tests, up from 295; 311 fast +
  3 slow), `mypy --strict`, `ruff` all pass.
  **What's still open, deliberately not attempted here** (matching the
  chiller's own 4-milestone arc — implementation first, validation as
  explicit follow-ups): multi-climate/duty-cycle validation beyond the two
  climates checked above; re-examining `resistance_to_coolant_k_per_w`'s
  magnitude against other profiles' convention; and the actual decision on
  whether i3-vs-Leaf battery comparisons are now publication-safe — the
  `CLAUDE.md`/`model_reference.md` caveat stays in place, reworded to say
  why (not yet re-validated under the real mechanism across climates), not
  lifted.
- **Milestone D follow-up — multi-climate validation, i3-vs-Leaf caveat
  LIFTED (done, 2026-09-26, `docs/DESIGN_QUESTIONS.md` Q-0024's
  follow-up resolution):** closes the one item Milestone D itself left
  open. `examples/i3_multiclimate_validation.py` (NEW) validates the
  refrigerant loop under two more real climates beyond the two used
  during implementation:
  - **Phoenix, AZ** — already sourced (real NOAA normals) for the chiller
    investigation; reused directly.
  - **Minneapolis, MN** — NEW, real NOAA NCEI 1991-2020 normals
    (Minneapolis/St Paul Intl AP, station USW00014922), first use of
    this location anywhere in the project. Derived the same way every
    other real climate here has been: mean=8.28°C (average of the 12
    monthly means, matches NOAA's own published Summary row exactly),
    seasonal_amplitude=16.14K, daily_amplitude=9.45K, elevation=872ft
    (real) — a genuinely cold climate (implied winter low -17.3°C, well
    below the battery's 0°C charge cutoff), chosen specifically because
    the refrigerant loop is cooling-only and should show NO engagement
    there, the complementary case to Phoenix's hot-peak stress test.
  **Two things checked, not assumed**: (1) a direct hot-bucket-vs-cold-
  bucket engagement comparison (WITH vs. WITHOUT the RefrigerantLoop,
  same technique the original chiller investigation used) — under
  Phoenix's 47.7°C peak, the refrigerant effect grows smoothly across a
  HOT_SOAK_COMMUTE trip's phases (+0.077K → +0.394K → +0.649K); under
  Minneapolis's -17.3°C trough, the effect is EXACTLY 0.0000K in every
  phase, confirming the mechanism stays fully, cleanly inactive in the
  cold rather than partially engaging or producing a numerical artifact.
  (2) A real 365-day i3-vs-Leaf comparison under all four climates
  (Guam, Seattle, Phoenix, Minneapolis), reusing `examples.flagship_
  comparison`'s own `run_one` machinery directly (same usage/charging/
  day-type-mix for both vehicles under every climate, isolating climate
  as the controlled variable). **Result, a coherent and physically
  sensible picture, not a fluke**: i3-vs-Leaf total-loss ratio —
  Guam 0.568× (i3 clearly better), Phoenix 0.648× (i3 clearly better),
  Seattle 1.130× (i3 slightly worse — a genuine multi-variable real-
  vehicle comparison, not an isolated-variable counterfactual, so some
  non-thermal difference is expected), Minneapolis 1.005× (essentially
  identical — no artificial advantage where the mechanism is correctly
  inactive). i3 winning decisively in both hot climates is directly
  consistent with the Leaf's own real documented hot-climate weakness
  (docs/research/profiles_v1.md: "~4.2%/year in hot climates"). No
  NaN/inf/runaway temperatures across any of the four climates for
  either vehicle.
  **The i3-vs-Leaf battery-comparison publication caveat is LIFTED** —
  `CLAUDE.md`, `docs/ROADMAP.md`, and `docs/model_reference.md` updated
  accordingly. Still open, unaffected by this: the i3 battery's own
  `resistance_to_coolant_k_per_w` magnitude (flagged in the original
  Milestone D entry, not re-derived here — out of this validation's
  scope).
- **Milestone "Mistake library's third entry" (done, 2026-09-27,
  `docs/DESIGN_QUESTIONS.md` Q-0025):** "DX refrigerant compressor internal
  failure ('Black Death') leaves the battery with no active cooling (BMW
  i3)" — unblocked directly by the multi-climate validation above (any i3
  number built before that caveat lifted would itself have carried the
  same "not yet validated" flag).
  **A genuinely new lesson category, not a repeat of the existing two
  entries**: Leaf's entry is a manufacturer DESIGN decision (never built
  active cooling); e-tron's is an INTEGRATION mistake in a hypothetical
  combined-loop design; this one is a RELIABILITY risk in an otherwise
  validated, working real system — a real, specifically-documented BMW
  i3/i3s failure mode (`docs/research/profiles_v1.md`'s own i3 "Story":
  "DX cooling elegant... but fragile -- if A/C compressor fails
  internally, entire battery cooling system can't be flushed, requiring
  full pack + cooling system replacement"; corroborated by a real Autopian
  article on the same failure, cited directly in the script).
  **A real architectural question resolved, not silently assumed**: a
  direct i3-vs-Leaf `MistakeCase` (two different real vehicles) was
  considered and rejected — `MistakeCase`'s own "only variable is the
  design decision" docstring already ruled this out, the identical
  reasoning Q-0019 used to reject the same option for the Leaf's own
  entry. Lifting the publication caveat unblocked using the i3 AT ALL, but
  never changed this separate architectural constraint — the two are
  distinct gates, not one.
  **Construction**: both `failure_profile` and `counterfactual_profile`
  are the SAME real i3 profile (`configs/profiles/bmw_i3.yaml`) — the
  first mistake-library entry where both sides share one real vehicle,
  differing only in whether `thermal_architecture.refrigerant_loops` is
  present. `failure_profile` removes it entirely (total loss, not a
  reduced `capacity_w`), matching the real documented consequence ("entire
  system can't be flushed") of an unrecoverable internal failure rather
  than a gradual degradation. Run under Phoenix, AZ's real climate —
  already sourced and i3-validated by the multi-climate follow-up above,
  reusing that infrastructure directly rather than introducing a new
  unvalidated climate/vehicle pairing.
  **Result, run end-to-end at three horizons, not assumed proportional**:
  1-year +0.90pp (failure 97.81% vs. counterfactual 98.71%), 5-year
  +4.40pp (89.13% vs. 93.52%), 20-year point estimate +17.66pp (56.47% vs.
  74.14%, carrying the same Q-0017 extrapolation caveat every multi-year
  number here does) — real, substantial, correctly-directed at every
  horizon checked. `severity="critical"`, matching the real documented
  consequence's severity.
  New tests: `test_i3_compressor_failure_case_produces_both_results_and_a_
  consistent_delta`, `test_i3_compressor_failure_case_counterfactual_
  never_ages_worse_than_failure` (`tests/test_mistake.py`, the latter
  asserting a real non-negligible delta, matching the e-tron entry's own
  precedent, not the Leaf entry's near-zero-tolerant one). `mypy --strict`,
  `ruff` pass on all new files.
- **Milestone "Battery Health tab" (done, 2026-09-27, `Transition` repo
  only — no ev-thermal-sim code changed):** the seam's tracked SOH data
  (`Route.batterySohPercent`, wired in the earlier "Generate a first
  Transition-seam table row"/"Transition-side wiring" milestones) finally
  gets a real UI surface. Previously the only place it appeared was one
  line — "Battery SOH: X%" — buried in the Overview tab's route-conversion
  row, matching the "track only, no new consequence yet" v1 scope the user
  approved when the wiring first landed. Still track-only (a visibility
  upgrade, not a new gameplay consequence) — that scope decision itself
  wasn't revisited here.
  **New `Battery Health` tab** (`Transition/src/ui/batteryHealthTab.ts`,
  registered in `app.ts`'s existing `TabModule`/`TABS` pattern — a
  first-class, already-supported mechanism, not a new one): a fleet-wide
  summary (how many converted routes have tracked data, a fleet-average
  SOH gauge) plus a per-route card for every route — not yet converted,
  converted-but-untracked (Tank Wash's truck class, honestly disclosed
  rather than silently blank), or converted-and-tracked (current SOH
  gauge, months since conversion, and a projected trajectory at 5/10/20
  years from now, computed from the seam table's own monthly deltas).
  **The seam-table row's own `caveats` are now shown in the UI itself**,
  not just carried in code comments or this repo's own docs — every
  caveat travels with the number all the way to the screen a player
  actually looks at, extending the "the caveat must travel with the
  number" discipline (Q-0022/Q-0024) to its first real UI surface.
  Small refactor, not new logic: `engine.ts`'s `updateRouteBatterySohPercent`
  had its seam-row lookup and cumulative-loss math extracted into two
  reusable, exported pure functions (`seamRowForFleet`,
  `cumulativeSohLossPct`) so the new tab's projections reuse the exact
  same logic the tick loop uses, not a parallel reimplementation —
  confirmed behavior-identical by manual review (same limitation flagged
  for every prior Transition-side change this session: no test framework,
  no Node/tsc available in this environment).
- **Milestone "Real preconditioning" (done, 2026-09-27,
  `docs/DESIGN_QUESTIONS.md` Q-0026/Q-0027):** Q-0004 had deferred
  preconditioning entirely, naming the blocker as a "vehicle knows a
  charge stop is coming" signal requiring a route/navigation concept this
  simulator doesn't have.
  **First finding: that blocker is reducible, not real.**
  `TripType.FAST_CHARGE_SESSION` was already its own independent
  day-type-mix category — `characterize_fast_charge_session` starts from
  `initialize_sim_state` at a fixed ambient bucket, with zero composition
  with any preceding drive archetype. `simulate_long_horizon`'s day loop
  already knows, for every simulated day, whether that occurrence IS a
  fast-charge session — that's already exactly as knowable as any other
  day-type membership fact, with zero new machinery. Q-0004 had conflated
  "a real vehicle needs navigation to know this" (true) with "the
  simulator needs a route concept to know this" (false, since it already
  decides day types by category, not by simulating an actual
  drive-to-charger sequence).
  **Second finding, load-bearing for the architecture**:
  `apply_trip_start_offset` (Tier-2's linear-offset replay) only shifts
  temperature by a constant — it never re-derives current/SOC at the
  shifted value, so it cannot reach `battery.available_charge_current_a`'s
  `_cold_derate_scale` term, the one thing that makes preconditioning
  matter. The mechanism therefore had to live at Tier-1, inside
  `characterize_fast_charge_session` itself, not as a Tier-2 modifier.
  **Third finding, found while surveying, not asked for**: BMW i3's
  existing `battery_preconditioning: true` cited "preconditioning via
  wallbox before departure in cold" — real, but a DIFFERENT phenomenon
  (cold-weather pre-departure warming while already plugged in at home)
  from what the field's own `Field(description=...)` claims
  (fast-charge-arrival preconditioning specifically). Kia Niro EV's own
  citation, by contrast, genuinely is fast-charge-specific. Resolved
  (Q-0027, option 3): i3's `battery_preconditioning` corrected to
  `false` — a data-correctness fix removing an unsupported claim, not a
  new invented figure — with its real cold-departure capability flagged
  as a separate, still-unmodeled gap, not conflated with this field.
  **Architecture (Q-0026, approved in full)**: a `preconditioned: bool`
  parameter on `characterize_fast_charge_session`, gated by the two
  already-existing fields (`ThermalArchitecture.battery_preconditioning`
  × `ChargingProfile.preconditioning_enabled`, no new gate field needed).
  When `True`, a new `long_horizon._precondition_phase` runs FIRST: a
  real, physically-stepped stationary phase (`sim.
  advance_preconditioning_step`, peer to `advance_charging_step`, mirrors
  its cooling/refrigerant-loop machinery but inverts the sign — the
  heater DISCHARGES the battery) that draws the profile's own
  `preconditioning_heater_power_w` as a direct additive heat term on the
  battery node (on top of, not instead of, the battery's own incidental
  R0/entropic self-heating from carrying that current — this is a
  ~100%-resistive heater, no separate compressor-style capacity/COP
  figure the way `CoolingLoop.chiller_capacity_w`/
  `RefrigerantLoop.capacity_w` have one). The phase runs for up to
  `PRECONDITIONING_DURATION_S` (1500s/25min) or stops early the moment
  `preconditioning_target_temp_k` is reached, whichever comes first —
  a real BMS controls to a target, not a fixed duration regardless of
  outcome. Produces a genuinely new `precondition` phase (not folded into
  `ramp`), so `TripCharacterization.phases` is always exactly 2 or always
  3 entries per `FAST_CHARGE_SESSION` characterization set (never mixed),
  matching `_interpolate_characterization`'s `strict=True` zip
  requirement. New schema fields: `ThermalArchitecture.
  preconditioning_heater_power_w`/`preconditioning_target_temp_k`
  (`Sourced[float] | None`, `None` when incapable — named per the user's
  explicit correction, not `battery_heater_w`, since the mechanism is
  preconditioning-specific and shouldn't imply a general-purpose pack
  heater this project doesn't otherwise model).
  **Real-world figures sourced, not invented**: heater power 1.5-3kW is a
  real, multiply-sourced range for passenger-EV battery preheating (2kW
  chosen, mid-range, for Kia Niro EV — still a placeholder POINT value
  within a real range, not a Niro-specific number); target temp 293.15K
  (20°C) reuses Niro's own already-cited `cold_derate_threshold_temp_k`
  research value exactly — deliberately, not coincidentally, since a real
  BMS stops preconditioning once in the acceptable range rather than
  independently re-deriving a second number; duration 1500s is within
  Tesla's own real, widely-documented ~20-30min Supercharger-
  preconditioning window, not vehicle-specific for any of the six
  reference profiles.
  **A pre-existing edge-case bug found and fixed during implementation,
  not present before this milestone touched this code path**: an
  already-warm ambient bucket makes `_precondition_phase`'s loop break on
  its first iteration (target already met), leaving only 1 time sample —
  `_phase_aging`'s loop needs ≥2 to compute `covered_s`, so it raised
  `ValueError` instead of producing a legitimate zero-duration phase.
  Fixed using the exact same `_degenerate_phase` pattern
  `_fast_charge_phases` already established for its own "already past the
  knee at t=0" case — verified correct at all three boundary conditions
  (cold/never reaches target, mild/stops early exactly at target,
  warm/degenerate zero-duration phase).
  **The headline finding, verified sample-by-sample, not just at the
  aggregate level, and reported honestly rather than tuned toward a nicer
  story**: preconditioning DOES correctly raise the charge-current
  ceiling (confirmed: ~3-4A higher sustained current throughout a
  Minneapolis-cold session vs. unpreconditioned, consistent with
  `_cold_derate_scale` rising from ~0.61 to ~0.83 at the warmer starting
  temperature) — but the heater's own SOC draw during the up-to-25-minute
  precondition phase (a real cost, ~1.3 percentage points of SOC for
  Kia Niro EV's pack at the coldest tested bucket) is larger than what
  that current-ceiling benefit claws back within the fixed 1800s
  (`FAST_CHARGE_SESSION_DURATION_S`) charging window. Net result at the
  coldest tested Minneapolis bucket: preconditioned final session SOC
  (0.8432) finishes slightly BELOW unpreconditioned (0.8449) — the
  benefit recovers most, not all, of the heater's own cost. Rolled up
  into a 20-year Tier-2 run (an ad hoc, uncommitted verification-only
  day-type mix built specifically to include `FAST_CHARGE_SESSION`
  occurrences, since none of the six reference profiles' own canonical
  mixes do — see `docs/ROADMAP.md`), the same direction holds at the
  aggregate level: 19.77% total 20-year SOH loss preconditioned vs.
  19.59% unpreconditioned, i.e. net-negative. This is not a bug — every
  value in the underlying per-second trace (current, SOC, temperature) is
  internally consistent with the mechanism working exactly as specified
  — it's a genuine consequence of this project's aging model having no
  separate "charging cold accelerates degradation beyond what the
  current ceiling already throttles" term: the ONLY route by which
  preconditioning can help SOH here is "charge more, in the same
  fixed-duration window," and for the currently-sourced 2kW/25min
  figures, that benefit is smaller than the mechanism's own cost. A
  higher-powered or longer-duration heater could plausibly flip the sign
  — not tried here, since inflating either figure without a real citation
  to force a "nicer" result would be exactly the kind of invented
  parameter this project's "don't invent parameters" rule exists to
  prevent.
  New tests (12 total): `tests/test_schema.py` (2, round-trip +
  defaults-to-None for the new fields), `tests/test_sim.py` (4, covering
  `advance_preconditioning_step`'s stationarity, discharge direction, and
  direct battery heating), `tests/test_long_horizon.py` (6, covering
  2-vs-3-phase characterization shape, the warm/cold/stops-early-at-target
  boundary conditions, and `characterize_all_trips`' capability×scenario
  gating across all four combinations). Full suite: 325 passed (was 313
  before this milestone, +12 matching the new tests exactly), 3 slow
  tests unaffected. `mypy --strict`, `ruff check` clean on every file
  touched.
