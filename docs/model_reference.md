# Model Reference

## Per-profile confidence summary

Each of the six `configs/profiles/*.yaml` files is built from
`docs/research/profiles_v1.md`, a human-compiled research dump that is
itself not verified against primary sources beyond what it cites. Every
uncertain field in each profile is wrapped in `Sourced[T]` with a
`confidence` tag (`high`: manufacturer specs, patents, or recall/NHTSA
documents; `medium`: teardowns or aggregated fleet data; `low`: owner
reports, forum data, general Li-ion principles, or — for the large share of
fields the research doesn't cover at all — a placeholder reused identically
across all six profiles, see below).

The counts below are computed directly from each profile's actual `Sourced`
fields (via a recursive walk over the loaded `ManufacturerProfile`, the same
one `tests/test_profiles.py` uses), not estimated by eye.

| Vehicle | Total `Sourced` fields | High | Medium | Low |
|---|---:|---:|---:|---:|
| Nissan Leaf | 101 | 8 | 2 | 91 |
| Tesla Model 3 | 113 | 12 | 2 | 99 |
| Chevrolet Bolt | 111 | 10 | 2 | 99 |
| Audi e-tron | 113 | 9 | 6 | 98 |
| BMW i3 | 102 | 9 | 4 | 89 |
| Kia Niro EV | 102 | 8 | 5 | 89 |

**Why "low" dominates every profile**: `VehicleDynamics` (mass, drag,
gearing, etc.), `AuxLoads`, most of the battery's equivalent-circuit
parameters (OCV/R0/R1/C1 curves), the inverter's internals, and most
`CoolingLoop` hydraulic specifics are **not covered by the research at
all**, and are reused identically across all six profiles as low-confidence
placeholders (`source: "placeholder — outside docs/research/profiles_v1.md's
scope, not vehicle-specific"`). This is a deliberate experimental-design
choice, not an oversight: it isolates the thermal-architecture variable for
cross-vehicle comparison, so that any divergence in a simulation run is
attributable to the researched thermal fields, not to un-researched chassis
or electrical differences. See `nissan_leaf.yaml`'s header comment for the
full rationale, and CLAUDE.md's deferred-work notes for what a future
milestone would need to source to change this.

Separately, **every** `derate_temp_k`/`max_temp_k` pair and
`control_strategy.derate_curve` entry across all six profiles is tagged
`confidence: low` regardless of how well-documented the underlying
temperature number is — real derating curves are proprietary and
unpublished, so the linear-ramp *shape* is always an estimate, even where a
manufacturer clearly states an operating-range boundary (e.g. the BMW i3's
documented 60°C ceiling). This is intentional per the profile-population
milestone's explicit instruction, not an inconsistency.

### Nissan Leaf (Gen 1, 2011-2017)

**Reliable:** battery capacity (24/21.3 kWh nominal/usable), voltage,
chemistry, and the fact of passive-air-only battery cooling; motor peak
power/torque and its liquid cooling. **Guess:** almost everything else —
pack mass, motor type, inverter, all equivalent-circuit parameters, and
(per the derate-curve policy above) the exact derate/max temperatures,
though the motor's derate band at least starts from real ORNL lab test
points rather than a pure default.

### Tesla Model 3 (octovalve config, 2017+)

**Reliable:** battery capacity (gross/usable), voltage, pack mass, liquid
cooling mechanism, and the octovalve heat pump's motor/inverter waste-heat
recovery. **Guess:** this profile collapses the real front+rear AWD
configuration into a single rear-motor entry (research gives both motors'
power but only a combined torque figure for D-mode-style outputs elsewhere,
not per-motor), and — like every profile — vehicle dynamics, aux loads, and
equivalent-circuit parameters are all placeholder.

### Chevrolet Bolt (2017-2022)

**Reliable:** battery capacity, mass, voltage, DEX-COOL liquid cooling with
chiller assist, motor power/torque/cooling detail (down to "two oil jets
per side"), and — uniquely among these six — a real, vehicle-specific
`gear_ratio` (7.05:1) rather than the shared placeholder. **Guess:** heat
pump presence is defaulted to `false` on total research silence (the
research never mentions a heat pump for the Bolt either way), and most
cooling-loop hydraulics are placeholder. Note: the Bolt's real-world story
(a fire-risk recall from LG Chem cell manufacturing defects) is a
cell-quality issue this profile's thermal fields don't and can't represent.

### Audi e-tron (2019+)

**Reliable:** battery capacity (gross/usable), voltage, the "hardly
possible to derate" qualitative finding (reflected as a high derate/max
threshold), motor combined power/torque, rotor-internal/stator-jacket/
bearing-plate cooling detail, and heat-pump presence. **Guess:** pack mass
(not stated), the real four-independent-circuit cooling topology is
simplified to two loops here, and per-motor torque isn't split (only a
combined D-mode figure exists in the research). Also worth flagging: this
profile's `heat_pump_recovers_powertrain_waste_heat` is `false`, not
`true` — the research says the e-tron's heat pump recovers waste heat
*from the battery*, not from the motor/inverter, a distinct capability
from Tesla's or Kia's.

### BMW i3 (22 kWh base trim, 2013+)

**Reliable:** battery capacity, voltage (midpoint of a stated range), mass,
the direct-expansion-refrigerant cooling mechanism in detail, motor power/
torque/mass/water-jacket cooling, and — the profile that motivated this
milestone's `min_charge_temp_k` schema addition — a genuinely asymmetric,
directly-researched charge (0°C) vs. discharge (−25°C) cold cutoff.
**Guess:** heat pump type for the base trim (resistive assumed, since
research only confirms a heat pump exists as an optional upgrade, not what
the base trim uses instead), and the usual placeholder equivalent-circuit/
inverter/hydraulics fields.

**Caution — battery ambient-resistance value is an approximation, not a
measurement:** `resistance_to_ambient_k_per_w` on the i3's battery
(`configs/profiles/bmw_i3.yaml`) stands in for a refrigerant cooling loop
that the schema does not yet support. It is NOT physically the ambient
resistance. Real i3 battery thermal behavior requires Milestone D
(refrigerant loop abstraction — see CLAUDE.md). Do not use i3-vs-Leaf
battery comparisons for publication until Milestone D lands.

### Kia Niro EV (64 kWh, heat-pump trim, 2018+)

**Reliable:** battery capacity, voltage, and amp-hour rating (180Ah is
*directly* stated, unlike every other profile's derived Ah figure), the
single shared coolant loop through battery/motor/inverter/heat pump, motor
power/torque, heat-pump waste-heat recovery from power electronics, and
battery preconditioning before fast charging. **Guess:** pack mass (not
stated), and — per the research's own noted caveat — this profile models
the *later*, full-featured configuration; research states early Niro
models actually lacked DC-fast-charge preconditioning, a temporal nuance
this static profile doesn't represent.

## Aging model confidence and limits

All six profiles' seven `calendar_aging_*`/`cycle_aging_*`/
`resistance_growth_per_soh_loss` `BatterySpec` fields (Milestone E1) are
identical across every vehicle, `confidence: low`, and derived, not
researched — none of them come from `docs/research/profiles_v1.md`. See
`aging.py`'s module docstring ("Coefficient provenance") for the full
derivation; this section is the short version for readers of this document.

**What was calibrated** (fit to exactly reproduce real anchor points, not
sourced from a single literature value):
- `calendar_aging_rate_per_s` and `calendar_aging_activation_temp_k` were
  solved from two fleet anchors, holding `calendar_aging_soc_sensitivity`
  fixed at an assumed 1.0:
  - Nissan Leaf, hot climate (Guam-like): 40°C, 80% SOC, constant, 5 years
    → 70% SOH.
  - Tesla Model 3, mild climate (Seattle-like): 20°C, 60% SOC, constant, 10
    years → 90% SOH.
- `cycle_aging_rate_per_ah` was solved from one throughput anchor (82% SOH
  at 100,000 kWh pack throughput at 25°C), assuming a 370V reference pack
  voltage and 1C reference C-rate to convert kWh to amp-hours.

**What was assumed, not calibrated** (no anchor constrains these):
- `calendar_aging_soc_sensitivity = 1.0` — a fixed, round-number choice.
- `cycle_aging_c_rate_sensitivity = 1.0` (linear scaling with C-rate) — the
  simplest defensible assumption.
- `cycle_aging_activation_temp_k` — set to half of
  `calendar_aging_activation_temp_k`, since the single cycle-aging anchor
  sits exactly at `reference_temp_k`, where the Arrhenius term evaluates to
  1.0 regardless of this coefficient's value.
- `resistance_growth_per_soh_loss = 1.0` — a placeholder ballpark (roughly
  doubling resistance by SOH=0), not sourced from teardown/fleet data.

**Validated temperature envelope:** the two calendar-aging anchors span
roughly 20-40°C ambient. **SOH trajectories inside 20-40°C are calibrated;
outside this range they are extrapolated.** A Phoenix summer (highs near
50°C) or a Scandinavian winter (near -10°C) will produce plausible-looking
but unvalidated SOH numbers — the model's shape (faster aging when hotter)
is directionally sound, but its magnitude outside the anchor envelope has
no data behind it. `calendar_aging_activation_temp_k`'s calibrated value
(~7460K) is also notably lower than typical published Li-ion calendar-aging
activation energies expressed the same way (~20,000K+) — this is expected
for a two-point empirical fit, not an error, but it means this coefficient
must never be "corrected" toward a literature value without re-solving
against the same two anchors, or the anchor fit breaks.

**Shared across all six vehicles by design:** real cell chemistry (LMO for
the Leaf, NCA for the Model 3/Bolt, NMC for the Niro, Samsung cells for the
i3) ages differently in reality, but every profile here uses the same aging
coefficients deliberately, so that thermal architecture is the sole
independent variable in any cross-vehicle SOH comparison this simulator
produces — see `aging.py`'s docstring for the full reasoning. Per-vehicle
chemistry-specific aging coefficients are deferred (see CLAUDE.md).
