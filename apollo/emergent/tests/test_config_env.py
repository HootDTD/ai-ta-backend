"""Env-override parsing for the trust-gradient constants (config helpers)."""

from __future__ import annotations

import pytest

from apollo.emergent.config import _float_env, _int_env


@pytest.mark.parametrize(
    "raw,default,expected",
    [(None, 0.5, 0.5), ("", 0.5, 0.5), ("  ", 0.5, 0.5), ("0.7", 0.5, 0.7), ("bad", 0.5, 0.5)],
)
def test_float_env(monkeypatch, raw, default, expected):
    if raw is None:
        monkeypatch.delenv("X_EMG_F", raising=False)
    else:
        monkeypatch.setenv("X_EMG_F", raw)
    assert _float_env("X_EMG_F", default) == expected


@pytest.mark.parametrize(
    "raw,default,expected",
    [(None, 3, 3), ("", 3, 3), ("5", 3, 5), ("bad", 3, 3)],
)
def test_int_env(monkeypatch, raw, default, expected):
    if raw is None:
        monkeypatch.delenv("X_EMG_I", raising=False)
    else:
        monkeypatch.setenv("X_EMG_I", raw)
    assert _int_env("X_EMG_I", default) == expected
