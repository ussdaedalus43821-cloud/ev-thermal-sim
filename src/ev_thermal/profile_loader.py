"""Loads `ManufacturerProfile`s from YAML files. Pure I/O and validation, no
physics — same category as `schema.py` itself, not `sim.py`.

Every failure mode raises before a `ManufacturerProfile` is returned: there
is no silent-default or partially-valid-object path. See each branch below
for which exception a given failure produces.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ev_thermal.schema import ManufacturerProfile

PROFILES_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "profiles"


def load_profile(path: str | Path) -> ManufacturerProfile:
    """Load and validate one `ManufacturerProfile` from a YAML file.

    Raises `FileNotFoundError` if `path` doesn't exist, `ValueError` if the
    file isn't valid YAML or doesn't parse to a mapping (e.g. an empty or
    comment-only stub), and `pydantic.ValidationError` (propagated
    unmodified — it already reports a clear per-field path and reason) if
    the mapping doesn't satisfy `ManufacturerProfile`.
    """
    path = Path(path)
    text = path.read_text()

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: invalid YAML — {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping for ManufacturerProfile, got {type(data).__name__}")

    return ManufacturerProfile.model_validate(data)


def load_all_profiles(directory: Path = PROFILES_DIR) -> dict[str, ManufacturerProfile]:
    """Loads every `*.yaml` file in `directory` via `load_profile`, keyed by
    filename stem (e.g. "nissan_leaf"), not `ManufacturerProfile.name` — the
    stem is deterministic without needing to load every file first, and
    can't collide the way two profiles sharing a display name could.
    """
    return {path.stem: load_profile(path) for path in sorted(directory.glob("*.yaml"))}
