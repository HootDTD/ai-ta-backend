"""Tests for ai/router/wiring.py — orchestrator glue between cache, router,
and the QA pipeline.

Unit tests cover bundle assembly and scoring reuse; the integration test runs
the FRESH → NONE lifecycle against the real-Postgres harness with a faked
router LLM.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import ai.router.wiring as wiring
from ai.router.llm_router import LLMRouter
from ai.router.mode import ModeDecision
from chats.bundle_cache import CachedBundle
from config.contracts import BundleSnippet, ParsedTask
from database.models import (
    Document,
    ChatRouterDecision,
    ChatSession,
    Course,
)


def _snippet(sid: int, *, marker: str | None = None) -> BundleSnippet:
    return BundleSnippet(
        id=str(sid),
        type="body",
        page=sid,
        section_path=f"sec {sid}",
        text=f"text {sid}",
        figure_id=None,
        why="hit",
        source_path="",
        doc_title="Calc Textbook",
        doc_short="Calc Textbook",
        citation_marker=marker or f"[Textbook, p. {sid}]",
        final_score={"final": 0.1},
        metadata={},
    )


def _scoring_row(sid: int, score: float) -> dict:
    return {
        "marker": f"[Textbook, p. {sid}]",
        "page": sid,
        "snippet_id": str(sid),
        "concept_term": "t",
        "importance": 1.0,
        "relevance": score,
        "directness": score,
        "base_score": score,
        "score": score,
        "context": "",
        "why": "",
    }


def _cached(snippet_ids: list[int], scores: dict[int, float] | None = None) -> CachedBundle:
    scores = scores or {}
    return CachedBundle(
        snippets=[_snippet(i) for i in snippet_ids],
        scoring={str(i): _scoring_row(i, s) for i, s in scores.items()},
        visible_docs_hash="h1",
        saved_turn=1,
    )


@pytest.mark.unit
def test_bundle_from_cache_preserves_citation_contract():
    cached = _cached([1, 2], scores={1: 0.8, 2: 0.6})
    bundle = wiring.bundle_from_cache(
        cached,
        q_effective="why does the p-series converge?",
        subject="Calculus",
        reason="cyu reply",
    )
    # Snippet-level citation contract (bundle-level validate() is only used by
    # the legacy Orchestrator path and additionally requires equations/glossary)
    for sn in bundle.snippets:
        sn.validate()
    assert len(bundle.snippets) == 2
    assert bundle.allowed_markers == ["[Textbook, p. 1]", "[Textbook, p. 2]"]
    assert bundle.provenance["source"] == "session_cache"
    assert set(bundle.provenance[wiring.CACHED_SCORES_KEY]) == {"1", "2"}


@pytest.mark.unit
def test_merge_augment_dedupes_and_caps(monkeypatch):
    monkeypatch.setenv("ROUTER_MAX_SNIPPETS", "4")
    cached = _cached([1, 2, 3], scores={1: 0.9, 2: 0.5, 3: 0.7})
    fresh = wiring.bundle_from_cache(  # convenient way to build a bundle
        CachedBundle(
            snippets=[_snippet(3), _snippet(4)], scoring={}, visible_docs_hash="h1", saved_turn=1
        ),
        q_effective="q",
        subject="Calculus",
        reason="",
    )
    merged = wiring.merge_augment_bundle(
        cached,
        fresh,
        q_effective="q",
        subject="Calculus",
        reason="follow-up",
    )
    ids = [sn.id for sn in merged.snippets]
    # Fresh first (3, 4), then best-scored cached (1: 0.9, then 3 deduped, then 2 vs 7? -> 1 then 2 capped)
    assert ids[:2] == ["3", "4"]
    assert len(ids) == 4
    assert ids.count("3") == 1  # deduped
    # Cached scores only attach to cached snippets present in the merge
    assert "4" not in merged.provenance[wiring.CACHED_SCORES_KEY]


@pytest.mark.unit
def test_scoring_rows_extracted_from_provenance():
    bundle = wiring.bundle_from_cache(
        _cached([1]),
        q_effective="q",
        subject="s",
        reason="",
    )
    bundle.provenance["citation_rankings"] = [
        _scoring_row(1, 0.7),
        {"no_snippet_id": True},
        "garbage",
    ]
    rows = wiring.scoring_rows_from_bundle(bundle)
    assert set(rows) == {"1"}
    assert rows["1"]["score"] == 0.7


@pytest.mark.unit
def test_none_mode_skips_scoring_pool_entirely(monkeypatch):
    """A fully cached bundle must not trigger a single scorer call."""
    import ai.main_ai as main_ai

    def _explode(*args, **kwargs):
        raise AssertionError("scoring pool must not be hit for cached snippets")

    monkeypatch.setattr(main_ai, "_score_and_answer_snippet", _explode)

    cached = _cached([1, 2], scores={1: 0.8, 2: 0.6})
    bundle = wiring.bundle_from_cache(
        cached,
        q_effective="why?",
        subject="Calculus",
        reason="cyu",
    )
    parsed = ParsedTask(problem_type="why?", asked_outputs=["answer"], asked_output_keys=["answer"])

    system, user_base, model = main_ai._prepare_solve_prompt(parsed, bundle)
    assert system and user_base
    rankings = bundle.provenance["citation_rankings"]
    assert {r["snippet_id"] for r in rankings} == {"1", "2"}


@pytest.mark.unit
def test_augment_scores_only_uncached_snippets(monkeypatch):
    import ai.main_ai as main_ai

    calls: list[str] = []

    def _fake_score(q, sn, importance, focus_term, model=None):
        calls.append(str(sn.id))
        return _scoring_row(int(sn.id), 0.5)

    monkeypatch.setattr(main_ai, "_score_and_answer_snippet", _fake_score)

    cached = _cached([1], scores={1: 0.8})
    fresh = wiring.bundle_from_cache(
        CachedBundle(snippets=[_snippet(2)], scoring={}, visible_docs_hash="h1", saved_turn=1),
        q_effective="q",
        subject="Calculus",
        reason="",
    )
    merged = wiring.merge_augment_bundle(
        cached,
        fresh,
        q_effective="q",
        subject="Calculus",
        reason="",
    )
    parsed = ParsedTask(problem_type="q", asked_outputs=["answer"], asked_output_keys=["answer"])
    main_ai._prepare_solve_prompt(parsed, merged)
    assert calls == ["2"]  # only the fresh snippet hits the pool


# ---------------------------------------------------------------------------
# Integration: FRESH → NONE lifecycle on real Postgres
# ---------------------------------------------------------------------------


def _fake_router(mode: str, confidence: float = 0.9) -> LLMRouter:
    fake = AsyncMock()
    fake.chat.completions.create.return_value.choices = [
        type(
            "C",
            (),
            {
                "message": type(
                    "M",
                    (),
                    {
                        "content": json.dumps(
                            {
                                "route": "conceptual_explainer",
                                "retrieval_mode": mode,
                                "confidence": confidence,
                                "reason": "test",
                            }
                        )
                    },
                )()
            },
        )()
    ]
    return LLMRouter(client=fake, model="gpt-4o-mini")


@pytest.mark.integration
async def test_fresh_then_none_lifecycle(db_session, monkeypatch):
    space = Course(
        name="Wiring test space",
        slug="wiring-test",
        subject_name="Calculus",
    )
    db_session.add(space)
    await db_session.flush()
    doc = Document(
        title="Calc Textbook",
        content="content",
        content_hash="wiring-test-hash",
        unique_identifier_hash="wiring-test-uid",
        course_id=space.id,
        material_kind="textbook",
        status="ready",
    )
    db_session.add(doc)
    await db_session.flush()
    chat = ChatSession(
        chat_id="wiring-test-chat",
        user_id="00000000-0000-0000-0000-000000000002",
        search_space_id=space.id,
        meta={},
        memory_summary="",
    )
    db_session.add(chat)
    await db_session.flush()

    @contextlib.asynccontextmanager
    async def _session_cm():
        yield db_session

    monkeypatch.setattr(wiring, "get_async_session", _session_cm)
    # Commit inside persist would end the test transaction; flush instead.
    monkeypatch.setattr(db_session, "commit", db_session.flush)
    monkeypatch.setattr(wiring, "_get_llm_router", lambda: _fake_router("NONE"))

    # Turn 1: no cache yet → FRESH without an LLM call
    ctx1 = await wiring.prepare_router_context(
        chat_id=chat.chat_id,
        user_id=chat.user_id,
        search_space_id=space.id,
        question="What is a p-series?",
        has_attachments=False,
    )
    assert ctx1 is not None
    assert ctx1.decision.mode == "FRESH"
    assert ctx1.decision.llm_invoked is False
    assert ctx1.cached is None

    # Persist turn 1's bundle (uses real chunk? no — snippet ids must be real chunks)
    from database.models import DocumentChunk

    chunk = DocumentChunk(course_id=doc.course_id, content="c", document_id=doc.id, chunk_type="body", page_number=1)
    db_session.add(chunk)
    await db_session.flush()

    bundle1 = wiring.bundle_from_cache(
        CachedBundle(snippets=[_snippet(chunk.id)], scoring={}, visible_docs_hash="", saved_turn=0),
        q_effective="What is a p-series?",
        subject="Calculus",
        reason="",
    )
    bundle1.provenance["citation_rankings"] = [_scoring_row(chunk.id, 0.8)]
    await wiring.persist_turn_outcome(
        chat_id=chat.chat_id,
        user_id=chat.user_id,
        ctx=ctx1,
        bundle=bundle1,
        question="What is a p-series?",
        latency_retrieval_ms=100,
        latency_answer_ms=200,
    )

    # Turn 2: cache present → fake router says NONE → cached bundle returned
    ctx2 = await wiring.prepare_router_context(
        chat_id=chat.chat_id,
        user_id=chat.user_id,
        search_space_id=space.id,
        question="B",
        has_attachments=False,
    )
    assert ctx2 is not None
    assert ctx2.decision.mode == "NONE"
    assert ctx2.decision.llm_invoked is True
    assert ctx2.cached is not None
    assert [sn.id for sn in ctx2.cached.snippets] == [str(chunk.id)]
    # Cached scoring row survives → scoring wave will be skipped
    assert str(chunk.id) in ctx2.cached.scoring

    # Attachments force FRESH even with a warm cache
    ctx3 = await wiring.prepare_router_context(
        chat_id=chat.chat_id,
        user_id=chat.user_id,
        search_space_id=space.id,
        question="what is this?",
        has_attachments=True,
    )
    assert ctx3 is not None
    assert ctx3.decision.mode == "FRESH"
    assert ctx3.decision.llm_invoked is False

    # A change to the visible document set invalidates the cache → FRESH
    # (new ready doc shifts the fingerprint away from the one stored at save)
    doc2 = Document(
        title="Week 2 Notes",
        content="content",
        content_hash="wiring-test-hash-2",
        unique_identifier_hash="wiring-test-uid-2",
        course_id=space.id,
        material_kind="other",
        status="ready",
    )
    db_session.add(doc2)
    await db_session.flush()
    ctx4 = await wiring.prepare_router_context(
        chat_id=chat.chat_id,
        user_id=chat.user_id,
        search_space_id=space.id,
        question="B",
        has_attachments=False,
    )
    assert ctx4 is not None
    assert ctx4.cached is None
    assert ctx4.decision.mode == "FRESH"

    # Telemetry row was written for turn 1
    rows = (
        (
            await db_session.execute(
                select(ChatRouterDecision).where(ChatRouterDecision.chat_session_id == chat.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].retrieval_mode == "FRESH"
    assert rows[0].query == "What is a p-series?"
    assert rows[0].latency_retrieval_ms == 100


# ---------------------------------------------------------------------------
# Edge paths: env knobs, fail-open branches, malformed inputs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_env_knob_defaults(monkeypatch):
    monkeypatch.delenv("ROUTER_AUGMENT_TOP_K", raising=False)
    monkeypatch.delenv("ROUTER_AUGMENT_TOKEN_BUDGET", raising=False)
    monkeypatch.delenv("ROUTER_MAX_SNIPPETS", raising=False)
    monkeypatch.setenv("K_SEM", "17")
    assert wiring.augment_top_k() == 8
    assert wiring.augment_token_budget() == 2000
    assert wiring.max_merged_snippets() == 17


@pytest.mark.unit
def test_get_llm_router_is_cached_singleton(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(wiring, "_llm_router", None)
    first = wiring._get_llm_router()
    second = wiring._get_llm_router()
    assert first is second


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prepare_router_context_fails_open_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(wiring, "get_async_session", _boom)
    result = await wiring.prepare_router_context(
        chat_id="c",
        user_id="u",
        search_space_id=1,
        question="q",
        has_attachments=False,
    )
    assert result is None


@pytest.mark.unit
def test_merge_tolerates_bad_cached_score_and_respects_cap(monkeypatch):
    monkeypatch.setenv("ROUTER_MAX_SNIPPETS", "1")
    cached = CachedBundle(
        snippets=[_snippet(1), _snippet(2)],
        scoring={"1": {"score": object()}},  # unfloatable -> treated as 0.0
        visible_docs_hash="h1",
        saved_turn=1,
    )
    fresh = wiring.bundle_from_cache(
        CachedBundle(snippets=[_snippet(3)], scoring={}, visible_docs_hash="h1", saved_turn=1),
        q_effective="q",
        subject="Calculus",
        reason="",
    )
    merged = wiring.merge_augment_bundle(
        cached, fresh, q_effective="q", subject="Calculus", reason=""
    )
    # Cap of 1 is already filled by the fresh snippet; no cached ones added.
    assert [sn.id for sn in merged.snippets] == ["3"]


@pytest.mark.unit
def test_scoring_rows_tolerates_malformed_provenance():
    bundle = wiring.bundle_from_cache(_cached([1]), q_effective="q", subject="s", reason="")
    bundle.provenance = "not-a-dict"
    assert wiring.scoring_rows_from_bundle(bundle) == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_persist_turn_outcome_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(wiring, "get_async_session", _boom)
    await wiring.persist_turn_outcome(
        chat_id="c",
        user_id="u",
        ctx=wiring.RouterTurnContext(
            chat_session_id=1,
            decision=ModeDecision(
                mode="FRESH",
                route="",
                confidence=1.0,
                reason="",
                llm_invoked=False,
                latency_ms=0,
            ),
            cached=None,
            visible_docs_hash="",
        ),
        bundle=None,
        question="q",
    )


@pytest.mark.unit
def test_prepare_solve_prompt_tolerates_bad_cached_scores_shape(monkeypatch):
    """cached_citation_scores that is not a mapping degrades to full scoring."""
    import ai.main_ai as main_ai

    calls: list[str] = []

    def _fake_score(q, sn, importance, focus_term, model=None):
        calls.append(str(sn.id))
        return _scoring_row(int(sn.id), 0.5)

    monkeypatch.setattr(main_ai, "_score_and_answer_snippet", _fake_score)
    bundle = wiring.bundle_from_cache(_cached([1]), q_effective="q", subject="s", reason="")
    bundle.provenance[wiring.CACHED_SCORES_KEY] = ["not", "a", "mapping"]
    parsed = ParsedTask(problem_type="q", asked_outputs=["answer"], asked_output_keys=["answer"])
    main_ai._prepare_solve_prompt(parsed, bundle)
    assert calls == ["1"]  # fell back to scoring everything


@pytest.mark.unit
def test_prepare_solve_prompt_scorer_failure_yields_placeholder(monkeypatch):
    import ai.main_ai as main_ai

    def _boom(q, sn, importance, focus_term, model=None):
        raise RuntimeError("scorer down")

    monkeypatch.setattr(main_ai, "_score_and_answer_snippet", _boom)
    bundle = wiring.bundle_from_cache(_cached([1]), q_effective="q", subject="s", reason="")
    bundle.provenance.pop(wiring.CACHED_SCORES_KEY, None)
    parsed = ParsedTask(problem_type="q", asked_outputs=["answer"], asked_output_keys=["answer"])
    main_ai._prepare_solve_prompt(parsed, bundle)
    rankings = bundle.provenance["citation_rankings"]
    assert len(rankings) == 1
    assert rankings[0]["score"] == 0.0


@pytest.mark.integration
async def test_memory_loader_meta_refresh_preserves_cache_fingerprint(db_session, monkeypatch):
    """Regression: the per-turn chat-memory loader refreshes session.meta with
    workspace info; it must MERGE (not replace) or it erases the bundle_cache
    fingerprint and the router invalidates the session cache on every turn.

    Reproduces the staging sequence: turn-1 persist writes the fingerprint →
    turn-2 memory load refreshes meta → prepare must still find a valid cache.
    """
    import contextlib
    from types import SimpleNamespace

    import server

    space = Course(
        name="Meta merge test space", slug="meta-merge-test", subject_name="Calculus"
    )
    db_session.add(space)
    await db_session.flush()
    doc = Document(
        title="Calc Textbook",
        content="content",
        content_hash="meta-merge-hash",
        unique_identifier_hash="meta-merge-uid",
        course_id=space.id,
        material_kind="textbook",
        status="ready",
    )
    db_session.add(doc)
    await db_session.flush()
    from database.models import DocumentChunk

    chunk = DocumentChunk(course_id=doc.course_id, content="c", document_id=doc.id, chunk_type="body", page_number=1)
    db_session.add(chunk)
    await db_session.flush()

    user_id = "00000000-0000-0000-0000-000000000004"
    chat = ChatSession(
        chat_id="meta-merge-chat",
        user_id=user_id,
        search_space_id=space.id,
        meta={},
        memory_summary="",
    )
    db_session.add(chat)
    await db_session.flush()

    @contextlib.asynccontextmanager
    async def _session_cm():
        yield db_session

    monkeypatch.setattr(wiring, "get_async_session", _session_cm)
    monkeypatch.setattr(server, "get_async_session", _session_cm)
    monkeypatch.setattr(db_session, "commit", db_session.flush)
    monkeypatch.setattr(wiring, "_get_llm_router", lambda: _fake_router("NONE"))

    # Turn 1: prepare (FRESH) + persist writes the cache fingerprint into meta
    ctx1 = await wiring.prepare_router_context(
        chat_id=chat.chat_id,
        user_id=user_id,
        search_space_id=space.id,
        question="What is a p-series?",
        has_attachments=False,
    )
    bundle1 = wiring.bundle_from_cache(
        CachedBundle(snippets=[_snippet(chunk.id)], scoring={}, visible_docs_hash="", saved_turn=0),
        q_effective="What is a p-series?",
        subject="Calculus",
        reason="",
    )
    bundle1.provenance["citation_rankings"] = [_scoring_row(chunk.id, 0.8)]
    await wiring.persist_turn_outcome(
        chat_id=chat.chat_id, user_id=user_id, ctx=ctx1, bundle=bundle1, question="q1"
    )
    assert "bundle_cache" in (chat.meta or {})

    # Turn 2 starts: the memory loader refreshes meta with workspace info —
    # this is the step that used to clobber the fingerprint.
    auth = SimpleNamespace(user_id=user_id)
    await server._load_memory_and_append_user_turn_async(
        auth=auth,
        chat_id=chat.chat_id,
        search_space_id=space.id,
        user_content="B",
        attachments=[],
        meta={"search_space_id": space.id, "class_name": "Calc", "subject_name": "Calculus"},
    )
    assert "bundle_cache" in (chat.meta or {}), "memory loader must not erase bundle_cache"
    assert chat.meta["class_name"] == "Calc"  # refresh still applied

    # Turn 2 prepare: cache must survive and route NONE
    ctx2 = await wiring.prepare_router_context(
        chat_id=chat.chat_id,
        user_id=user_id,
        search_space_id=space.id,
        question="B",
        has_attachments=False,
    )
    assert ctx2 is not None
    assert ctx2.cached is not None, "cache must survive the meta refresh"
    assert ctx2.decision.mode == "NONE"
