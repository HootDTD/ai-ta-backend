"""RED tests for the misconception-detector flag + calibration constants (T1).

Mirrors ``apollo/emergent/config.py``'s call-time-read flag pattern and
``apollo/grading/composite.py``'s env-overridable float constants. See
``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md`` §3.
"""

from __future__ import annotations

import importlib

import pytest

from apollo.overseer.misconception_detector import config as detector_config


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Every test starts with a clean slate for all detector env vars."""
    env_vars = [
        detector_config.FLAG_ENV,
        "APOLLO_MISC_TAU_FIRE",
        "APOLLO_MISC_TAU_FIRE_VERBALIZED",
        "APOLLO_MISC_SEVERITY_CLAMP",
        "APOLLO_MISC_CENTRALITY_MIN",
        "APOLLO_MISC_CEILING",
        "APOLLO_MISC_BANK_SIM",
    ]
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)
    yield


class TestDetectorEnabled:
    def test_default_is_false_when_unset(self):
        assert detector_config.detector_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "YES", "TRUE", "On"])
    def test_true_for_each_truthy_value(self, monkeypatch, value):
        monkeypatch.setenv(detector_config.FLAG_ENV, value)
        assert detector_config.detector_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "garbage"])
    def test_false_for_non_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv(detector_config.FLAG_ENV, value)
        assert detector_config.detector_enabled() is False

    def test_is_read_at_call_time_not_cached(self, monkeypatch):
        assert detector_config.detector_enabled() is False
        monkeypatch.setenv(detector_config.FLAG_ENV, "true")
        assert detector_config.detector_enabled() is True
        monkeypatch.delenv(detector_config.FLAG_ENV)
        assert detector_config.detector_enabled() is False


class TestConstantsDefaults:
    def test_defaults_match_frozen_contract(self):
        # Reload so constants reflect the clean-env fixture (they are bound
        # at import time, mirroring apollo/emergent/config.py's OQ2 constants).
        importlib.reload(detector_config)
        assert detector_config.TAU_FIRE == pytest.approx(0.85)
        assert detector_config.TAU_FIRE_VERBALIZED == pytest.approx(0.90)
        assert detector_config.SEVERITY_CLAMP == pytest.approx(0.30)
        assert detector_config.CENTRALITY_W_MIN == pytest.approx(0.30)
        assert detector_config.CEILING_COMPOSITE == pytest.approx(0.84)
        assert detector_config.BANK_SIM_FLOOR == pytest.approx(0.80)


class TestConstantsEnvOverride:
    @pytest.mark.parametrize(
        "env_var,attr,override,expected",
        [
            ("APOLLO_MISC_TAU_FIRE", "TAU_FIRE", "0.5", 0.5),
            ("APOLLO_MISC_TAU_FIRE_VERBALIZED", "TAU_FIRE_VERBALIZED", "0.95", 0.95),
            ("APOLLO_MISC_SEVERITY_CLAMP", "SEVERITY_CLAMP", "0.75", 0.75),
            ("APOLLO_MISC_CENTRALITY_MIN", "CENTRALITY_W_MIN", "0.1", 0.1),
            ("APOLLO_MISC_CEILING", "CEILING_COMPOSITE", "0.5", 0.5),
            ("APOLLO_MISC_BANK_SIM", "BANK_SIM_FLOOR", "0.9", 0.9),
        ],
    )
    def test_env_override_parses(self, monkeypatch, env_var, attr, override, expected):
        monkeypatch.setenv(env_var, override)
        importlib.reload(detector_config)
        try:
            assert getattr(detector_config, attr) == pytest.approx(expected)
        finally:
            monkeypatch.delenv(env_var, raising=False)
            importlib.reload(detector_config)

    @pytest.mark.parametrize(
        "env_var,attr,default",
        [
            ("APOLLO_MISC_TAU_FIRE", "TAU_FIRE", 0.85),
            ("APOLLO_MISC_TAU_FIRE_VERBALIZED", "TAU_FIRE_VERBALIZED", 0.90),
            ("APOLLO_MISC_SEVERITY_CLAMP", "SEVERITY_CLAMP", 0.30),
            ("APOLLO_MISC_CENTRALITY_MIN", "CENTRALITY_W_MIN", 0.30),
            ("APOLLO_MISC_CEILING", "CEILING_COMPOSITE", 0.84),
            ("APOLLO_MISC_BANK_SIM", "BANK_SIM_FLOOR", 0.80),
        ],
    )
    def test_malformed_value_falls_back_to_default(self, monkeypatch, env_var, attr, default):
        monkeypatch.setenv(env_var, "not-a-float")
        importlib.reload(detector_config)
        try:
            assert getattr(detector_config, attr) == pytest.approx(default)
        finally:
            monkeypatch.delenv(env_var, raising=False)
            importlib.reload(detector_config)


def test_reload_restores_defaults_after_module_teardown():
    """Sanity check that the last test in this file leaves the module clean
    for any other test module that imports it at collection time."""
    importlib.reload(detector_config)
    assert detector_config.TAU_FIRE == pytest.approx(0.85)
