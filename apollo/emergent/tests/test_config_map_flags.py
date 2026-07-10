"""Flag-gate tests: APOLLO_EMERGENT_MAP_CAPTURE / APOLLO_EMERGENT_MAP_ASSERT
default OFF (2026-07-10 emergent misconception map plan, Wave 2 flags).

Both are new, independent, call-time-read flags — mirrors
``apollo/overseer/misconception_detector/config.py``'s ``detector_enabled`` /
``struct_cokey_enabled`` sub-flag convention. The legacy
``APOLLO_EMERGENT_MISCONCEPTIONS`` flag (covered by ``test_config_flag.py``)
is untouched by either.
"""

from __future__ import annotations

import pytest

from apollo.emergent.config import (
    emergent_map_assert_enabled,
    emergent_map_capture_enabled,
)


def test_capture_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("APOLLO_EMERGENT_MAP_CAPTURE", raising=False)
    assert emergent_map_capture_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_capture_flag_truthy_values(monkeypatch, value):
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", value)
    assert emergent_map_capture_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_capture_flag_falsy_values(monkeypatch, value):
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", value)
    assert emergent_map_capture_enabled() is False


def test_assert_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("APOLLO_EMERGENT_MAP_ASSERT", raising=False)
    assert emergent_map_assert_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_assert_flag_truthy_values(monkeypatch, value):
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_ASSERT", value)
    assert emergent_map_assert_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_assert_flag_falsy_values(monkeypatch, value):
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_ASSERT", value)
    assert emergent_map_assert_enabled() is False


def test_capture_and_assert_flags_are_independent(monkeypatch):
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")
    monkeypatch.delenv("APOLLO_EMERGENT_MAP_ASSERT", raising=False)
    assert emergent_map_capture_enabled() is True
    assert emergent_map_assert_enabled() is False


def test_legacy_flag_untouched_by_new_flags(monkeypatch):
    """The pre-existing APOLLO_EMERGENT_MISCONCEPTIONS flag is a separate env
    key from both new flags — setting the new ones must never flip it."""
    from apollo.emergent.config import emergent_misconceptions_enabled

    monkeypatch.delenv("APOLLO_EMERGENT_MISCONCEPTIONS", raising=False)
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_CAPTURE", "1")
    monkeypatch.setenv("APOLLO_EMERGENT_MAP_ASSERT", "1")
    assert emergent_misconceptions_enabled() is False
