"""Tests for NLI config module (per-model tuned defaults + env overrides)."""

import pytest

from apollo.resolution.nli_config import (
    NLI_ENABLED_FLAG,
    NLI_MODEL_ENV,
    NLI_MODEL_LARGE,
    NLI_MODEL_SMALL,
    NLIParams,
    active_nli_model,
    default_params_for_model,
    load_nli_params,
    nli_enabled,
)
from config.settings import apollo_nli_misc_positive_certify

_PARAM_ENVS = (
    "APOLLO_NLI_MODEL",
    "APOLLO_NLI_TOP_K",
    "APOLLO_NLI_MIN_ENTAILMENT",
    "APOLLO_NLI_MAX_CONTRADICTION",
    "APOLLO_NLI_AMBIGUITY_MARGIN",
    "APOLLO_NLI_MISC_VETO_ENT",
    "APOLLO_NLI_MISC_POSITIVE_CERTIFY",
)


@pytest.fixture(autouse=True)
def _clean_nli_env(monkeypatch):
    """Every test starts from a clean env so defaults are deterministic."""
    for name in _PARAM_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


def test_flag_defaults_on_when_unset(monkeypatch):
    """NLI is DEFAULT-ON (enabled 2026-07-01 after per-model tuning): an unset
    flag means enabled."""
    monkeypatch.delenv(NLI_ENABLED_FLAG, raising=False)
    assert nli_enabled() is True


def test_flag_truthy_values(monkeypatch):
    for v in ("1", "true", "YES"):
        monkeypatch.setenv(NLI_ENABLED_FLAG, v)
        assert nli_enabled() is True


def test_flag_explicit_off_values_disable(monkeypatch):
    """A deployment (or the test guards) can still force NLI OFF explicitly — the
    kill switch. Anything not truthy disables it."""
    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(NLI_ENABLED_FLAG, v)
        assert nli_enabled() is False


# --- active model selection -------------------------------------------------


def test_active_model_defaults_to_large():
    """Production targets the -large checkpoint (2026-07-01 tuning)."""
    assert active_nli_model() == NLI_MODEL_LARGE


def test_active_model_env_override(monkeypatch):
    monkeypatch.setenv(NLI_MODEL_ENV, NLI_MODEL_SMALL)
    assert active_nli_model() == NLI_MODEL_SMALL


# --- per-model tuned defaults -----------------------------------------------


def test_default_params_large():
    p = default_params_for_model(NLI_MODEL_LARGE)
    assert (p.min_entailment, p.max_contradiction, p.misconception_veto_entailment) == (
        0.90,
        0.05,
        0.98,
    )


def test_default_params_small():
    p = default_params_for_model(NLI_MODEL_SMALL)
    assert (p.min_entailment, p.max_contradiction, p.misconception_veto_entailment) == (
        0.70,
        0.10,
        0.96,
    )


def test_default_params_unknown_model_uses_safe_fallback():
    p = default_params_for_model("some/unknown-checkpoint")
    # conservative recall bar + a SAFE veto floor (never the unsafe 0.80)
    assert p.min_entailment >= 0.87
    assert p.misconception_veto_entailment >= 0.96


def test_all_tuned_veto_floors_are_safe():
    """Safety invariant: no shipped default veto sits at the unsafe 0.80 — a
    correct student statement can false-entail a misconception surface at
    0.88-0.98 (2026-07-01 tuning §3), so every default must clear that band."""
    for model in (NLI_MODEL_LARGE, NLI_MODEL_SMALL, "unknown/x"):
        assert default_params_for_model(model).misconception_veto_entailment >= 0.96


# --- load_nli_params: per-model base + env overrides ------------------------


def test_load_params_uses_active_model_defaults(monkeypatch):
    monkeypatch.setenv(NLI_MODEL_ENV, NLI_MODEL_SMALL)
    assert load_nli_params() == default_params_for_model(NLI_MODEL_SMALL)
    monkeypatch.setenv(NLI_MODEL_ENV, NLI_MODEL_LARGE)
    assert load_nli_params() == default_params_for_model(NLI_MODEL_LARGE)


def test_load_params_defaults_to_large_when_model_unset():
    # env cleaned by fixture -> active model is large
    assert load_nli_params() == default_params_for_model(NLI_MODEL_LARGE)
    assert isinstance(load_nli_params(), NLIParams)


def test_load_params_env_overrides_win_over_model_defaults(monkeypatch):
    monkeypatch.setenv(NLI_MODEL_ENV, NLI_MODEL_LARGE)  # base veto 0.98, min_ent 0.90
    monkeypatch.setenv("APOLLO_NLI_MIN_ENTAILMENT", "0.55")
    monkeypatch.setenv("APOLLO_NLI_MAX_CONTRADICTION", "0.33")
    monkeypatch.setenv("APOLLO_NLI_MISC_VETO_ENT", "0.71")
    monkeypatch.setenv("APOLLO_NLI_TOP_K", "9")
    monkeypatch.setenv("APOLLO_NLI_AMBIGUITY_MARGIN", "0.22")
    p = load_nli_params()
    assert p.min_entailment == 0.55
    assert p.max_contradiction == 0.33
    assert p.misconception_veto_entailment == 0.71
    assert p.top_k == 9
    assert p.ambiguity_margin == 0.22


def test_load_params_partial_env_override_keeps_model_default(monkeypatch):
    """Overriding one knob leaves the others at the active model's tuned default."""
    monkeypatch.setenv(NLI_MODEL_ENV, NLI_MODEL_SMALL)  # base 0.70 / 0.10 / 0.96
    monkeypatch.setenv("APOLLO_NLI_MIN_ENTAILMENT", "0.60")
    p = load_nli_params()
    assert p.min_entailment == 0.60  # overridden
    assert p.max_contradiction == 0.10  # small default preserved
    assert p.misconception_veto_entailment == 0.96  # small default preserved


# --- APOLLO_NLI_MISC_POSITIVE_CERTIFY (default OFF) ---------------------------


def test_misc_positive_certify_defaults_off_when_unset():
    """DEFAULT-OFF: an unset flag means veto-only behavior (byte-identical)."""
    assert apollo_nli_misc_positive_certify() is False
    assert NLIParams().misc_positive_certify is False
    assert load_nli_params().misc_positive_certify is False


def test_misc_positive_certify_truthy_values(monkeypatch):
    for v in ("1", "true", "YES", " True "):
        monkeypatch.setenv("APOLLO_NLI_MISC_POSITIVE_CERTIFY", v)
        assert apollo_nli_misc_positive_certify() is True
        assert load_nli_params().misc_positive_certify is True


def test_misc_positive_certify_non_truthy_values_stay_off(monkeypatch):
    """Anything not explicitly truthy keeps the flag OFF (fail-closed)."""
    for v in ("0", "false", "no", "off", "", "banana"):
        monkeypatch.setenv("APOLLO_NLI_MISC_POSITIVE_CERTIFY", v)
        assert apollo_nli_misc_positive_certify() is False
        assert load_nli_params().misc_positive_certify is False


def test_misc_positive_certify_flag_never_touches_thresholds(monkeypatch):
    """The flag reuses the existing veto threshold — enabling it must not move
    any tuned per-model threshold."""
    monkeypatch.setenv("APOLLO_NLI_MISC_POSITIVE_CERTIFY", "1")
    on = load_nli_params()
    monkeypatch.delenv("APOLLO_NLI_MISC_POSITIVE_CERTIFY")
    off = load_nli_params()
    for name in (
        "top_k",
        "min_entailment",
        "max_contradiction",
        "ambiguity_margin",
        "misconception_veto_entailment",
    ):
        assert getattr(on, name) == getattr(off, name)
