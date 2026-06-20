"""WU-3B2h — quarantine_constants unit tests (pure config, no network, no DB).

Pins the three committed defaults (N_MIN / THETA_MISS / CONCENTRATION_MARGIN)
and the env-override reimport path. Mirrors ``test_cost_constants.py`` —
calibration recalibrates via env WITHOUT a code change, but the committed
defaults must stay 8 / 0.80 / 0.40 so these tests pin them.
"""

from __future__ import annotations

import importlib

from apollo.provisioning import quarantine_constants


def test_n_min_pinned():
    assert quarantine_constants.N_MIN == 8


def test_theta_miss_pinned():
    assert quarantine_constants.THETA_MISS == 0.80


def test_concentration_margin_pinned():
    assert quarantine_constants.CONCENTRATION_MARGIN == 0.40


def test_env_override_reimport(monkeypatch):
    # The env-override branch: a reimport with the env vars set reads the
    # overrides (calibration can retune without a code change).
    monkeypatch.setenv("APOLLO_QUARANTINE_N_MIN", "20")
    monkeypatch.setenv("APOLLO_QUARANTINE_THETA_MISS", "0.95")
    monkeypatch.setenv("APOLLO_QUARANTINE_CONCENTRATION_MARGIN", "0.50")
    reloaded = importlib.reload(quarantine_constants)
    try:
        assert reloaded.N_MIN == 20
        assert reloaded.THETA_MISS == 0.95
        assert reloaded.CONCENTRATION_MARGIN == 0.50
    finally:
        # Restore the committed defaults for every other test in the session.
        monkeypatch.delenv("APOLLO_QUARANTINE_N_MIN", raising=False)
        monkeypatch.delenv("APOLLO_QUARANTINE_THETA_MISS", raising=False)
        monkeypatch.delenv("APOLLO_QUARANTINE_CONCENTRATION_MARGIN", raising=False)
        importlib.reload(quarantine_constants)
