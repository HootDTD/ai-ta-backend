"""Tests for NLI config module."""

from apollo.resolution.nli_config import NLI_ENABLED_FLAG, NLIParams, load_nli_params, nli_enabled


def test_flag_defaults_off_when_unset(monkeypatch):
    """Test that nli_enabled() returns False when env var is unset."""
    monkeypatch.delenv(NLI_ENABLED_FLAG, raising=False)
    assert nli_enabled() is False


def test_flag_truthy_values(monkeypatch):
    """Test that nli_enabled() returns True for truthy env values."""
    for v in ("1", "true", "YES"):
        monkeypatch.setenv(NLI_ENABLED_FLAG, v)
        assert nli_enabled() is True


def test_params_defaults_and_env_override(monkeypatch):
    """Test NLIParams defaults and env override behavior."""
    monkeypatch.delenv("APOLLO_NLI_MIN_ENTAILMENT", raising=False)
    assert load_nli_params().min_entailment == 0.87
    monkeypatch.setenv("APOLLO_NLI_MIN_ENTAILMENT", "0.93")
    assert load_nli_params().min_entailment == 0.93
    assert isinstance(load_nli_params(), NLIParams)
