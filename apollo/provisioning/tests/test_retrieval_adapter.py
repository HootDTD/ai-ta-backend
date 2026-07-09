"""Unit tests for the course-scoped retrieval-grounding adapter.

``make_course_retrieve_fn`` converts hybrid-search chunk dicts into immutable
``GroundingSpan`` values, course-scoped to the BOUND ``search_space_id``. PURE —
``AITAHybridSearchRetriever`` is patched at the module surface; NO DB, NO network.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import apollo.provisioning.retrieval_adapter as adapter_mod
from apollo.provisioning.retrieval_adapter import make_course_retrieve_fn
from apollo.provisioning.scrape import chunk_content_hash
from apollo.provisioning.solution import GroundingSpan

# pytest.ini sets asyncio_mode = auto.


class _FakeRetriever:
    """Records constructor args + the hybrid_search call; returns canned rows."""

    last_init: tuple | None = None
    last_call: dict | None = None
    rows: list[dict] = []

    def __init__(self, db_session, search_space_id):  # noqa: ANN001
        type(self).last_init = (db_session, search_space_id)

    async def hybrid_search(self, query_text, top_k=60, material_kind=None):  # noqa: ANN001
        type(self).last_call = {"query_text": query_text, "top_k": top_k}
        return list(type(self).rows)


def _question(*, problem_text="Find P2.", chash="c1"):
    return SimpleNamespace(problem_text=problem_text, chunk_content_hash=chash)


def _install(monkeypatch, rows):
    _FakeRetriever.rows = rows
    _FakeRetriever.last_init = None
    _FakeRetriever.last_call = None
    monkeypatch.setattr(adapter_mod, "AITAHybridSearchRetriever", _FakeRetriever)


async def test_maps_chunk_dicts_to_grounding_spans(monkeypatch):
    rows = [
        {"content": "Bernoulli: P + 0.5*rho*v^2 = const", "document_id": 7, "page_number": 3},
        {"content": "Continuity: A1 v1 = A2 v2", "document_id": 7, "page_number": 4},
    ]
    _install(monkeypatch, rows)
    retrieve = make_course_retrieve_fn(object(), search_space_id=42)

    spans = await retrieve(_question())

    assert isinstance(spans, tuple)
    assert all(isinstance(s, GroundingSpan) for s in spans)
    assert [s.text for s in spans] == [r["content"] for r in rows]
    assert spans[0].document_id == 7
    assert spans[0].page == 3
    assert spans[0].chunk_content_hash == chunk_content_hash(rows[0]["content"])
    assert all(s.carries_solution is False for s in spans)


async def test_retrieve_skips_rows_missing_content(db_session, monkeypatch):
    """REGRESSION: a hybrid_search row lacking a 'content' key must be SKIPPED,
    not crash the whole document with a KeyError (the orchestrator maps an
    unexpected exception to a per-DOCUMENT abort). DISCRIMINATING: reverting to
    row['content'] REDs with KeyError."""

    async def _fake_search(self, query_text, top_k):  # noqa: ANN001
        return [
            {"content": "good chunk", "document_id": 1, "page_number": 2},
            {"document_id": 9, "page_number": 3},  # NO 'content' key
        ]

    monkeypatch.setattr(
        adapter_mod.AITAHybridSearchRetriever, "hybrid_search", _fake_search
    )
    retrieve = make_course_retrieve_fn(db_session, search_space_id=1)

    class _Q:
        problem_text = "find downstream pressure P2"
        chunk_content_hash = "abc"

    spans = await retrieve(_Q())
    assert len(spans) == 1
    assert spans[0].text == "good chunk"


async def test_query_is_problem_text_and_topk_forwarded(monkeypatch):
    _install(monkeypatch, [])
    retrieve = make_course_retrieve_fn(object(), search_space_id=1, top_k=9)
    await retrieve(_question(problem_text="What is P2?"))
    assert _FakeRetriever.last_call == {"query_text": "What is P2?", "top_k": 9}


async def test_retriever_constructed_with_bound_scope(monkeypatch):
    _install(monkeypatch, [])
    sentinel_db = object()
    retrieve = make_course_retrieve_fn(sentinel_db, search_space_id=99)
    await retrieve(_question())
    db_arg, ss_arg = _FakeRetriever.last_init
    assert db_arg is sentinel_db
    assert ss_arg == 99  # never a foreign course


async def test_empty_result_returns_empty_tuple_and_logs(monkeypatch, caplog):
    _install(monkeypatch, [])
    retrieve = make_course_retrieve_fn(object(), search_space_id=5)
    with caplog.at_level(logging.INFO):
        spans = await retrieve(_question(chash="abc"))
    assert spans == ()
    assert any(r.getMessage() == "provisioning_retrieval_empty" for r in caplog.records)
