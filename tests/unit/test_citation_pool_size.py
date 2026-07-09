"""_citation_pool_size: one scoring wave by default, env-cappable."""

from __future__ import annotations

import pytest

from ai.main_ai import _citation_pool_size

pytestmark = pytest.mark.unit


def test_default_cap_covers_default_snippet_count(monkeypatch):
    monkeypatch.delenv("CITATION_WORKERS", raising=False)
    # K_SEM default is 20 snippets — all must score in a single wave.
    assert _citation_pool_size(20) == 20


def test_pool_never_exceeds_snippet_count(monkeypatch):
    monkeypatch.delenv("CITATION_WORKERS", raising=False)
    assert _citation_pool_size(5) == 5


def test_env_override_caps_pool(monkeypatch):
    monkeypatch.setenv("CITATION_WORKERS", "8")
    assert _citation_pool_size(20) == 8


def test_zero_snippets_still_returns_valid_pool_size(monkeypatch):
    monkeypatch.delenv("CITATION_WORKERS", raising=False)
    assert _citation_pool_size(0) == 1
