# Seam reconnaissance: ev-thermal-sim ↔ Transition

Read-only investigation. No code or schema changes in either repo. All
`file:line` references below were read directly, not inferred.

## 1. What does Transition currently model for battery degradation?

**Nothing.** There is no capacity-fade, SOH, or continuous aging model
anywhere in Transition. Confirmed by direct search: "battery" appears only
in two unrelated contexts — (a) stationary grid battery *storage* for
outage mitigation, which doesn't exist yet and is referenced only as a
hypothetical future mitigation (`types.ts:220-236`, `engine.ts:1188-1218`),
and (b) EV *pack price* trend, used purely for fleet-conversion cost
economics (`constants.ts:621`, `evCostPerVehicle`), never capacity or
health.

The nearest thing to a battery-condition model is `ev_drivetrain_failure`
(`engine.ts:1154-1185`, table entry `constants.ts:659`): a per-vehicle,
per-month Bernoulli roll (`baseMonthlyProbability: 0.001`) that jumps 6x
after a fixed 96-month warranty boundary, drawing a flat random repair
cost on trigger. It has **no dependency on temperature, climate,
duty-cycle intensity, charging pattern, or elapsed usage beyond a single
warranty-window flag** (`route.conversionCompletionOrdinal`, a completion
timestamp, not a degradation state). It also isn't battery-specific — the
event name is generic "drivetrain" and could as easily represent a motor
or inverter fault. This is a reliability-event placeholder, not an aging
model, and its own doc comment says so explicitly ("placeholder
probability and cost range, not real fleet-maintenance data").

Vehicles in Transition have no per-unit state at all: `VehicleClass`
(`types.ts:33-40`) is a fleet-level count + average mileage; `Route`
(`types.ts:42-52`) tracks miles/vehicle-count/conversion status, nothing
per-vehicle. There is nowhere in the schema a SOH value *could* currently
live without adding a field.

## 2. What could the thermal sim supply that Transition currently lacks?

- **A real degradation curve as a function of climate + duty cycle**,
  replacing (or informing) `ev_drivetrain_failure`'s flat-probability
  placeholder. `LongHorizonCheckpoint` (`schema.py:1371-1394`) gives daily
  `battery_soh`, split into `calendar_soh_loss_cumulative` vs.
  `cycle_soh_loss_cumulative` — the split matters for Transition because
  calendar loss depends on climate/idle-SOC (facility-level, doesn't vary
  route to route) while cycle loss depends on throughput (does vary by
  route). Cadence is daily; Transition ticks monthly, so this would need
  down-sampling to monthly deltas, not a 1:1 field mapping.
- **A climate-dependent failure-probability curve**, derived from a SOH
  trajectory's slope, could replace `ev_drivetrain_failure`'s fixed
  warranty-boundary jump with something that actually varies by
  `ClimateZone` — currently a purely cosmetic field for solar yield
  (`types.ts:116-121`), never touching vehicle reliability at all.
- **A distribution, not a point estimate.** `MCResult`/`MCPercentileBand`
  (`schema.py`, `mc.py`) already produce p05/p50/p95 SOH bands under
  aging-coefficient uncertainty. Transition's whole design language is
  risk under uncertainty (event tables, DSCR floors, objective tiers) —
  a SOH *band* fits that idiom far better than a single number would.
- **Per-trip-archetype thermal/aging signatures**
  (`TripCharacterization`/`TripPhaseAging`, `schema.py:701-757`) could in
  principle inform which route *shapes* (stop-and-go vs. highway vs.
  fast-charge-heavy) age a pack faster — but see §3, Transition doesn't
  currently have the route detail to consume this.

## 3. What would Transition need that the thermal sim doesn't currently provide?

- **A vehicle class that matches Transition's fleets.** All six
  thermal-sim reference profiles are passenger EVs (Leaf, Model 3, Bolt,
  e-tron, i3, Niro) — none is a school bus or a tank-wash truck, Transition's
  only two presets (`constants.ts`, `FacilityType` in `types.ts:17`). Using
  an existing profile as a stand-in would misrepresent pack size, duty
  cycle, and thermal architecture; a real seam needs at least one new
  `ManufacturerProfile` per vehicle class Transition models, per this
  project's own "don't invent parameters" rule.
- **Payload/cargo variation.** Thermal sim's `VehicleDynamics` has only
  `curb_mass_kg` (`schema.py:183`) — fixed, no variable load. A tank-wash
  truck's payload swings full-vs-empty in a way a bus's doesn't; not
  modeled on either side.
- **Per-route duty cycles.** Transition's `Route` (`types.ts:42-52`) has
  `dailyMiles` and `dedicatedVehicleCount` only — no speed profile, no
  stop frequency, no charging pattern per route. The thermal sim's
  `TripType` archetypes (cold-start commute, highway, city stop-and-go,
  fast-charge session — `schema.py:694-698`) are far richer than anything
  Route currently expresses. Feeding trip-archetype-level output into
  Transition would mean either coarsening the thermal sim's output down to
  "one number per route" or enriching `Route` to carry an archetype
  mix — the latter is real schema work on Transition's side, not this
  repo's to decide.
- **Distinct charging patterns per facility type**: Transition has one
  aggregate `chargingDemandPerVehicleKWPlaceholder`
  (`types.ts:161`) and one facility-wide `annualFacilityLoadKWh`
  (`types.ts:185`) — no `ChargingProfile`-equivalent (start/end SOC,
  DC-fast fraction, charge-completion timing) per route or vehicle class.
- **Ambient/climate exposure per facility**, not per profile. Transition's
  `ClimateZone` (`types.ts:116-121`) carries only a solar yield factor —
  no mean/seasonal/diurnal temperature, which `Climate`
  (`schema.py`) needs to drive aging. This is the single largest schema
  gap on Transition's side for consuming thermal-sim output meaningfully.

## 4. Where are the two genuinely redundant?

**Nowhere, currently — but not because the underlying concern is disjoint;
because Transition doesn't model the concern at all yet.**
`ev_drivetrain_failure` is not a lesser version of the thermal sim's aging
model that should be deleted — it's a different kind of thing entirely (a
discrete reliability event vs. a continuous degradation state), and it
currently covers "drivetrain" broadly, not battery capacity specifically.
Nothing in Transition computes anything the thermal sim already does
better; there is no overlap to delete. This is itself the finding: today
these are complementary-but-unconnected, not duplicated.

## 5. Proposed interface

**Shape, not spec.** Precompute, don't query live: Transition's tick loop
is a lightweight monthly `Math.random()` roll (`engine.ts`, no async/IO
in the hot path); embedding a Python long-horizon sim call inside it would
be a architectural mismatch, not just a performance one. The natural
artifact is a **precomputed table**, produced offline by this repo, of the
shape `(vehicle_class, climate_zone, duty_cycle_archetype) → monthly SOH
delta distribution` (median + spread, from `MCResult`, not a single
value) — keyed on axes Transition would need to gain (vehicle class per
§3, climate per facility) before it could look anything up. JSON is the
natural artifact format (both TypeScript and Python read/write it without
friction); an npm-published table or a static asset bundled at build time
are both plausible, not decided here.

Consumer API on Transition's side would be a pure function —
`monthlySohDelta(vehicleClass, climateZone, monthsSinceConversion,
dutyCycleArchetype) -> number` (or a sampled draw, if Transition wants to
roll it stochastically like its other events) — called once per converted
route per tick, replacing or augmenting `ev_drivetrain_failure`'s
post-warranty cost draw with an actual capacity-loss consequence (e.g.
range-derating a route past some SOH floor, or a la carte cash cost
proportional to lost capacity).

**Preconditioning-gap dependency**: this interface's *shape* doesn't
change once preconditioning lands — SOH delta as a function of the same
axes stays the same signature either way. What changes is the *severity*
of climate-driven degradation the table would report, particularly for
fast-charge-heavy duty cycles. Flagging so this isn't mistaken for a
blocker on the interface design, only on how aggressive the numbers in
the eventual table should look.

## 6. Gap-closure assessment: which of the three is smallest to close first?

Ranked smallest-to-close first, by direct comparison of what each side
would actually need to do — not by which gap matters most.

**Smallest: gap 3, temperature data.** Zero work on ev-thermal-sim's side
— `Climate` (`schema.py:662`) already carries exactly the fields needed
(`mean_annual_temp_k:666`, `seasonal_amplitude_k:669`,
`daily_amplitude_k:669`), sourced as real climate normals, not invented.
On Transition's side, `ClimateZone` (`types.ts:116-121`) currently holds
only `solarYieldFactor`; both presets share the identical placeholder
value (1,250 kWh/kW/yr) with an explicit code comment marking Tank
Wash's copy as "not real Iowa solar-resource data" — `constants.ts:191`.
Closing this means adding 2-3 numeric fields to `ClimateZone` and
populating them with published NOAA/climate-normal figures for Ohio
(school bus preset) and Iowa (tank wash preset) — a bounded factual
lookup, not a design decision, and nothing in `engine.ts` needs to change
to add unconsumed data (Transition's own established pattern — see
`day_type_mix`'s empty-default precedent this repo already uses, and
`targetElectrificationRateByYear20`'s "added as data only, not yet read"
precedent at `types.ts:170-182`).

**Middle: gap 2, route duty-cycle detail.** Also mostly additive schema
work on Transition's side — `Route` (`types.ts:42-52`) would need
something like an archetype-mix field alongside `dailyMiles`. But this
gap's closure is only as useful as gap 1's: `TripType` characterization
(`characterize_trip`, referenced in CLAUDE.md's Milestone E2b entry)
runs a full `sim.run_scenario` against a real `ManufacturerProfile` — so
a route's duty-cycle mix has nothing to be characterized against until a
matching vehicle profile exists. Populating this field before gap 1
closes produces data Transition can display but the thermal sim can't yet
consume. There's also a real judgment call baked in (which archetype mix
represents "Elementary AM/PM" vs. "Special Education / Door-to-Door" —
`constants.ts:59-62`) that gap 3 doesn't have, since gap 3 is a lookup,
not a categorization.

**Largest: gap 1, shared vehicle classes.** The Transition-side change is
trivial — one new field on `VehicleClass` (`types.ts:33-40`) pointing at
a `ManufacturerProfile` key. The ev-thermal-sim-side change is not: none
of the six existing reference profiles (Leaf, Model 3, Bolt, e-tron, i3,
Niro) represents a school bus or a Class 8 tank-wash truck, and per this
project's "don't invent parameters" rule, a real commercial-vehicle
profile needs actually-sourced battery capacity, thermal architecture,
and duty-cycle data before it can be built at all — the same sourcing
burden every one of the six existing profiles already went through. This
is the one gap whose cost sits almost entirely on ev-thermal-sim's side,
and it's substantial.

**Net read, since this is the direct input to the next decision:**
smallest-to-close and highest-leverage are not the same gap here. Gap 3
is cheapest and can close independently today with no new invented
numbers. Gap 2's payoff is gated behind gap 1 regardless of when it's
built. Gap 1 is the actual bottleneck — nothing downstream (a real
duty-cycle mix, a meaningful SOH-delta table) means anything until at
least one commercial-vehicle `ManufacturerProfile` exists, and that's the
most expensive of the three, not the least. Closing gap 3 first is real
progress and low-risk, but doesn't by itself unblock a consumable table.

## 7. Revisiting §5's interface shape, now that gap 3 is closed and a real commercial profile exists

Per the decision to close gap 3 fully and gap 1 scoped to one vehicle
(Blue Bird Vision Electric — see CLAUDE.md's "Transition seam — gap 3
closed, first commercial-vehicle profile" milestone for the full record).
Checked directly against what actually landed, not assumed.

**The proposed shape itself — a precomputed `(vehicle_class, climate_zone,
duty_cycle_archetype) → monthly SOH delta distribution` table, produced
offline, consumed as a pure function on Transition's side — still holds.**
Nothing found in closing gap 3 or building the first commercial profile
argues for a different shape. The two specific axis concerns §5 flagged
are now concretely testable, not just theoretical:

- **`vehicle_class` axis:** now has one real key —
  `configs/profiles_commercial/blue_bird_vision_electric.yaml`'s filename
  stem, the same "key by filename stem" convention `profile_loader.
  load_all_profiles` already uses for the six passenger profiles. A second
  key (tank-wash truck) is still deliberately deferred.
- **`climate_zone` axis:** Transition's `ClimateZone` now carries real
  mean/seasonal/diurnal temperature data that converts to ev-thermal-sim's
  `Climate.mean_annual_temp_k`/`seasonal_amplitude_k`/`daily_amplitude_k`
  via the documented Fahrenheit→kelvin formula.

**One thing §3's original gap list didn't name, found only by actually
trying to close gap 3 rather than just scoping it: `Climate` needs two
MORE required fields gap 3's own closure didn't touch —
`altitude_m` and `solar_irradiance_w_per_m2`** (both `Sourced[float]`,
no default, `schema.py:675-678`). `ClimateZone.solarYieldFactor`
(`Transition/src/types.ts:120`) is a different quantity — an annual
generation-yield placeholder (kWh/kW/yr) for solar-array sizing, not a
peak-irradiance figure for cabin heat-gain modeling — so it can't stand in
for `solar_irradiance_w_per_m2` even though both are "solar" numbers.
Building an actual `Scenario` for the seam (Tier-1 characterization needs
one) would still fail today on these two missing fields. Not a blocker on
the same scale as gap 3 was — altitude is a trivial per-location lookup,
and a generic peak-irradiance placeholder (the six passenger profiles all
use one already) would suffice for a first version — but it's a real,
previously-unnamed residual gap, surfaced by trying the actual thing
rather than reasoning about it in the abstract. Worth closing alongside
whenever `ChargingProfile`/`UsageProfile` (below) get built.

**`duty_cycle_archetype` axis:** still not populated on Transition's
`Route` (gap 2, unchanged, deliberately deferred) — but this checked out
differently than §5's original framing implied. A FIRST version of the
table doesn't strictly need gap 2 closed: it could run the school bus
profile's day-type mix at one fixed, representative default (the same
"single fixed schedule, not per-route" pattern `examples/
flagship_comparison.py` already uses for the six passenger profiles),
producing a table whose third axis doesn't yet vary meaningfully by
route. Gap 2 is what makes that axis actually informative per-route, not
what makes the table producible at all — a real distinction §5 didn't
draw explicitly.

**Two more concrete gaps found only by getting this close to actually
building the table, neither in the original three:**

1. **The school bus `ManufacturerProfile` alone isn't enough to run a
   `Scenario`** — `characterize_all_trips`/`simulate_long_horizon` also
   need a `UsageProfile` and `ChargingProfile` for this specific vehicle
   (annual mileage, trip-length distribution, charge power, typical start/
   end SOC). Transition's own `SCHOOL_BUS_DEPOT_PRESET` already has real
   numbers these could be built from (70mi/day × ~180 school days/year,
   11kW L2 charging per bus) — this is genuinely reusable data sitting on
   Transition's own side, not a new research task on ev-thermal-sim's.
2. **The new profile has only been validated with a 60-second smoke
   test** (`tests/test_profiles_commercial.py`), the same bar every YAML
   gets at load time — it has NOT yet been run through a real Tier-1
   characterization or a long-horizon SOH trajectory the way the six
   passenger profiles have been (flagship comparison, Monte Carlo). Doing
   that once is the natural next validation step before this profile's
   numbers go into any table, the same "run it and check the actual
   numbers before publishing" discipline this project has applied
   everywhere else.

**Preconditioning-gap dependency (§5's original note): confirmed still
correct, and now concretely, not just abstractly, relevant.** The Blue
Bird profile's own research confirms real preconditioning capability
(`battery_preconditioning: true`) — meaning THIS specific vehicle's
fast-charge thermal story is exactly the case Q-0004's deferred
preconditioning gap would understate, not a generic hypothetical. Same
conclusion as before: doesn't change the interface's shape, only the
severity of what the eventual table would report for fast-charge-heavy
duty cycles.
