# EV Thermal Simulator

It models six real manufacturer drivetrains (Nissan Leaf, Tesla Model 3, Chevrolet
Bolt, Audi e-tron, BMW i3, Kia Niro EV) plus user-editable counterfactuals,
over 20–30 year vehicle lifetimes, to teach why thermal management
decisions compound over a vehicle's life. A separate commercial-vehicle
track (a Blue Bird Vision Electric school bus) feeds a seam into the sister
project [Transition](https://github.com/ussdaedalus43821-cloud/transition).

## Status

Working simulator. Milestones A through E2e are done (see
[`docs/MILESTONES.md`](docs/MILESTONES.md)), and the test suite passes.
What is still open, placeholder or undecided is tracked in
[`docs/ROADMAP.md`](docs/ROADMAP.md) and
[`docs/DESIGN_QUESTIONS.md`](docs/DESIGN_QUESTIONS.md).

What exists today:

- **Physics** (`src/ev_thermal/`): battery electrical and thermal model,
  motor and inverter losses, vehicle road load, cooling loops, and a
  refrigerant chiller, stepped by an adaptive-time-step simulation loop.
- **Charging**: fast-charge sessions with the BMS current-limit curve, and
  battery preconditioning before a session.
- **Aging**: calendar and cycle aging, including cold-charging lithium
  plating, feeding a state-of-health trajectory.
- **Long horizon**: 20–30 year runs through a two-tier pipeline
  (characterize trips once, then replay them over the lifetime), Monte Carlo
  uncertainty bands over the aging coefficients, and fork-based what-if
  branching from any simulated moment.
- **Explanations**: causal traces of why each simulated event happened.
- **Mistake library**: real design decisions paired with a counterfactual
  under the same scenario, so the lesson is the difference between them
  (Leaf passive-air battery cooling, the i3 compressor failure, the
  e-tron's shared powertrain loop).
- **Data**: every uncertain real-world value carries its source and a
  confidence (`Sourced[T]`); vehicle profiles are in `configs/profiles/`
  and the research behind them in `docs/research/`.
- **Dashboard**: a Streamlit explorer for a single run of one selected
  vehicle.

Known limits worth knowing before citing a number (the full list is in
`docs/ROADMAP.md`, and `CLAUDE.md` summarizes the load-bearing ones):

- Motor mechanical-loss coefficients are placeholders, not a real
  efficiency map.
- Entropic heat is inert until a real dOCV/dT curve is sourced, so
  fast-charge heat is under-predicted.
- Calendar aging is linear in time, an extrapolation past its 5–10 year
  calibration window; multi-year state-of-health numbers carry the
  "Validated time horizon" caveat in `docs/model_reference.md`.
- Some demo scenario inputs (climate, usage and charging profiles) are
  still placeholders.
- The Transition seam is wired end to end but has no gameplay effect yet.
- The dashboard shows one vehicle at a time; fork branching works only at
  full fidelity, not from long-horizon checkpoints.

## Setup

Python 3.11, managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Run

```bash
uv run python examples/first_run.py --plot         # one short Leaf run
uv run python examples/flagship_comparison.py      # Leaf (hot) vs Model 3 (mild), long horizon
uv run python -m examples.monte_carlo_leaf_guam    # Monte Carlo band for the Leaf
uv run python -m examples.mistake_library_leaf_battery_cooling
uv run streamlit run src/ev_thermal/dashboard/app.py
```

Each script in `examples/` documents its own options at the top of the file.

## Tests

```bash
uv run pytest            # the default suite
uv run pytest -m slow    # full-fidelity multi-day reference runs, excluded by default
uv run mypy src tests    # strict type checking
uv run ruff check
```

## Layout

| Path | Holds |
| --- | --- |
| `src/ev_thermal/` | the physics, simulation, aging, long-horizon, Monte Carlo and fork modules |
| `src/ev_thermal/dashboard/` | presentation only: no physics lives here |
| `configs/profiles/` | the six passenger-EV reference profiles |
| `configs/profiles_commercial/` | the commercial-vehicle profile for the Transition seam |
| `examples/` | reproducible runners for every published comparison |
| `docs/` | milestones, roadmap, design questions, model reference and research notes |
| `CLAUDE.md` | architecture rules and coding standards |

## License

Not licensed yet: all rights reserved for now. It will take the same dual
`MIT OR Apache-2.0` license as the rest of the Nova ecosystem when it joins.
