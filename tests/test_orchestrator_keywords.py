"""Orchestrator-side contract tests for WU-5B4 (no DB, LLM mocked).

WU-5B4 persists the ALREADY-COMPUTED ``extract_and_filter_keywords`` output. The
orchestrator already normalizes that output into a ``List[str]`` (<=8 concept
terms) and carries it on ``ResearchBundle.found_terms`` (``ai/orchestrator.py``
keyword block). These tests PIN that contract — the list a persist site would
read off ``found_terms`` is exactly the normalized term list — with all
LLM/network/DB calls monkeypatched (no live OpenAI, no Postgres).

NOTE: the production keyword-carrying method is ``Orchestrator._retrieve`` (the
public entry ``_iterative_research`` delegates straight to it). The plan refers
to it as ``_iterative_research_pgvector``; we drive it via the public
``_iterative_research`` entry so the test is robust to the internal name.
"""

import pytest

import ai.orchestrator as orch_mod
from ai.orchestrator import Orchestrator


def _make_orchestrator(monkeypatch, *, terms):
    """Build an Orchestrator whose retrieval is a deterministic no-op and whose
    keyword extractor returns ``terms`` (the raw extract_and_filter_keywords
    second-tuple element)."""

    orch = Orchestrator(ctx={"search_space_id": 42, "db_session": object()})

    # Keyword extractor: returns (context_summary, filtered_terms).
    monkeypatch.setattr(
        orch_mod,
        "extract_and_filter_keywords",
        lambda *a, **k: ("", terms),
    )
    # Relevance guard → full (so the keyword path runs, not the early-return).
    monkeypatch.setattr(
        Orchestrator,
        "_check_question_relevance",
        lambda self, question: {"relevance": "full", "on_topic_portion": ""},
    )
    # Retrieval is a no-op: zero snippets + minimal diagnostics.
    import retrieval.pipeline as pipeline_mod

    async def _fake_retrieve(*a, **k):
        return ([], {"combined_query": "q", "hit_count_sem": 0})

    monkeypatch.setattr(pipeline_mod, "retrieve_for_question", _fake_retrieve)
    # run_async is imported inside _retrieve from database.session — patch source.
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
def test_iterative_research_carries_keywords_to_found_terms(monkeypatch):
    orch = _make_orchestrator(
        monkeypatch,
        terms=[{"term": "momentum"}, {"term": "impulse"}, "force"],
    )
    bundle = orch._iterative_research("q", {})
    # dict-`term` entries + bare-str entries both flatten to the term string.
    assert bundle.found_terms == ["momentum", "impulse", "force"]
    # The same list is mirrored on the metadata.
    assert bundle.metadata.found_terms == ["momentum", "impulse", "force"]


@pytest.mark.unit
def test_keywords_passthrough_count(monkeypatch):
    """The orchestrator normalization does NOT re-cap; the <=8 guarantee lives
    upstream in extract_and_filter_keywords (spec L1463). Pin the ACTUAL
    behavior: a 12-term extractor output flows through unchanged (each valid
    term flattened), so a persist site relies on the upstream <=8 bound."""
    raw = [{"term": f"t{i}"} for i in range(12)]
    orch = _make_orchestrator(monkeypatch, terms=raw)
    bundle = orch._iterative_research("q", {})
    assert bundle.found_terms == [f"t{i}" for i in range(12)]


@pytest.mark.unit
def test_keywords_empty_on_extractor_failure(monkeypatch):
    """extract_and_filter_keywords raising → fail-open to [] (so persistence
    writes [], never crashes)."""
    orch = Orchestrator(ctx={"search_space_id": 42, "db_session": object()})

    def _boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(orch_mod, "extract_and_filter_keywords", _boom)
    monkeypatch.setattr(
        Orchestrator,
        "_check_question_relevance",
        lambda self, question: {"relevance": "full", "on_topic_portion": ""},
    )
    import retrieval.pipeline as pipeline_mod

    async def _fake_retrieve(*a, **k):
        return ([], {"combined_query": "q", "hit_count_sem": 0})

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

    bundle = orch._iterative_research("q", {})
    assert bundle.found_terms == []


@pytest.mark.unit
def test_keywords_skip_blank_and_malformed_entries(monkeypatch):
    """Whitespace-only strings and dicts with no usable term key are dropped;
    only clean term strings reach found_terms."""
    raw = [
        {"term": "  energy  "},  # stripped
        {"term": ""},  # empty → dropped
        "   ",  # blank str → dropped
        {"keyword": "work"},  # alt key honored
        {"nope": "x"},  # no term key → dropped
        "heat",  # bare str kept
    ]
    orch = _make_orchestrator(monkeypatch, terms=raw)
    bundle = orch._iterative_research("q", {})
    assert bundle.found_terms == ["energy", "work", "heat"]
