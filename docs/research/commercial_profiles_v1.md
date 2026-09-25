# Commercial-Vehicle Profile Research — v1

Human-compiled research dump for commercial/heavy-duty vehicle profiles,
started for the Transition seam's gap-1 closure (see
`docs/seam_reconnaissance.md`). Same status and caveats as
`docs/research/profiles_v1.md`: **not** a verified primary-source document
for every field — sourcing quality varies field by field, and each field is
tagged with its own `confidence` in the profile YAML that cites this
document. Treat every number below as "reported," not "verified," until
traced to its own citation.

This is a separate, versioned document from `profiles_v1.md`, not an
addition to it — deliberately: `profiles_v1.md` documents the six passenger-
EV reference vehicles that this project's aging-coefficient-sharing design,
flagship comparisons, and Monte Carlo record are all built around (see
docs/MILESTONES.md's repeated "shared across all six profiles by design" rationale).
A commercial vehicle is a genuinely different vehicle class, not a revision
or correction of that existing research, so it gets its own file and its own
profile directory (`configs/profiles_commercial/`, not `configs/profiles/`)
rather than silently becoming an unannounced "seventh" member of "the six."
Future commercial-vehicle research passes should land in a new
`commercial_profiles_v2.md` etc., same versioning discipline as
`profiles_v1.md` itself.

---

## Blue Bird Vision Electric (Type C school bus, 2024+ / 194 kWh pack generation)

**Why this vehicle, not the Lion Electric Lion C:** both were named as
candidates. Blue Bird's official, dated (2025-03-17) spec sheet gives a
directly-disclosed peak motor power/torque figure (315 hp / 2,400 ft-lbs)
and pack voltage (620V nominal) that the Lion C's public documentation does
not — every Lion C source found gave battery capacity and charging current
only, with voltage and motor specs absent. Blue Bird's propulsion supplier
(Cummins/Accelera) also separately publishes a full datasheet for the exact
battery pack module Blue Bird uses (the Accelera BP97E), which is what makes
several fields below (operating temperature range, C-rate limits, cooling
architecture) real sourced data rather than placeholders — a materially
stronger starting point for this project's `Sourced[T]`-per-field
methodology.

**Battery:** 194 kWh gross (2x Accelera BP97E packs, ~97kWh nominal each),
88% usable (~171 kWh usable, per Blue Bird's own "88% usable capacity"
statement about this pack generation). Li-ion NMC/graphite chemistry ("Li
Ion NMC-A" per Accelera; Blue Bird's own spec sheet says "Lithium ion
NMC/G"). 620V nominal, 583-689V operating range (single BP97E module
spec; the 194kWh system's exact series/parallel integration across two
modules is not independently confirmed, but Blue Bird's own spec sheet
states the same 620V nominal for the full system). Dry mass: 550kg per
BP97E module (Accelera datasheet) — two modules ≈ 1,100kg for the full
194kWh system, not independently confirmed for this specific application's
integration. Operating temperature range: −40°C to 50°C (Accelera BP97E
datasheet — real, pack-specific, not a generic placeholder). Peak charge
209kW at 50% SOC / continuous charge 82kW at 50% SOC; peak discharge 131kW
at 20% SOC / continuous discharge 105kW at 20% SOC — all per-BP97E-module
figures from the Accelera datasheet. Thermal management: liquid-cooled
(H2O/ethylene glycol 50:50 per the Accelera datasheet), with an active
refrigerant chiller — the Cummins PowerDrive 7000 system diagram
explicitly labels a "Thermal Management Module... central location for
pumps, heaters and A/C chillers for the PowerDrive system," serving the
battery, motor, and power electronics from one shared loop. Blue Bird's own
marketing copy also states "advanced preconditioning" and "enhanced cold
and hot weather performance" for this thermal system.

**Motor:** Cummins PowerDrive 7000 propulsion system, TM4 SUMO motor
(exact SUMO variant/model number not disclosed by either Blue Bird or
Cummins/Accelera in any source found). Peak: 315 hp (234.9 kW) / 2,400
ft-lbs (3,254 Nm) — directly stated, Blue Bird's own spec sheet, explicitly
labeled "peak." No continuous rating disclosed anywhere found. Motor mass
not disclosed.

**Charging:** CCS1 connector, AC (Level 2) and DC fast charge capable.
Level 2: ~8 hours to full charge (rate not explicitly stated in kW).
DC fast charge: ~2 hours to full charge; a separate source (Rush Truck
Centers, a Blue Bird dealer) states 120kW DC fast charging specifically,
consistent in order of magnitude with the BP97E's own continuous-charge
rating scaled to two modules (~164kW) minus vehicle-level derating.

**GVWR / dimensions:** Up to 33,000 lb GVWR (front axle rated 10,000 lb,
rear axle rated 23,000 lb — these are axle RATINGS that sum to GVWR, not
independent curb-weight evidence). Exterior width 96", overall height 123"
(across the base spec) or 124" per the dimensions table (minor
inconsistency within Blue Bird's own spec sheet, not resolved here).
Wheelbase 252"/273", seating up to 66/77 depending on wheelbase. Tires:
11R22.5. Curb weight is NOT disclosed anywhere found, despite searching
specifically for it — see the profile YAML's own note on how it was
estimated instead of left as an implausible passenger-car placeholder.

**Range:** Up to 150 miles per charge, "dependent on drive cycle, driver
behavior, accessories and HVAC usage" (Blue Bird's own phrasing — a real,
if unquantified, acknowledgment of duty-cycle-dependent range, directly
relevant to this project's whole teaching premise).

**Cabin heating:** Spec sheet lists a "90,000 btu front heater and
defroster" under base Comfort & Convenience features, and separately lists
an "Auxiliary fuel-fired heater" under Optional Features. Ambiguous whether
the base 90,000 BTU figure describes the standard electric-resistive heater
on this specific electric trim, or is boilerplate carried over from Blue
Bird's shared diesel/electric spec-sheet template — not resolved by any
source found. Used in the profile as a real, if uncertain, order-of-
magnitude figure for cabin heating load (a school bus's cabin heating draw
is a genuinely different scale than a passenger car's, and the existing
six profiles' shared 1000W placeholder would be a much worse, silently-
wrong stand-in for this vehicle class than a flagged estimate).

**Story:** the propulsion architecture — one shared coolant loop, with an
active refrigerant chiller, serving battery + motor + power electronics
together — most closely resembles the Kia Niro EV's integrated approach
among the six existing passenger-EV profiles, not the Leaf's passive-air or
the e-tron's four-independent-circuits design. This is the first profile in
this project with `chiller_present: true` on a cooling loop — see
docs/MILESTONES.md's existing "chiller path is not implemented in cooling.py"
deferred-work note; this profile is the first real vehicle that gap
actually affects (every one of the six passenger-EV profiles has
`chiller_present: false` everywhere).

---

## Fields intentionally NOT re-derived for this vehicle class

Per this project's established methodology (see `profiles_v1.md`'s own
framing and every existing profile YAML's header comment), fields outside
a vehicle-specific research source's scope are shared, non-vehicle-specific
placeholders reused verbatim across every profile — the point is to isolate
the researched variable (here: thermal architecture and a few electrical
specs) rather than have unresearched noise differ profile to profile. This
convention is followed for the school bus profile too: battery equivalent-
circuit internals (`r0_curve_ohm`, `r1_ohm`, `c1_farad`,
`internal_resistance_ohm`), all aging-rate coefficients, the charge-current-
limit curve, control-strategy placeholders, and most `AuxLoads` fields are
copied verbatim from the shared value used across all six passenger-EV
profiles — flagged per-field in the YAML, not silently reused.

**Two categories of field needed vehicle-class-appropriate values instead of
verbatim reuse, not just a vehicle-specific research number:**

1. **Vehicle-dynamics fields whose passenger-car placeholder would be
   physically implausible for a 33,000lb-GVWR bus** — `curb_mass_kg`,
   `frontal_area_m2`, `drag_coefficient`, `wheel_radius_m`, and motor
   `mass_kg`. Reusing the passenger-car placeholder (e.g. `curb_mass_kg:
   1800.0`) would make the simulated vehicle accelerate like a sports car,
   not merely be less accurate — a categorically different failure than an
   unresearched but plausible number. Each is estimated from real disclosed
   dimensions/geometry (tire size, exterior width/height, GVWR) or generic
   published heavy-vehicle engineering figures (drag coefficient, frontal-
   area shape factor), confidence: low, with the derivation shown in the
   profile YAML itself.
2. **The battery OCV curve**, which had to be rescaled (not reused verbatim)
   to this pack's real, disclosed 583-689V operating range — reusing the
   shared 300-395V curve verbatim would place the pack's own documented
   minimum operating voltage (583V) above its OCV curve's maximum (395V), an
   internal contradiction within the same profile, not just an unresearched
   placeholder. See the profile YAML's own note on the linear rescaling
   used.

## Derating-curve caveat

Same caveat as `profiles_v1.md`'s own: no proprietary derating curve was
found for this vehicle. The one real anchor point is the Accelera BP97E's
documented −40°C to 50°C operating range; the profile encodes a linear
derate ramp from an estimated onset (40°C) to that documented 50°C ceiling,
tagged `confidence: low`, same convention as every existing profile's
derate curve.
