"""Tests for `ev_thermal.profile_loader`'s YAML parsing and error-handling
surface. Generic loader-mechanism tests, not about the six reference
vehicles' actual data — see test_profiles.py for that (once the YAMLs are
populated).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ev_thermal.profile_loader import load_all_profiles, load_profile
from tests.test_schema import make_manufacturer_profile


def _write_valid_profile_yaml(path: Path) -> None:
    profile = make_manufacturer_profile()
    path.write_text(yaml.safe_dump(profile.model_dump(mode="json")))


def test_load_profile_round_trips_a_valid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "test_ev.yaml"
    _write_valid_profile_yaml(path)

    loaded = load_profile(path)

    assert loaded == make_manufacturer_profile()


def test_load_profile_accepts_str_path(tmp_path: Path) -> None:
    path = tmp_path / "test_ev.yaml"
    _write_valid_profile_yaml(path)

    loaded = load_profile(str(path))

    assert loaded == make_manufacturer_profile()


def test_load_profile_raises_file_not_found_for_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / "does_not_exist.yaml")


def test_load_profile_raises_value_error_for_malformed_yaml_syntax(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("battery:\n  chemistry: NMC\n\tbad_indent: true\n")  # tab after spaces: invalid YAML

    with pytest.raises(ValueError, match="invalid YAML"):
        load_profile(path)


def test_load_profile_raises_value_error_for_empty_stub(tmp_path: Path) -> None:
    path = tmp_path / "stub.yaml"
    path.write_text("# ManufacturerProfile data for the Test EV — not yet populated.\n")

    with pytest.raises(ValueError, match="expected a YAML mapping"):
        load_profile(path)


def test_load_profile_raises_value_error_for_non_mapping_yaml(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- 1\n- 2\n")

    with pytest.raises(ValueError, match="expected a YAML mapping"):
        load_profile(path)


def test_load_profile_raises_validation_error_for_missing_required_field(tmp_path: Path) -> None:
    profile = make_manufacturer_profile()
    data = profile.model_dump(mode="json")
    del data["battery"]

    path = tmp_path / "incomplete.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ValidationError):
        load_profile(path)


def test_load_all_profiles_keys_by_filename_stem(tmp_path: Path) -> None:
    _write_valid_profile_yaml(tmp_path / "vehicle_a.yaml")
    _write_valid_profile_yaml(tmp_path / "vehicle_b.yaml")

    loaded = load_all_profiles(tmp_path)

    assert set(loaded.keys()) == {"vehicle_a", "vehicle_b"}
    assert all(profile == make_manufacturer_profile() for profile in loaded.values())
