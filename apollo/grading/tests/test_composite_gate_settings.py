"""Tests for the §10 composite-gate settings (config/settings.py).

Mirrors ``apollo/resolution/tests/test_nli_config.py``'s pattern for
``apollo_nli_misc_positive_certify``: default-OFF/default-value pins, truthy/
non-truthy acceptance sets, and the malformed-value fallback.
"""

from __future__ import annotations

import pytest

from config.settings import (
    apollo_abstention_composite_enabled,
    apollo_composite_coverage_min,
)

_ENVS = ("APOLLO_ABSTENTION_COMPOSITE", "APOLLO_COMPOSITE_COVERAGE_MIN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


def test_composite_enabled_defaults_off_when_unset():
    assert apollo_abstention_composite_enabled() is False


def test_composite_enabled_truthy_values(monkeypatch):
    for v in ("1", "true", "YES", " True "):
        monkeypatch.setenv("APOLLO_ABSTENTION_COMPOSITE", v)
        assert apollo_abstention_composite_enabled() is True


def test_composite_enabled_non_truthy_values_stay_off(monkeypatch):
    for v in ("0", "false", "no", "off", "", "banana"):
        monkeypatch.setenv("APOLLO_ABSTENTION_COMPOSITE", v)
        assert apollo_abstention_composite_enabled() is False


def test_coverage_min_defaults_to_calibrated_floor_when_unset():
    """0.1 = the 2026-07-07 F1c-corpus calibration: every correct-persona
    attempt's resolver coverage sat at >=0.20, and one-resolved-node on the
    longest declared path (7 nodes) is ~0.14 — so 0.1 means "abstain only when
    the resolver credited essentially nothing"."""
    assert apollo_composite_coverage_min() == 0.1


def test_coverage_min_matches_abstention_constant():
    from apollo.grading.abstention import COMPOSITE_DEFAULT_COVERAGE_MIN

    assert apollo_composite_coverage_min() == COMPOSITE_DEFAULT_COVERAGE_MIN


def test_coverage_min_reads_override(monkeypatch):
    monkeypatch.setenv("APOLLO_COMPOSITE_COVERAGE_MIN", "0.8")
    assert apollo_composite_coverage_min() == 0.8


def test_coverage_min_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("APOLLO_COMPOSITE_COVERAGE_MIN", "not-a-float")
    assert apollo_composite_coverage_min() == 0.1
