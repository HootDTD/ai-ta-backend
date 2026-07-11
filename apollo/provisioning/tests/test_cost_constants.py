"""WU-3B2f — cost_constants unit tests (pure config, no network, no DB).

Pins the committed defaults (the runaway circuit-breaker ceiling + dead-letter
attempt cap), the gpt-4o / gpt-4o-mini price table, the Decimal cost arithmetic,
the unknown-model best-effort-zero branch, and the env-override reimport path.
"""

from __future__ import annotations

import importlib
from decimal import Decimal

from apollo.provisioning import cost_constants


def test_per_document_token_ceiling_pinned():
    assert cost_constants.PER_DOCUMENT_TOKEN_CEILING == 2_000_000


def test_max_attempts_pinned():
    assert cost_constants.MAX_ATTEMPTS == 3


def test_model_prices_present():
    assert cost_constants.MODEL_PRICES["gpt-4o"] == (
        Decimal("2.50"),
        Decimal("10.00"),
    )
    assert cost_constants.MODEL_PRICES["gpt-4o-mini"] == (
        Decimal("0.15"),
        Decimal("0.60"),
    )


def test_cost_usd_for_gpt4o():
    # (1M/1M * $2.50) + (1M/1M * $10.00) == $12.50
    assert cost_constants.cost_usd_for(
        "gpt-4o", tokens_in=1_000_000, tokens_out=1_000_000
    ) == Decimal("12.50")


def test_cost_usd_for_mini_fractional():
    # (500k/1M * $0.15) + (100k/1M * $0.60) == 0.075 + 0.06 == 0.135
    assert cost_constants.cost_usd_for(
        "gpt-4o-mini", tokens_in=500_000, tokens_out=100_000
    ) == Decimal("0.135")


def test_cost_usd_for_unknown_model_is_zero():
    # An unknown model must NEVER raise — counts still accrue elsewhere; cost is
    # best-effort and falls back to Decimal('0').
    assert cost_constants.cost_usd_for(
        "some-future-model", tokens_in=1_000, tokens_out=1_000
    ) == Decimal("0")


def test_cost_usd_for_zero_tokens():
    assert cost_constants.cost_usd_for("gpt-4o", tokens_in=0, tokens_out=0) == Decimal("0")


def test_cost_usd_for_returns_decimal_type():
    # Decimal is load-bearing (the column is NUMERIC(12,6)); float would drift.
    result = cost_constants.cost_usd_for("gpt-4o-mini", tokens_in=1, tokens_out=1)
    assert isinstance(result, Decimal)


def test_ceiling_env_override(monkeypatch):
    # The env-override branch: a reimport with the env var set reads the override.
    monkeypatch.setenv("APOLLO_PROVISION_TOKEN_CEILING", "10")
    monkeypatch.setenv("APOLLO_PROVISION_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("APOLLO_SCRAPE_SECTION_CHAR_CAP", "9000")
    reloaded = importlib.reload(cost_constants)
    try:
        assert reloaded.PER_DOCUMENT_TOKEN_CEILING == 10
        assert reloaded.MAX_ATTEMPTS == 7
        assert reloaded.APOLLO_SCRAPE_SECTION_CHAR_CAP == 9000
    finally:
        # Restore the committed defaults for every other test in the session.
        monkeypatch.delenv("APOLLO_PROVISION_TOKEN_CEILING", raising=False)
        monkeypatch.delenv("APOLLO_PROVISION_MAX_ATTEMPTS", raising=False)
        monkeypatch.delenv("APOLLO_SCRAPE_SECTION_CHAR_CAP", raising=False)
        importlib.reload(cost_constants)


def test_scrape_section_bounds_defaults():
    """Phase-2 structure-aware scrape bounds carry committed defaults."""
    import importlib

    import apollo.provisioning.cost_constants as cc

    importlib.reload(cc)
    assert cc.APOLLO_SCRAPE_MAX_SECTIONS == 120
    assert cc.APOLLO_SCRAPE_MIN_CANDIDATES == 3
    assert cc.APOLLO_SCRAPE_SECTION_CHAR_CAP == 2500


def test_structured_scrape_enabled_default_on_and_overridable(monkeypatch):
    """The structured-scrape flag defaults ON and reads per-call (env-overridable)."""
    from apollo.provisioning.cost_constants import structured_scrape_enabled

    monkeypatch.delenv("APOLLO_STRUCTURED_SCRAPE", raising=False)
    assert structured_scrape_enabled() is True
    monkeypatch.setenv("APOLLO_STRUCTURED_SCRAPE", "0")
    assert structured_scrape_enabled() is False
    monkeypatch.setenv("APOLLO_STRUCTURED_SCRAPE", "true")
    assert structured_scrape_enabled() is True
