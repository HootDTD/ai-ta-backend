"""Flag-gate tests: APOLLO_EMERGENT_MISCONCEPTIONS defaults OFF."""

from __future__ import annotations

import pytest

from apollo.emergent.config import emergent_misconceptions_enabled


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("APOLLO_EMERGENT_MISCONCEPTIONS", raising=False)
    assert emergent_misconceptions_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_flag_truthy_values(monkeypatch, value):
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", value)
    assert emergent_misconceptions_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nope"])
def test_flag_falsy_values(monkeypatch, value):
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", value)
    assert emergent_misconceptions_enabled() is False
