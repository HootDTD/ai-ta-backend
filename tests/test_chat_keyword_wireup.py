"""WU-5B5 — LIVE /ask chat-keyword wire-up (unit, helper-level, append_turn spied).

These tests drive the two assistant-turn persist helpers in ``server.py`` and the
new ``_keywords_from_bundle`` extractor directly — the LLM / network / DB are never
hit. ``append_turn`` is monkeypatched with an ``AsyncMock`` that records the
``keywords=`` kwarg, and the async core's infra deps (session CM, chat-session
lookup, memory refresh) are stubbed so it runs in a fresh event loop.

Spec invariants locked here (§10 RQ5 hedge):
- the keyword list is pulled robustly off the bundle (top-level OR metadata OR []);
- a None / AUGMENT / cache bundle that carries no ``found_terms`` yields ``[]``;
- the list is NOT re-capped at 8 (the bound is upstream in extract_and_filter_keywords);
- keywords attach to the **assistant** turn only;
- omitting ``keywords`` reproduces the pre-WU-5B5 ``[]`` write semantics.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import server


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _fake_bundle(*, found_terms=None, metadata_found_terms=None, has_found_terms=True):
    """Build a bundle-shaped namespace.

    - ``has_found_terms=False`` -> the bundle has NO top-level ``found_terms`` attr
      at all (exercises the metadata fallback / missing-attr tolerance).
    - ``metadata_found_terms=None`` -> the bundle has NO ``metadata`` attr at all.
    """
    attrs = {}
    if has_found_terms:
        attrs["found_terms"] = list(found_terms) if found_terms is not None else []
    if metadata_found_terms is not None:
        attrs["metadata"] = SimpleNamespace(found_terms=list(metadata_found_terms))
    return SimpleNamespace(**attrs)


class _FakeSessionCM:
    """Async context manager that yields a MagicMock db session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _spy_append_turn(monkeypatch):
    """Spy ``server.append_turn`` and stub the async core's infra deps.

    Returns the AsyncMock standing in for ``append_turn`` so tests can assert on
    its recorded ``keywords=`` kwarg.
    """
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(server, "append_turn", spy)

    db_session = MagicMock()
    db_session.commit = AsyncMock(return_value=None)
    monkeypatch.setattr(server, "get_async_session", lambda: _FakeSessionCM(db_session))

    fake_chat = SimpleNamespace(id=7, search_space_id=1, updated_at=None)
    monkeypatch.setattr(
        server,
        "get_chat_session_for_user",
        AsyncMock(return_value=fake_chat),
    )
    monkeypatch.setattr(server, "refresh_memory_summary", AsyncMock(return_value=None))

    def _fake_run_async(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(server, "run_async", _fake_run_async)
    return spy


_AUTH = SimpleNamespace(user_id="student-1", access_token="tok")


# --------------------------------------------------------------------------- #
# _keywords_from_bundle — Edit 3 (all branches)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_keywords_from_bundle_reads_top_level_found_terms():
    bundle = _fake_bundle(found_terms=["momentum", "impulse", "force"])
    assert server._keywords_from_bundle(bundle) == ["momentum", "impulse", "force"]


@pytest.mark.unit
def test_keywords_from_bundle_falls_back_to_metadata_found_terms():
    # No top-level found_terms; metadata carries the list.
    bundle = _fake_bundle(has_found_terms=False, metadata_found_terms=["energy", "work"])
    assert server._keywords_from_bundle(bundle) == ["energy", "work"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "bundle",
    [
        None,  # no bundle at all (retrieval failed)
        _fake_bundle(found_terms=[], metadata_found_terms=[]),  # NONE/AUGMENT/cache shape
        _fake_bundle(has_found_terms=False),  # neither attribute present
    ],
)
def test_keywords_from_bundle_none_or_empty_yields_empty_list(bundle):
    assert server._keywords_from_bundle(bundle) == []


@pytest.mark.unit
def test_keywords_from_bundle_does_not_recap_at_8():
    terms = [f"t{i}" for i in range(12)]
    bundle = _fake_bundle(found_terms=terms)
    out = server._keywords_from_bundle(bundle)
    assert out == terms
    assert len(out) == 12  # the upstream <=8 bound is NOT re-applied here


@pytest.mark.unit
def test_keywords_from_bundle_returns_str_list_copy():
    # str() guard keeps the JSONB list homogeneous; result must be a fresh list.
    src = ["alpha", 2]
    bundle = _fake_bundle(found_terms=src)
    out = server._keywords_from_bundle(bundle)
    assert out == ["alpha", "2"]
    out.append("mutated")
    assert src == ["alpha", 2]  # source list not aliased


# --------------------------------------------------------------------------- #
# Helper forward — Edits 1 & 2
# --------------------------------------------------------------------------- #
@pytest.mark.unit
async def test_happy_path_async_threads_found_terms_to_append_turn(monkeypatch):
    spy = _spy_append_turn(monkeypatch)
    await server._append_assistant_turn_and_refresh_async(
        auth=_AUTH,
        chat_id="chat-1",
        search_space_id=1,
        assistant_content="the answer",
        citations=None,
        keywords=["a", "b", "c"],
    )
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["role"] == "assistant"
    assert kwargs["keywords"] == ["a", "b", "c"]


@pytest.mark.unit
def test_happy_path_sync_wrapper_forwards_keywords(monkeypatch):
    spy = _spy_append_turn(monkeypatch)
    server._append_assistant_turn_and_refresh(
        auth=_AUTH,
        chat_id="chat-1",
        search_space_id=1,
        assistant_content="the answer",
        citations=None,
        keywords=["x"],
    )
    spy.assert_awaited_once()
    assert spy.await_args.kwargs["keywords"] == ["x"]


@pytest.mark.unit
async def test_omitted_keywords_writes_empty_list_semantics(monkeypatch):
    # Omit keywords -> default None -> `list(keywords) if keywords else None` yields
    # None at the helper boundary; the frozen append_turn coalesces None -> [].
    spy = _spy_append_turn(monkeypatch)
    await server._append_assistant_turn_and_refresh_async(
        auth=_AUTH,
        chat_id="chat-1",
        search_space_id=1,
        assistant_content="error message",
        citations=[],
    )
    spy.assert_awaited_once()
    assert spy.await_args.kwargs["keywords"] is None


@pytest.mark.unit
def test_keywords_attached_to_assistant_role_only(monkeypatch):
    spy = _spy_append_turn(monkeypatch)
    server._append_assistant_turn_and_refresh(
        auth=_AUTH,
        chat_id="chat-1",
        search_space_id=1,
        assistant_content="the answer",
        citations=None,
        keywords=["k"],
    )
    # Every append_turn call from the assistant helper is role="assistant",
    # never "user" (the user-turn append is a separate, untouched code path).
    for call in spy.await_args_list:
        assert call.kwargs["role"] == "assistant"
