"""Battery state-of-health (SOH) aging model: calendar aging (time/temperature/SOC)
plus cycle aging (throughput/temperature/C-rate) plus cold-charging lithium-plating
aging (throughput/temperature/C-rate, charge-only), combined additively, and SOH
feedback into a `BatterySpec`'s effective capacity and resistance.

Calendar aging: rate(T, SOC) = calendar_aging_rate_per_s
    * exp(calendar_aging_activation_temp_k * (1/reference_temp_k - 1/T))
    * (1 + calendar_aging_soc_sensitivity * (soc - 0.5))
applied as `rate * dt_s`.

Cycle aging: rate(T) = cycle_aging_rate_per_efc
    * exp(cycle_aging_activation_temp_k * (1/reference_temp_k - 1/T))
    * (applied_c_rate ** cycle_aging_c_rate_sensitivity)
applied as `rate * (|applied_current_a| * dt_s / 3600) / capacity_nominal_ah`
(this step's throughput expressed in equivalent full cycles, EFC, rather
than raw amp-hours — see "EFC normalization" below).

Cold-charging lithium plating: rate(T) = cold_charge_plating_rate_per_efc_per_k
    * max(0, cold_charge_plating_onset_temp_k - T) ** cold_charge_plating_temp_sensitivity
    * (applied_c_rate ** cold_charge_plating_c_rate_sensitivity)
applied the same way as cycle aging (EFC-normalized), but ONLY when
`applied_current_a < 0` (this project's charging convention) — zero during
discharge, and zero at/above the onset temperature. See "Cold-charging
lithium plating (Q-0028)" below for why this is a separate additive term
rather than a modification of the existing cycle-aging Arrhenius term.

Calendar, cycle, and cold-charge-plating deltas are simply summed (no
interaction term) per docs/MILESTONES.md's deliberate simplification for
this milestone.

## Coefficient provenance

`calendar_aging_rate_per_s`, `calendar_aging_activation_temp_k`, and
`calendar_aging_soc_sensitivity` were calibrated (not sourced from a single
literature value) to exactly fit two real-fleet anchor points, with
`calendar_aging_soc_sensitivity` fixed at 1.0 and the other two solved from
that choice:
  - Nissan Leaf, hot climate (Guam-like): 40 C (313.15 K) constant, 80% SOC
    constant, 5 years -> 70% SOH (30% loss).
  - Tesla Model 3, mild climate (Seattle-like): 20 C (293.15 K) constant,
    60% SOC constant, 10 years -> 90% SOH (10% loss).
These two anchors alone constrain two of the three unknowns for a given
choice of the third; `calendar_aging_soc_sensitivity=1.0` was chosen as a
moderate, round-number assumption (full-SOC aging 1.5x the SOC=0.5 baseline,
empty-SOC aging 0.5x), not fit from data.

The resulting `calendar_aging_activation_temp_k` (~7460 K) is an empirical
fit to these two points, not a physical constant, and is notably LOWER than
typical literature values for real Li-ion calendar-aging activation energies
expressed the same way (~20,000 K+). At this magnitude, the model's rate
roughly doubles (not triples) per 10 C — plausible in shape, low in
magnitude versus real cells, which is acceptable for a placeholder but means
this coefficient must never be "corrected" to a literature value without
re-deriving it against the same two anchors (doing so would break the
anchor fit). Do not use this value outside this simulator's context.

The two anchors span roughly 20-40 C ambient. SOH behavior predicted INSIDE
that envelope is calibrated; behavior predicted OUTSIDE it (e.g. Phoenix
summer highs near 50 C, or a Scandinavian winter near -10 C) is
extrapolation from an empirical fit with no data support there, and should
not be treated as validated.

Cycle-aging coefficients are calibrated against a single anchor (82% SOH at
100,000 kWh pack throughput at 25 C / 298.15 K), which is not enough data to
constrain all three cycle-aging unknowns simultaneously — three had to be
assumed outright (originally two; a third was added by the EFC-normalization
fix below):
  - The anchor's 100,000 kWh was converted to amp-hours using an assumed
    reference pack voltage of 370 V (a rough average across the six
    reference profiles), giving ~270,270 Ah, and an assumed reference
    C-rate of 1.0 (so `cycle_aging_c_rate_sensitivity` doesn't affect the
    calibration point itself).
  - `cycle_aging_c_rate_sensitivity = 1.0` (linear scaling with C-rate) was
    chosen as the simplest defensible assumption, not fit.
  - `cycle_aging_activation_temp_k` is completely UNCONSTRAINED by this
    anchor, since the anchor sits exactly at reference_temp_k (the
    Arrhenius term evaluates to 1.0 regardless of its value there). It was
    set to `calendar_aging_activation_temp_k / 2`, i.e. an assumption that
    cycle aging is half as temperature-sensitive as calendar aging, not a
    calibrated value.
  - **`cycle_aging_rate_per_efc` (EFC normalization, resolved via
    `docs/DESIGN_QUESTIONS.md` Q-0006/Q-0007):** the anchor's 100,000 kWh
    of throughput is, by itself, energy-throughput-shaped, not tied to any
    specific pack's capacity — converting it into equivalent full cycles
    (EFC) requires a THIRD assumed constant, a reference pack capacity,
    alongside the existing 370 V and 1C assumptions above. Assumed
    reference capacity: **75 kWh (~202.7 Ah at the same 370 V assumption)**
    — an explicit, generic placeholder, deliberately NOT the mean or
    median `capacity_nominal_ah` of the six actual reference profiles
    (that would carry a hidden "this represents our fleet" claim that a
    round, generic number doesn't). `EFC = 100,000 kWh / 75 kWh =
    1,333.33` (the 370 V assumption cancels out of this ratio, since both
    the throughput and the reference capacity were expressed through it).
    `cycle_aging_rate_per_efc = 0.18 / 1,333.33 = 1.350e-4` — the only
    genuinely solved value here, after all three assumptions above are
    fixed. Superseded a prior `cycle_aging_rate_per_ah = 6.660e-7` (solved
    the same anchor without EFC normalization; see the "EFC normalization"
    section below for why that formulation was replaced, not just
    renamed). **The original 100,000 kWh/82% SOH anchor was never tied to
    a real named vehicle or fleet, checked directly (docs/DESIGN_QUESTIONS.md
    Q-0007's own follow-up) — the 75kWh reference capacity isn't standing
    in for a discoverable real number, since none exists in this project's
    records.** `cycle_aging_rate_per_efc` scales EXACTLY linearly with this
    assumed reference capacity (confirmed empirically, not just derived:
    doubling/quadrupling it exactly doubles/quadruples `cycle_soh_loss_
    cumulative` for every profile) — real, not cosmetic, for the absolute
    cycle-aging number, but only a ~7% relative effect on the flagship
    comparison's headline A/B ratio across a 50-200kWh test range, since
    calendar aging dominates every currently-published scenario. Would
    become directly load-bearing for any future cycle-aging-dominated
    scenario. See Q-0007's follow-up for the full sensitivity check.
Treat every cycle-aging coefficient as a placeholder assumption pending real
throughput-vs-temperature fleet or lab data, with more skepticism than the
calendar-aging coefficients above.

## EFC normalization (added after Milestone E2e's first real comparison)

`compute_aging_step` normalizes cycle-aging throughput into equivalent full
cycles (EFC) — `throughput_efc = throughput_ah / capacity_nominal_ah` —
rather than using raw amp-hours directly. This was a genuine fix, not a
rename: the prior `throughput_ah`-direct formulation made `cycle_delta`
scale EXACTLY linearly with pack capacity for matched C-rate/temperature
(hand-traced proof during the investigation that led to this fix: the
Leaf/Model 3 `cycle_delta` ratio matched their `capacity_nominal_ah` ratio
to three decimal places, 3.017 both ways), making pack size — not just
thermal architecture — a live confound in any cross-vehicle cycle-aging
comparison. EFC normalization removes that confound: two packs cycled at
the same C-rate and temperature for the same number of equivalent full
cycles now accrue the same cycle-aging delta regardless of absolute size.
**`capacity_nominal_ah` is deliberately the pack's fixed, UNAGED nominal
capacity — never the current SOH-scaled effective capacity — per
`docs/DESIGN_QUESTIONS.md` Q-0006.** Normalizing by current capacity would
be more physically precise at low SOH (a real EFC is conventionally
relative to a pack's present capacity), but it introduces an uncalibrated
positive-feedback mechanism this simulator does not model or validate
anywhere else: a pack's own past aging would change the denominator of its
own future cycle-aging rate (SOH -> EFC rate -> SOH). That feedback loop,
not simplicity, is why nominal capacity was chosen — this is a bounded
approximation, most accurate at SOH >= 0.7; below that, treat cycle-aging
trajectories with more skepticism, since the fixed-nominal-capacity
denominator increasingly understates each EFC's true throughput relative
to the pack's shrinking actual capacity. Revisit if a future milestone
specifically targets low-SOH, near-end-of-life runs.

## Cold-charging lithium plating (Q-0028)

A real, physically distinct mechanism from the cycle-aging term above, not a
tuning of it. `cycle_aging_activation_temp_k`'s Arrhenius term makes cycle
aging monotonically WORSE as temperature rises and monotonically BETTER as it
falls — correct for the SEI-growth-dominated aging that term's coefficients
represent, but wrong, not just incomplete, for charging current specifically:
real cells charged at low temperature suffer lithium plating (metallic
lithium deposits on the graphite anode instead of intercalating, because low
temperature slows solid-state diffusion/intercalation kinetics faster than it
slows the charge current pushing ions in), which gets WORSE, not better, as
temperature drops. This is charge-specific — discharge doesn't push lithium
into the anode, so it isn't at risk the same way — hence the `applied_current_a
< 0` gate (this project's charging convention, confirmed in `battery.py`).
`battery.py`'s `_cold_derate_scale`/`available_charge_current_a` already model
the BMS's defensive response (throttling charge current when cold), but a
derate curve that reduces available current is not the same claim as "the
current that DOES flow at that reduced rate causes zero extra aging" — a real
BMS's derate curve keeps plating within an acceptable envelope, not zero, so
this term charges a residual, real cost on whatever current actually flows
cold, complementary to (not redundant with) the derate curve.

**Why a separate additive term, not a modified `cycle_aging_activation_temp_k`
(see docs/DESIGN_QUESTIONS.md Q-0028 for the full options considered):**
changing that term's functional form (rather than its value) would silently
break its documented provenance and risk corrupting behavior at every
temperature, not just cold-charging ones. A new, independent term keeps the
existing anchors untouched and is separately attributable in
`AgingStepResult.cold_charge_plating_delta` /
`LongHorizonCheckpoint.cold_charge_plating_soh_loss_cumulative` — matching
this project's causal-trace goal (a mechanism this different shouldn't be
merged into a less-specific bucket).

**Coefficient provenance — real literature anchors, one citation-hop removed
for the rate constant, not invented (see Q-0028 for the full survey,
including why no clean fleet-level anchor was found: Geotab's own 2026
reports explicitly disclaim insufficient cold-only fleet samples to isolate
this, and Leaf fleet data is dominated by the well-known hot-climate signal
instead):**
  - `cold_charge_plating_onset_temp_k = 293.15` (20°C): from Yang & Wang,
    "Understanding the Trilemma of Fast Charging, Energy Density and Cycle
    Life of Lithium-ion Batteries" (Penn State ECEC, DOE/OSTI-hosted,
    peer-reviewed physics-based aging model, read directly) — their
    simulated PHEV cell's no-plating floor at 1C charge (the reference C-rate
    used throughout this schema's other cycle-aging coefficients). Real
    cells' onset temperature RISES with C-rate (their own finding: ~20°C
    floor at 1C, ~40°C at 2C, >50°C at 3C) — held fixed at the 1C reference
    value here rather than modeling the full C-rate-coupled onset surface,
    a deliberate first-pass simplification (this project's cycle-aging model
    already makes similar single-reference-condition simplifications
    elsewhere, e.g. `cycle_aging_activation_temp_k` being assumed rather
    than derived).
  - `cold_charge_plating_rate_per_efc_per_k = 0.001`: solved from a single
    anchor point, the same one-anchor-many-unknowns situation the original
    cycle-aging coefficients were in (see "Coefficient provenance" above).
    The anchor: Matadi et al., cited directly within Yang & Wang's paper (a
    real, experimental number — but relayed through Yang & Wang's citation
    of it, not read from Matadi et al.'s own paper) — a 16Ah graphite/NMC
    cell lost 75% capacity in 50 cycles at 5°C. Treating 1 test cycle ≈ 1
    EFC (this project's own convention elsewhere) gives a target rate of
    0.75/50 = 0.015 per EFC at 5°C, 15K below the 293.15K onset. With
    `cold_charge_plating_temp_sensitivity` assumed at 1.0 (linear — the
    single anchor point can constrain at most one free parameter beyond the
    rate itself, so the exponent had to be assumed, same reasoning
    `cycle_aging_c_rate_sensitivity`'s own assumption used), solving
    `0.015 = rate * 15^1.0` gives `rate = 0.001`. **A known, flagged
    imprecision, not glossed over**: Matadi et al.'s cited 4000-cycles-at-
    25°C figure and the 50-cycles-at-5°C figure use different implied
    end-of-life thresholds (conventionally ~20-30% for the former, an
    explicit 75% for the latter), so this ratio likely OVERSTATES the
    like-for-like rate difference somewhat — treat this coefficient with
    the same or greater skepticism this project already applies to the
    original cycle-aging anchor, and revisit if Matadi et al.'s own paper is
    obtained directly. Matadi et al.'s own charge C-rate for this test was
    not available from the citing source either — assumed to be the same
    1.0 reference C-rate used throughout, not vehicle- or test-specific.
  - `cold_charge_plating_temp_sensitivity = 1.0` and
    `cold_charge_plating_c_rate_sensitivity = 1.0`: both the simplest
    defensible assumption (linear), not fit — identical reasoning to
    `cycle_aging_c_rate_sensitivity`'s own precedent. A real onset-adjacent
    curve is more likely convex (Yang & Wang describe an accelerating,
    positive-feedback plating rate once onset is crossed, not a gentle
    linear ramp), but a single anchor point cannot constrain a higher
    exponent's value — flagged here, not silently assumed away.
  - **A relevant, if secondary, real-world validation of direction and
    magnitude for preconditioning specifically** (also Yang & Wang, their
    own prior work, not part of this term's calibration): a cell with an
    internal rapid-heating element (~1°C/sec, reaching an "optimal" charge
    temperature before charging starts) sustained 4500 cycles of 3.5C
    charging at 0°C before losing 20% capacity, vs. only 50 cycles for an
    identical cell with no preheating step under the same condition — a
    ~90x cycle-life improvement from real preconditioning done aggressively
    enough to reach the cell's true optimal temperature. Kia Niro EV's real
    preconditioning heater (2kW, external, resistive, 25-minute budget) is a
    far weaker intervention than this — its own net-negative SOH finding
    (`docs/MILESTONES.md`) and this term's existence are consistent with
    each other, not in tension; this term gives preconditioning something
    real to protect against for the first time.

Treat every cold-charge-plating coefficient with at least as much skepticism
as the cycle-aging coefficients above — this is newer, less-anchored, and
known to carry a real EOL-threshold-mismatch imprecision in its single rate
anchor.

## Shared coefficients across all six reference vehicles (deliberate)

All six profiles use IDENTICAL aging coefficients. This is a deliberate
experimental-design choice, not an oversight or a shortcut: this project's
teaching goal is to demonstrate how *thermal architecture* (passive air vs.
liquid-cooled vs. DX-refrigerant, etc.) changes battery longevity outcomes.
Real cell chemistry (LMO for the Leaf, NCA for the Model 3/Bolt, NMC for the
Niro, Samsung cells for the i3) does age differently, and giving each
profile its own chemistry-specific aging coefficients would be a more
complete model — but it would also confound the comparison: any SOH
difference between two profiles could then be attributed to either thermal
design OR chemistry, muddying the exact lesson this simulator exists to
teach. Holding aging coefficients constant across all six profiles makes
thermal architecture the sole independent variable for calendar aging AND
(after the EFC-normalization fix below) EFC-normalized cycle aging.
Per-vehicle chemistry-specific aging coefficients are deferred work (see
docs/MILESTONES.md) until the thermal story has been demonstrated in isolation.

**Cycle aging previously did NOT satisfy this claim — discovered during
Milestone E2e's first real Leaf-vs-Model3 comparison, confirmed by direct
hand-trace, and since fixed via EFC normalization (see "EFC normalization"
below).** The original `cycle_delta = cycle_rate_per_ah * throughput_ah`
formulation used an ABSOLUTE amp-hour quantity, never normalized by pack
capacity, so `cycle_delta` scaled EXACTLY linearly with `capacity_nominal_ah`
for matched C-rate/temperature (verified: the Leaf/Model 3 `cycle_delta`
ratio matched their capacity ratio to three decimal places, 3.017 both
ways) — pack size, not just thermal architecture, was a live confounding
variable. Fixed by normalizing throughput into equivalent full cycles
(EFC) before applying the rate constant, removing the capacity dependence
for matched relative usage. Calendar aging never had this problem — it has
no capacity or current term at all.

**The two calendar-aging anchors above are idealized, single-condition,
calendar-ONLY benchmarks (constant temperature, constant SOC, zero
current) -- verified to reproduce EXACTLY (0.7000 and 0.9000 SOH,
matching their stated targets to four decimal places) when the actual code
is run under their exact stated conditions. They are NOT a claim that a
realistic mixed-usage scenario (varying temperature, varying SOC, real
driving/charging cycling) will show the same ratio.** A 90-day mixed
scenario with a 40C-mean/oscillating-SOC Leaf profile produced a calendar-
only loss matching a direct calculation at that scenario's own average SOC
(~0.65) almost exactly, NOT the anchor's constant 80% SOC condition -- the
anchor and a realistic scenario are different questions, not a discrepancy.
Do not treat "the flagship comparison's ratio looks smaller than the
anchors" as evidence of a calibration regression without first checking
whether the comparison scenario's conditions actually match an anchor's.
"""

from __future__ import annotations

import math

from ev_thermal.schema import AgingStepResult, BatterySpec, Sourced


def _arrhenius_scale(activation_temp_k: float, reference_temp_k: float, temp_k: float) -> float:
    return math.exp(activation_temp_k * (1.0 / reference_temp_k - 1.0 / temp_k))


def compute_aging_step(
    spec: BatterySpec,
    soh: float,
    cell_temp_k: float,
    soc: float,
    applied_current_a: float,
    dt_s: float,
) -> AgingStepResult:
    """Advance `soh` by one step's worth of calendar + cycle + cold-charge-plating aging.

    `spec` must be the pack's UNAGED spec (reference coefficients), not an
    already-SOH-scaled one from `apply_soh_to_battery_spec` — aging rate
    coefficients don't themselves change with SOH.
    """
    reference_temp_k = spec.reference_temp_k.value

    calendar_rate_per_s = (
        spec.calendar_aging_rate_per_s.value
        * _arrhenius_scale(spec.calendar_aging_activation_temp_k.value, reference_temp_k, cell_temp_k)
        * (1.0 + spec.calendar_aging_soc_sensitivity.value * (soc - 0.5))
    )
    calendar_delta = max(0.0, calendar_rate_per_s * dt_s)

    throughput_ah = abs(applied_current_a) * dt_s / 3600.0
    throughput_efc = throughput_ah / spec.capacity_nominal_ah.value
    c_rate = abs(applied_current_a) / spec.capacity_nominal_ah.value
    cycle_rate_per_efc = (
        spec.cycle_aging_rate_per_efc.value
        * _arrhenius_scale(spec.cycle_aging_activation_temp_k.value, reference_temp_k, cell_temp_k)
        * (c_rate**spec.cycle_aging_c_rate_sensitivity.value if c_rate > 0.0 else 0.0)
    )
    cycle_delta = max(0.0, cycle_rate_per_efc * throughput_efc)

    is_charging = applied_current_a < 0.0
    temp_below_onset_k = spec.cold_charge_plating_onset_temp_k.value - cell_temp_k
    if is_charging and temp_below_onset_k > 0.0 and c_rate > 0.0:
        plating_rate_per_efc = (
            spec.cold_charge_plating_rate_per_efc_per_k.value
            * temp_below_onset_k**spec.cold_charge_plating_temp_sensitivity.value
            * c_rate**spec.cold_charge_plating_c_rate_sensitivity.value
        )
        cold_charge_plating_delta = max(0.0, plating_rate_per_efc * throughput_efc)
    else:
        cold_charge_plating_delta = 0.0

    new_soh = max(0.0, soh - calendar_delta - cycle_delta - cold_charge_plating_delta)
    return AgingStepResult(
        new_soh=new_soh,
        calendar_delta=calendar_delta,
        cycle_delta=cycle_delta,
        cold_charge_plating_delta=cold_charge_plating_delta,
    )


def apply_soh_to_battery_spec(spec: BatterySpec, soh: float) -> BatterySpec:
    """Return a copy of `spec` with capacity/resistance scaled for the given SOH.

    Leaves every other field, and the raw aging-rate coefficients themselves,
    untouched -- callers pass this aged copy to `battery.py`/`controller.py`
    for capability/ceiling math, while `compute_aging_step` always takes the
    original unaged `spec`.
    """
    scaled_capacity_ah = Sourced[float](
        value=spec.capacity_nominal_ah.value * soh,
        confidence=spec.capacity_nominal_ah.confidence,
        source=spec.capacity_nominal_ah.source,
        note="SOH-scaled from capacity_nominal_ah for the current simulated pack age.",
    )
    resistance_scale = 1.0 + spec.resistance_growth_per_soh_loss.value * (1.0 - soh)
    scaled_r0_curve = [
        (
            soc_point,
            Sourced[float](
                value=r0_point.value * resistance_scale,
                confidence=r0_point.confidence,
                source=r0_point.source,
                note="SOH-scaled from r0_curve_ohm for the current simulated pack age.",
            ),
        )
        for soc_point, r0_point in spec.r0_curve_ohm
    ]
    return spec.model_copy(update={"capacity_nominal_ah": scaled_capacity_ah, "r0_curve_ohm": scaled_r0_curve})
