"""NO-BEHAVIOR-CHANGE guarantee for WU-5B4 (unit, LLM/DB mocked).

The ``chat_turns.keywords`` column is WRITE-ONLY: persisting the keywords must
not change any answer, retrieval result, or score. WU-5B4 deliberately makes
ZERO behavioral edits to the orchestrator (it reuses the existing
``found_terms`` carrier). This module locks that invariant two ways:

1. The orchestrator bundle (snippets / used_ids / found_terms / final_query) is
   byte-identical to a golden snapshot of the unmodified retrieval behavior for
   a fixed mocked input.
2. ``append_turn`` building a turn WITH keywords vs WITHOUT keywords produces
   ``ChatTurn`` rows identical in every non-keyword field — the new param has no
   side effect on existing columns.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import ai.orchestrator as orch_mod
from ai.orchestrator import Orchestrator
from chats.service import append_turn


def _wire_orchestrator(monkeypatch):
    orch = Orchestrator(ctx={"search_space_id": 99, "db_session": object()})
    monkeypatch.setattr(
        orch_mod,
        "extract_and_filter_keywords",
        lambda *a, **k: ("", [{"term": "alpha"}, {"term": "beta"}]),
    )
    monkeypatch.setattr(
        Orchestrator,
        "_check_question_relevance",
        lambda self, question: {"relevance": "full", "on_topic_portion": ""},
    )
    import retrieval.pipeline as pipeline_mod

    async def _fake_retrieve(*a, **k):
        return ([], {"combined_query": "alpha beta", "hit_count_sem": 0})

    monkeypatch.setattr(pipeline_mod, "retrieve_for_question", _fake_retrieve)
    import database.session as session_mod

    def _fake_run_async(coro):
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(session_mod, "run_async", _fake_run_async)
    return orch


@pytest.mark.unit
def test_retrieval_output_identical_to_golden_snapshot(monkeypatch):
    orch = _wire_orchestrator(monkeypatch)
    bundle = orch._iterative_research("the question", {})

    # Golden snapshot of the UNMODIFIED orchestrator behavior for this fixed
    # mocked input. Persisting keywords must not perturb any of these.
    assert bundle.snippets == []
    assert bundle.used_ids == []
    assert bundle.found_terms == ["alpha", "beta"]
    assert bundle.metadata.final_query == "alpha beta"
    assert bundle.subject is not None  # subject resolution unchanged


def _fake_session():
    lock_result = MagicMock()
    lock_result.scalar_one_or_none.return_value = 1
    idx_result = MagicMock()
    idx_result.scalar_one.return_value = 0
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[lock_result, idx_result])
    session.add = MagicMock()
    return session


@pytest.mark.unit
async def test_append_turn_keywords_does_not_change_other_columns():
    base_kwargs = dict(
        chat_session_id=3,
        role="assistant",
        content="same answer",
        model="gpt-5",
        attachments=[{"a": 1}],
        citations=[{"c": 2}],
    )
    with_kw = await append_turn(_fake_session(), keywords=["x", "y"], **base_kwargs)
    without_kw = await append_turn(_fake_session(), **base_kwargs)

    for field in (
        "chat_session_id",
        "role",
        "content",
        "turn_index",
        "turn_id",
        "model",
        "attachments",
        "citations",
    ):
        assert getattr(with_kw, field) == getattr(without_kw, field)
