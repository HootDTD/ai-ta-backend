"""Unit coverage for the boot-time NLI pre-warm hook (campaign C2).

The real ``prewarm()`` (real-model path) is exercised by
``apollo/resolution/tests/test_nli_prewarm.py``; this module only covers the
env-gating + failure-isolation wiring in ``server.py``.
"""

import pytest

import server


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("YES", True),
    ],
)
def test_apollo_nli_prewarm_enabled_parses_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("APOLLO_NLI_PREWARM", raising=False)
    else:
        monkeypatch.setenv("APOLLO_NLI_PREWARM", raw)
    assert server._apollo_nli_prewarm_enabled() is expected


@pytest.mark.unit
def test_prewarm_hook_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("APOLLO_NLI_PREWARM", raising=False)
    calls = []
    monkeypatch.setattr("apollo.resolution.nli_adjudicator.prewarm", lambda: calls.append(1))
    server._prewarm_nli_on_startup()
    assert calls == []


@pytest.mark.unit
def test_prewarm_hook_calls_prewarm_when_enabled(monkeypatch):
    monkeypatch.setenv("APOLLO_NLI_PREWARM", "1")
    calls = []
    monkeypatch.setattr("apollo.resolution.nli_adjudicator.prewarm", lambda: calls.append(1))
    server._prewarm_nli_on_startup()
    assert calls == [1]


@pytest.mark.unit
def test_prewarm_hook_swallows_failure(monkeypatch):
    monkeypatch.setenv("APOLLO_NLI_PREWARM", "1")

    def _boom():
        raise RuntimeError("no network, cold cache")

    monkeypatch.setattr("apollo.resolution.nli_adjudicator.prewarm", _boom)
    # must not raise — a prewarm failure must never block server boot.
    server._prewarm_nli_on_startup()
