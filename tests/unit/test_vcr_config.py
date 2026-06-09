"""Unit tests for the VCR.py replay configuration (no Docker, no network).

Validates the safety rails that keep secrets out of cassettes and prevent live
API calls in CI.
"""
from __future__ import annotations

import pytest

from tests.support import vcr as vcr_support

pytestmark = pytest.mark.unit


def test_record_mode_is_none_in_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("VCR_RECORD_MODE", raising=False)
    assert vcr_support.record_mode() == "none"


def test_record_mode_defaults_to_none_locally(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("VCR_RECORD_MODE", raising=False)
    assert vcr_support.record_mode() == "none"


def test_ci_overrides_explicit_record_mode(monkeypatch):
    # Even if someone sets a recording mode, CI must force replay-only.
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv("VCR_RECORD_MODE", "all")
    assert vcr_support.record_mode() == "none"


def test_secrets_are_filtered():
    instance = vcr_support.build_vcr()
    for header in ("authorization", "x-api-key", "openai-organization"):
        assert header in vcr_support.FILTER_HEADERS
    # body is part of the match so different prompts get different cassettes
    assert "body" in instance.match_on
