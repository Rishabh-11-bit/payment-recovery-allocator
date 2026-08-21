"""Config is load-bearing: every limit lives in YAML and nothing has a default.

These tests exist to keep it that way. A threshold that can go missing without
anyone noticing is not config -- it is a hardcoded value with extra steps.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from pydantic import ValidationError

from recovery.config import load_config

CONFIG = pathlib.Path("config/default.yaml")


def write(tmp_path: pathlib.Path, data: dict) -> pathlib.Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_shipped_config_loads(config):
    assert config.policy.version
    assert config.regulatory.attempt_cap == 4
    assert len(config.regulatory.peak_windows_ist) == 2


def test_missing_key_is_an_error_not_a_default(tmp_path):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    del data["regulatory"]["attempt_cap"]

    with pytest.raises(ValidationError):
        load_config(write(tmp_path, data))


def test_unknown_key_is_rejected(tmp_path):
    """A typo'd key must fail loudly rather than silently doing nothing."""
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    data["regulatory"]["atempt_cap"] = 4

    with pytest.raises(ValidationError):
        load_config(write(tmp_path, data))


def test_peak_window_must_advance(tmp_path):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    data["regulatory"]["peak_windows_ist"] = [["13:00", "10:00"]]

    with pytest.raises(ValidationError, match="does not advance"):
        load_config(write(tmp_path, data))


def test_config_is_frozen(config):
    with pytest.raises(ValidationError):
        config.regulatory.attempt_cap = 99


def test_missing_file_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")
