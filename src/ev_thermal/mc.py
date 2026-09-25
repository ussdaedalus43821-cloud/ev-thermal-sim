"""Monte Carlo orchestration: perturbs a `ManufacturerProfile`'s aging-related
`Sourced` fields many times per an `MCConfig`, runs Milestone E2's Tier-2
long-horizon extrapolation for each perturbation, and aggregates the
resulting SOH trajectories into a percentile band.

## v1 scope (see docs/DESIGN_QUESTIONS.md Q-0008)

Only `BatterySpec`'s seven aging-related `Sourced[float]` fields are
sampled: `calendar_aging_rate_per_s`, `calendar_aging_activation_temp_k`,
`calendar_aging_soc_sensitivity`, `cycle_aging_rate_per_efc`,
`cycle_aging_activation_temp_k`, `cycle_aging_c_rate_sensitivity`,
`resistance_growth_per_soh_loss`. None of these feed into Tier-1 physics
(`characterize_trip` only reads thermal/electrical specs) -- only into
`aging.compute_aging_step`, called during Tier-2 replay -- so Tier-1
characterization is computed ONCE and reused across every sample
(`characterize_all_trips`'s own docstring already documents this reuse
pattern; this is the first caller that actually exercises it this way).
Sampling thermal/electrical specs or climate would force re-running Tier-1
per sample (~500x the cost) and is deferred to a later milestone.

## Sampling: confidence -> log-normal spread (see Q-0009)

Every sampled field is drawn from a log-normal distribution whose MEDIAN is
its `value` (not its mean -- a log-normal's median is `exp(mu)` directly,
with no `sigma`-dependent correction, which is why sampling is unbiased at
z=0 regardless of spread), with a coefficient of variation (CV = std/mean
of the resulting distribution) determined ENTIRELY by its `confidence`
tier, via ONE fixed, project-wide mapping -- stated once, here, and
referenced from every sampled field, not decided per-field (per-field
spreads would reintroduce exactly the confound class the EFC-normalization
fix, see aging.py, just removed):

    Confidence.LOW    -> CV = 0.50
    Confidence.MEDIUM -> CV = 0.20
    Confidence.HIGH   -> CV = 0.05

For a log-normal distribution parameterized by CV around a target median m:
    sigma = sqrt(ln(1 + CV^2))
    mu = ln(m)
    sample = exp(mu + sigma * z), z ~ N(0, 1)

(CV itself is a property of `sigma` alone -- `CV = sqrt(exp(sigma^2) - 1)`
-- so this sigma achieves the target CV regardless of how `mu` is chosen;
median-centering, not mean-centering, is what makes `MCPercentileBand.p50`
track the unperturbed point estimate, which is exactly what
`test_mc_p50_approximates_flagship_point_estimate` in `tests/test_mc.py`
checks and what caught this distinction during v1's own implementation.)

Sampled fields are drawn INDEPENDENTLY of each other -- no covariance or
shared latent parameter is modeled for v1. This is a deliberately
conservative choice: independent sampling produces the WIDEST plausible
band, since any true correlation between coefficients would narrow it, not
widen it. Revisit only if a real fleet-covariance estimate becomes
available.

## Two explicit caveats on every `MCResult` (see Q-0009 and the schedule-seed decision)

1. **The band is uncertainty around a PLACEHOLDER aging-coefficient value,
   not uncertainty around real battery chemistry.** Same honesty register
   as aging.py's own anchor-idealization note (see Q-0007). Do not present
   an MC band as a real-world confidence interval until the underlying
   coefficients are sourced, not assumed.
2. **The band is also conditional on one fixed day-type schedule.**
   `simulate_long_horizon`'s only internal RNG consumer is
   `shuffle_day_type_schedule` (confirmed by reading the function body, not
   assumed), driven entirely by its own `seed` argument. `run_monte_carlo`
   holds that seed fixed (`scenario_template.random_seed`) across every
   sample -- a separate, independent RNG stream (derived from
   `MCConfig.random_seed` via `numpy.random.SeedSequence.spawn`) drives
   ONLY the aging-coefficient sampling. Schedule-order variance is real
   (especially at short horizons) but not sampled in v1 -- this band
   represents sensitivity to aging-coefficient uncertainty only. Sampling
   schedule-order variance is a separate Monte Carlo mode, deferred.

## p50 vs. the point estimate: a Jensen's-inequality gap that grows with horizon

`MCResult.soh_band[i].p50` and a direct, unperturbed `long_horizon.
simulate_long_horizon` call's SOH at the same checkpoint are DIFFERENT
OBJECTS BY CONSTRUCTION, not by accident, and the gap between them grows
with horizon -- this is expected of ANY correctly-built Monte Carlo over
this model, not a defect to fix here.

**Why**: the point estimate evaluates the aging model once, at each
coefficient's central (median) value. The MC instead pushes a full
distribution of coefficient values through the same model and reports the
resulting OUTPUT distribution's own median. Both `calendar_delta` and
`cycle_delta` (`aging.compute_aging_step`) are Arrhenius-exponential in a
sampled activation-temperature coefficient, and deltas are summed
additively across every step of a multi-year run -- integrating a convex,
monotonic function of a randomly-sampled input over many compounding steps
is a textbook setting for Jensen's inequality to bite AT THE MEDIAN, not
just the mean: an unbiased median INPUT (guaranteed by `sample_lognormal`'s
median-centering, see above) does not imply an unbiased median OUTPUT once
that input is run through a nonlinear map and compounded over time. This is
intrinsic to the model's own Arrhenius/exponential structure, not an
artifact of `sample_lognormal`'s implementation or of the parked-SOC model
(`long_horizon.py`, Milestone "parked-SOC model") -- confirmed by direct
investigation (`examples/mc_horizon_gap_investigation.py`, and see
CLAUDE.md's "Multi-year Monte Carlo v1" entry for the full record): the gap
is already present, smaller, at 90 days and 1 year, horizons where NO
sample has come anywhere near the SOH floor, and it grows smoothly and
monotonically with horizon (roughly 0.03% at 90 days -> 6.3% at 20 years
for the Nissan Leaf / Guam scenario) with no discontinuity where a
SOC-model-specific effect would show up instead.

**Direction is informative, not incidental**: `p50` comes in BELOW the
point estimate, consistently, at every horizon checked. That's expected,
not arbitrary -- "ages faster" is unbounded/exponential in this model,
while "ages slower" is bounded below by `aging.py`'s explicit
`max(0.0, ...)` floor (SOH can't go negative). The resulting SOH
distribution is left-skewed: bad coefficient draws pull the tail out
further than good draws can compensate, and a skewed distribution's median
sits on the side its longer tail points away from. A literal `p05=0.0%`
seen at a long enough horizon is this same effect reaching its own limit --
a real fraction of sampled coefficient draws, compounded linearly for many
years with no saturation term (see `docs/model_reference.md`'s "Validated
time horizon" note), cross zero and sit at the floor -- not a distinct bug.

**Practical consequence**: at a long enough horizon, the unperturbed point
estimate stops being a "central case" description of the model's own
output under its own stated coefficient uncertainty. "The Leaf reaches
~47% SOH at 20 years" (the point estimate) and "the Leaf reaches ~44% SOH
at 20 years" (the MC median, same scenario) are different claims, and only
the second describes what happens more often than not once coefficient
uncertainty is accounted for. Do not present a long-horizon point estimate
as "the median/typical case" without this caveat, and do not treat a
smaller p50-vs-point-estimate agreement at short horizons as proof the
same agreement holds at long ones -- it doesn't, by construction, and the
divergence is the expected shape, not noise.

This explanation is evidence-consistent, not a formal derivation -- read it
as "the mechanism most consistent with what was actually measured," not as
a proven theorem about this specific aging formulation.

## Percentile support at N=500 (see Q-0011)

The band is defined as p05/p50/p95, not a wider tail. At N=500 (this
project's own target sample count), sampling error on the p05 estimate is
roughly +/-0.7% absolute, which does not support a defensible p01/p99
estimate -- producing wider percentiles the sample size can't actually
support would be manufacturing false precision. Raise `n_runs` to 2000 if a
wider band is ever needed; cheap, since only Tier-2's per-sample replay
cost scales with `n_runs` (Tier-1 characterization is computed once
regardless of `n_runs`).
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np

from ev_thermal.long_horizon import characterize_all_trips, simulate_long_horizon
from ev_thermal.schema import (
    AnnualDayTypeMix,
    BatterySpec,
    Confidence,
    LongHorizonCheckpoint,
    ManufacturerProfile,
    MCConfig,
    MCPercentileBand,
    MCResult,
    MCSampleResult,
    Scenario,
    Sourced,
)

CV_BY_CONFIDENCE: dict[Confidence, float] = {
    Confidence.LOW: 0.50,
    Confidence.MEDIUM: 0.20,
    Confidence.HIGH: 0.05,
}

_BATTERY_PATH_PREFIX = "battery."


def _extract_final_soh(checkpoint: LongHorizonCheckpoint) -> float:
    return checkpoint.battery_soh


def _extract_final_calendar_loss(checkpoint: LongHorizonCheckpoint) -> float:
    return checkpoint.calendar_soh_loss_cumulative


def _extract_final_cycle_loss(checkpoint: LongHorizonCheckpoint) -> float:
    return checkpoint.cycle_soh_loss_cumulative


_OUTPUT_METRIC_EXTRACTORS: dict[str, Callable[[LongHorizonCheckpoint], float]] = {
    "final_soh": _extract_final_soh,
    "final_calendar_loss": _extract_final_calendar_loss,
    "final_cycle_loss": _extract_final_cycle_loss,
}


def sample_lognormal(median: float, confidence: Confidence, rng: np.random.Generator) -> float:
    """Draws one sample from a log-normal distribution whose MEDIAN is
    `median` (mu = ln(median), no mean-centering correction -- see this
    module's docstring), with spread set by `CV_BY_CONFIDENCE[confidence]`.
    """
    if median <= 0.0:
        raise ValueError(f"log-normal sampling requires a strictly positive value, got {median}")
    cv = CV_BY_CONFIDENCE[confidence]
    sigma = math.sqrt(math.log(1.0 + cv**2))
    mu = math.log(median)
    return float(rng.lognormal(mu, sigma))


def _resolve_battery_field_name(path: str) -> str:
    """v1's minimal resolver (see Q-0010): every v1-sampled parameter lives
    at `battery.<field_name>`. Not a general dotted-path walker -- raises on
    anything else rather than silently guessing.
    """
    if not path.startswith(_BATTERY_PATH_PREFIX):
        raise ValueError(
            f"Monte Carlo v1 only supports 'battery.<field>' parameter paths, got {path!r} "
            "-- see docs/DESIGN_QUESTIONS.md Q-0010."
        )
    return path.removeprefix(_BATTERY_PATH_PREFIX)


def sample_battery_spec(
    spec: BatterySpec, parameter_paths: list[str], rng: np.random.Generator
) -> tuple[BatterySpec, dict[str, float]]:
    """Draws one perturbed `BatterySpec` copy, sampling every field named in
    `parameter_paths` per `sample_lognormal`. Returns the perturbed spec and
    the drawn values keyed by their original dotted path, for
    `MCSampleResult.sampled_values` provenance.
    """
    updates: dict[str, Sourced[float]] = {}
    sampled_values: dict[str, float] = {}
    for path in parameter_paths:
        field_name = _resolve_battery_field_name(path)
        original = getattr(spec, field_name, None)
        if not isinstance(original, Sourced):
            raise ValueError(f"{path!r} does not resolve to a Sourced field on BatterySpec")
        drawn = sample_lognormal(original.value, original.confidence, rng)
        updates[field_name] = original.model_copy(update={"value": drawn})
        sampled_values[path] = drawn
    return spec.model_copy(update=updates), sampled_values


def _compute_percentile_band(samples: list[MCSampleResult]) -> list[MCPercentileBand]:
    n_days = len(samples[0].soh_trajectory)
    band: list[MCPercentileBand] = []
    for day_index in range(n_days):
        day_values = [s.soh_trajectory[day_index] for s in samples]
        p05, p50, p95 = np.percentile(day_values, [5.0, 50.0, 95.0])
        band.append(MCPercentileBand(day_index=day_index, p05=float(p05), p50=float(p50), p95=float(p95)))
    return band


def run_monte_carlo(
    profile: ManufacturerProfile,
    scenario_template: Scenario,
    day_type_mix: list[AnnualDayTypeMix],
    config: MCConfig,
    duration_years: int,
) -> MCResult:
    """Runs `config.n_runs` perturbed long-horizon samples and aggregates them.

    `scenario_template.random_seed` is the FIXED schedule seed shared by
    every sample (see this module's docstring, caveat 2) -- a separate
    stream derived from `config.random_seed` drives only the aging-
    coefficient sampling. Tier-1 characterization runs exactly once,
    against `profile`/`scenario_template` unperturbed, and is reused for
    every sample (see this module's docstring, "v1 scope").
    """
    trip_characterizations = characterize_all_trips(profile, scenario_template)
    child_seeds = np.random.SeedSequence(config.random_seed).spawn(config.n_runs)

    samples: list[MCSampleResult] = []
    for sample_index, child_seed in enumerate(child_seeds):
        rng = np.random.default_rng(child_seed)
        perturbed_battery, sampled_values = sample_battery_spec(profile.battery, config.parameters_to_vary, rng)
        perturbed_profile = profile.model_copy(update={"battery": perturbed_battery})

        result = simulate_long_horizon(
            profile=perturbed_profile,
            trip_characterizations=trip_characterizations,
            day_type_mix=day_type_mix,
            climate=scenario_template.climate,
            charging_profile=scenario_template.charging_profile,
            duration_years=duration_years,
            seed=scenario_template.random_seed,
        )

        last_checkpoint = result.checkpoints[-1]
        endpoint_metrics: dict[str, float] = {}
        for metric_name in config.output_metrics:
            if metric_name not in _OUTPUT_METRIC_EXTRACTORS:
                raise ValueError(
                    f"unrecognized MCConfig.output_metrics entry {metric_name!r}; "
                    f"supported names: {sorted(_OUTPUT_METRIC_EXTRACTORS)}"
                )
            endpoint_metrics[metric_name] = _OUTPUT_METRIC_EXTRACTORS[metric_name](last_checkpoint)

        samples.append(
            MCSampleResult(
                sample_index=sample_index,
                sampled_values=sampled_values,
                soh_trajectory=[c.battery_soh for c in result.checkpoints],
                endpoint_metrics=endpoint_metrics,
            )
        )

    return MCResult(config=config, samples=samples, soh_band=_compute_percentile_band(samples))
