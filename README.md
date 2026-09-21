# EV Thermal Simulator

An educational, comparative electric-drivetrain thermal simulator. It models
six real manufacturer drivetrains (Nissan Leaf, Tesla Model 3, Chevrolet
Bolt, Audi e-tron, BMW i3, Kia Niro EV) plus user-editable counterfactuals,
over 20–30 year vehicle lifetimes, to teach why thermal management
decisions compound over a vehicle's life.

Status: schema scaffold only — no physics or simulation loop yet. See
`CLAUDE.md` for architecture rules and `src/ev_thermal/schema.py` for the
current data model.

## Setup

```bash
uv sync
```

## Tests

```bash
uv run pytest
```
