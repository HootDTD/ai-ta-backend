"""Coverage for the server.py retrieval-mode orchestrator hooks.

These exercise the sync bridge helpers (`_prepare_router_context_sync`,
`_retrieve_bundle_with_router`, `_persist_router_outcome_sync`) and the
`_ask_pgvector` parameter defaults with the wiring layer mocked out.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

import server
from ai.router import wiring

AUTH = SimpleNamespace(user_id="00000000-0000-0000-0000-000000000003")


def _ctx(mode: str, cached: object | None) -> SimpleNamespace:
    return SimpleNamespace(
        decision=SimpleNamespace(mode=mode, reason="test"),
        cached=cached,
    )


@pytest.mark.unit
def test_prepare_router_context_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("ROUTER_ENABLED", raising=False)
    result = server._prepare_router_context_sync(
        auth=AUTH, chat_id="c1", search_space_id=1, question="q", has_attachments=False
    )
    assert result is None


@pytest.mark.unit
def test_prepare_router_context_returns_wiring_result(monkeypatch):
    monkeypatch.setenv("ROUTER_ENABLED", "true")
    sentinel = _ctx("NONE", cached=object())

    async def _fake_prepare(**kwargs):
        assert kwargs["chat_id"] == "c1"
        assert kwargs["question"] == "q"
        return sentinel

    monkeypatch.setattr(wiring, "prepare_router_context", _fake_prepare)
    result = server._prepare_router_context_sync(
        auth=AUTH, chat_id="c1", search_space_id=1, question="q", has_attachments=False
    )
    assert result is sentinel


@pytest.mark.unit
def test_prepare_router_context_failure_returns_none(monkeypatch):
    monkeypatch.setenv("ROUTER_ENABLED", "true")

    async def _boom(**kwargs):
        raise RuntimeError("router down")

    monkeypatch.setattr(wiring, "prepare_router_context", _boom)
    result = server._prepare_router_context_sync(
        auth=AUTH, chat_id="c1", search_space_id=1, question="q", has_attachments=False
    )
    assert result is None


@pytest.mark.unit
def test_retrieve_uses_legacy_path_without_router_ctx(monkeypatch):
    sentinel = object()
    seen: dict = {}

    def _fake_ask(**kwargs):
        seen.update(kwargs)
        return sentinel

    monkeypatch.setattr(server, "_ask_pgvector", _fake_ask)
    bundle, ms = server._retrieve_bundle_with_router(
        router_ctx=None, q_effective="q", workspace=None, weight_overrides={}, cfg=None
    )
    assert bundle is sentinel
    assert "top_k" not in seen  # legacy call uses env defaults
    assert ms >= 0


@pytest.mark.unit
def test_retrieve_none_mode_skips_retrieval(monkeypatch):
    sentinel = object()

    def _no_retrieval(**kwargs):
        raise AssertionError("NONE mode must not retrieve")

    monkeypatch.setattr(server, "_ask_pgvector", _no_retrieval)
    monkeypatch.setattr(wiring, "bundle_from_cache", lambda cached, **kw: sentinel)
    bundle, _ = server._retrieve_bundle_with_router(
        router_ctx=_ctx("NONE", cached=object()),
        q_effective="q",
        workspace=None,
        weight_overrides={},
        cfg=None,
    )
    assert bundle is sentinel


@pytest.mark.unit
def test_retrieve_augment_mode_merges_topup(monkeypatch):
    fresh, merged = object(), object()
    seen: dict = {}

    def _fake_ask(**kwargs):
        seen.update(kwargs)
        return fresh

    def _fake_merge(cached, fresh_bundle, **kw):
        assert fresh_bundle is fresh
        return merged

    monkeypatch.setattr(server, "_ask_pgvector", _fake_ask)
    monkeypatch.setattr(wiring, "merge_augment_bundle", _fake_merge)
    bundle, _ = server._retrieve_bundle_with_router(
        router_ctx=_ctx("AUGMENT", cached=object()),
        q_effective="q",
        workspace=None,
        weight_overrides={},
        cfg=None,
    )
    assert bundle is merged
    assert seen["top_k"] == wiring.augment_top_k()
    assert seen["token_budget"] == wiring.augment_token_budget()


@pytest.mark.unit
def test_retrieve_fresh_mode_uses_legacy_path(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(server, "_ask_pgvector", lambda **kw: sentinel)
    bundle, _ = server._retrieve_bundle_with_router(
        router_ctx=_ctx("FRESH", cached=None),
        q_effective="q",
        workspace=None,
        weight_overrides={},
        cfg=None,
    )
    assert bundle is sentinel


@pytest.mark.unit
def test_persist_outcome_forwards_to_wiring(monkeypatch):
    seen: dict = {}

    async def _fake_persist(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(wiring, "persist_turn_outcome", _fake_persist)
    server._persist_router_outcome_sync(
        auth=AUTH,
        chat_id="c1",
        router_ctx=_ctx("FRESH", None),
        bundle=None,
        question="q",
        retrieval_ms=12,
        answer_ms=34,
    )
    assert seen["chat_id"] == "c1"
    assert seen["latency_retrieval_ms"] == 12
    assert seen["latency_answer_ms"] == 34


@pytest.mark.unit
def test_persist_outcome_swallows_errors(monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(wiring, "persist_turn_outcome", _boom)
    # Must not raise.
    server._persist_router_outcome_sync(
        auth=AUTH,
        chat_id="c1",
        router_ctx=_ctx("FRESH", None),
        bundle=None,
        question="q",
        retrieval_ms=None,
        answer_ms=None,
    )


@pytest.mark.unit
def test_ask_pgvector_defaults_token_budget_and_top_k(monkeypatch):
    """Covers the env-default branch of the new top_k/token_budget params."""
    import database.session as db_session_mod
    from ai import main_ai
    from retrieval import pipeline as retrieval_pipeline

    seen: dict = {}

    async def _fake_retrieve(**kwargs):
        seen.update(kwargs)
        return [], {"combined_query": kwargs["query"]}

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield None

    monkeypatch.setattr(main_ai, "extract_and_filter_keywords", lambda q: ("", []))
    monkeypatch.setattr(retrieval_pipeline, "retrieve_for_question", _fake_retrieve)
    monkeypatch.setattr(db_session_mod, "get_async_session", _fake_session)
    monkeypatch.delenv("TOKEN_BUDGET", raising=False)
    monkeypatch.delenv("K_SEM", raising=False)

    workspace = SimpleNamespace(metadata={"search_space_id": 7})
    bundle = server._ask_pgvector(
        q_effective="what is a p-series?",
        workspace=workspace,
        weight_overrides={},
        cfg=None,
    )
    assert seen["token_budget"] == 6000
    assert seen["top_k"] == 20
    assert bundle.snippets == []
