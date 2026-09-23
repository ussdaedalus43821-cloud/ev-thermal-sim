"""Battery state-of-health (SOH) aging model: calendar aging (time/temperature/SOC)
plus cycle aging (throughput/temperature/C-rate), combined additively, and SOH
feedback into a `BatterySpec`'s effective capacity and resistance.

Calendar aging: rate(T, SOC) = calendar_aging_rate_per_s
    * exp(calendar_aging_activation_temp_k * (1/reference_temp_k - 1/T))
    * (1 + calendar_aging_soc_sensitivity * (soc - 0.5))
applied as `rate * dt_s`.

Cycle aging: rate(T) = cycle_aging_rate_per_ah
    * exp(cycle_aging_activation_temp_k * (1/reference_temp_k - 1/T))
    * (applied_c_rate ** cycle_aging_c_rate_sensitivity)
applied as `rate * |applied_current_a| * dt_s / 3600` (amp-hours of throughput
this step).

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
constrain all three cycle-aging unknowns simultaneously — two had to be
assumed outright:
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
  - `cycle_aging_rate_per_ah` is the only genuinely solved value here, and
    only after the two assumptions above were fixed.
Treat every cycle-aging coefficient as a placeholder assumption pending real
throughput-vs-temperature fleet or lab data, with more skepticism than the
calendar-aging coefficients above.

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
thermal architecture the sole independent variable, so any SOH divergence
between vehicles in a run is attributable entirely to how well each
profile's thermal system keeps the pack in its calibrated temperature
envelope. Per-vehicle chemistry-specific aging coefficients are deferred
work (see CLAUDE.md) until the thermal story has been demonstrated in
isolation.
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
    c_rate = abs(applied_current_a) / spec.capacity_nominal_ah.value
    cycle_rate_per_ah = (
        spec.cycle_aging_rate_per_ah.value
        * _arrhenius_scale(spec.cycle_aging_activation_temp_k.value, reference_temp_k, cell_temp_k)
        * (c_rate**spec.cycle_aging_c_rate_sensitivity.value if c_rate > 0.0 else 0.0)
    )
    cycle_delta = max(0.0, cycle_rate_per_ah * throughput_ah)

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
