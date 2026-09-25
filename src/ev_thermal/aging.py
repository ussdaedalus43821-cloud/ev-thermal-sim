"""Battery state-of-health (SOH) aging model: calendar aging (time/temperature/SOC)
plus cycle aging (throughput/temperature/C-rate), combined additively, and SOH
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

Calendar and cycle deltas are simply summed (no interaction term) per
CLAUDE.md's deliberate simplification for this milestone.

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
    renamed).
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
CLAUDE.md) until the thermal story has been demonstrated in isolation.

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
    """Advance `soh` by one step's worth of calendar + cycle aging.

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

    new_soh = max(0.0, soh - calendar_delta - cycle_delta)
    return AgingStepResult(new_soh=new_soh, calendar_delta=calendar_delta, cycle_delta=cycle_delta)


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
