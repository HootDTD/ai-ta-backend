"""Tests for chats/bundle_cache.py — session bundle cache for the orchestrator.

Unit tests cover fingerprinting and payload shapes; integration tests run the
full save/load/evict cycle against the real-Postgres ``db_session`` harness.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from chats.bundle_cache import (
    BUNDLE_CACHE_MAX_CHUNKS,
    CachedBundle,
    load_bundle_cache,
    save_bundle_cache,
    visible_docs_fingerprint,
)
from config.contracts import BundleSnippet
from database.models import (
    AITAChunk,
    AITADocument,
    ChatSession,
    ChatSessionSnippet,
    SearchSpace,
)


def _snippet(chunk_id: int, *, score: float = 0.5, title: str = "Calc Textbook") -> BundleSnippet:
    return BundleSnippet(
        id=str(chunk_id),
        type="body",
        page=10 + chunk_id,
        section_path=f"7.{chunk_id} Integration",
        text=f"snippet text {chunk_id}",
        figure_id=None,
        why="hit",
        source_path="",
        doc_title=title,
        doc_short=title[:40],
        citation_marker=f"[Textbook, p. {10 + chunk_id}]",
        final_score={"final": score},
        metadata={},
    )


def _scoring_row(chunk_id: int, score: float) -> dict:
    return {
        "marker": f"[Textbook, p. {10 + chunk_id}]",
        "page": 10 + chunk_id,
        "snippet_id": str(chunk_id),
        "concept_term": "integration",
        "importance": 1.0,
        "relevance": score,
        "directness": score,
        "base_score": score,
        "score": score,
        "context": "",
        "why": "cached",
    }


@pytest.mark.unit
def test_fingerprint_is_order_insensitive_and_deterministic():
    assert visible_docs_fingerprint([3, 1, 2]) == visible_docs_fingerprint([1, 2, 3])
    assert visible_docs_fingerprint([1, 2]) != visible_docs_fingerprint([1, 2, 3])
    assert visible_docs_fingerprint([]) == visible_docs_fingerprint([])


@pytest.mark.unit
def test_cached_bundle_titles_dedupe_and_include_sections():
    snippets = [_snippet(1), _snippet(2), _snippet(3, title="Slides Week 2")]
    bundle = CachedBundle(snippets=snippets, scoring={}, visible_docs_hash="", saved_turn=1)
    titles = bundle.titles
    assert "Calc Textbook — 7.1 Integration" in titles
    assert "Slides Week 2 — 7.3 Integration" in titles
    assert len(titles) == len(set(titles))


# ---------------------------------------------------------------------------
# Integration: real Postgres via the pgvector test container
# ---------------------------------------------------------------------------


async def _seed_session_with_chunks(db_session, n_chunks: int):
    space = SearchSpace(
        name="Bundle cache test space",
        slug="bundle-cache-test",
        subject_name="Calculus",
    )
    db_session.add(space)
    await db_session.flush()

    doc = AITADocument(
        title="Calc Textbook",
        content="calc textbook content",
        content_hash="bundle-cache-test-hash",
        unique_identifier_hash="bundle-cache-test-uid",
        search_space_id=space.id,
        material_kind="textbook",
        status={"state": "ready"},
    )
    db_session.add(doc)
    await db_session.flush()

    chunk_ids = []
    for i in range(n_chunks):
        chunk = AITAChunk(
            content=f"chunk content {i}",
            document_id=doc.id,
            chunk_type="body",
            page_number=10 + i,
        )
        db_session.add(chunk)
        await db_session.flush()
        chunk_ids.append(chunk.id)

    session = ChatSession(
        chat_id="cache-test-chat",
        user_id="00000000-0000-0000-0000-000000000001",
        search_space_id=space.id,
        meta={},
        memory_summary="",
    )
    db_session.add(session)
    await db_session.flush()
    return session, chunk_ids


@pytest.mark.integration
async def test_save_then_load_roundtrip(db_session):
    session, chunk_ids = await _seed_session_with_chunks(db_session, 3)
    snippets = [_snippet(cid, score=0.9 - 0.1 * i) for i, cid in enumerate(chunk_ids)]
    scoring = {str(cid): _scoring_row(cid, 0.9 - 0.1 * i) for i, cid in enumerate(chunk_ids)}

    await save_bundle_cache(
        db_session,
        chat_session=session,
        turn_index=1,
        snippets=snippets,
        scoring=scoring,
        visible_docs_hash="abc123",
        replace=True,
    )
    await db_session.flush()

    cached = await load_bundle_cache(db_session, chat_session=session)
    assert cached is not None
    assert len(cached.snippets) == 3
    assert cached.visible_docs_hash == "abc123"
    assert cached.saved_turn == 1
    # Highest score first
    assert cached.snippets[0].id == str(chunk_ids[0])
    # Scoring rows survive the roundtrip with markers intact
    first = cached.scoring[str(chunk_ids[0])]
    assert first["marker"] == snippets[0].citation_marker
    # Citation markers preserved on rehydrated snippets
    assert all(sn.citation_marker for sn in cached.snippets)


@pytest.mark.integration
async def test_load_returns_none_for_empty_session(db_session):
    session, _ = await _seed_session_with_chunks(db_session, 0)
    assert await load_bundle_cache(db_session, chat_session=session) is None


@pytest.mark.integration
async def test_replace_drops_previous_cache(db_session):
    session, chunk_ids = await _seed_session_with_chunks(db_session, 4)
    first_half = [_snippet(cid) for cid in chunk_ids[:2]]
    second_half = [_snippet(cid) for cid in chunk_ids[2:]]

    await save_bundle_cache(
        db_session,
        chat_session=session,
        turn_index=1,
        snippets=first_half,
        scoring={},
        visible_docs_hash="h1",
        replace=True,
    )
    await db_session.flush()
    await save_bundle_cache(
        db_session,
        chat_session=session,
        turn_index=2,
        snippets=second_half,
        scoring={},
        visible_docs_hash="h2",
        replace=True,
    )
    await db_session.flush()

    cached = await load_bundle_cache(db_session, chat_session=session)
    assert cached is not None
    assert {sn.id for sn in cached.snippets} == {str(c) for c in chunk_ids[2:]}
    assert cached.visible_docs_hash == "h2"


@pytest.mark.integration
async def test_merge_updates_last_used_turn(db_session):
    session, chunk_ids = await _seed_session_with_chunks(db_session, 3)
    snippets = [_snippet(cid) for cid in chunk_ids]

    await save_bundle_cache(
        db_session,
        chat_session=session,
        turn_index=1,
        snippets=snippets,
        scoring={},
        visible_docs_hash="h1",
        replace=True,
    )
    await db_session.flush()
    # AUGMENT-style merge on turn 3 reuses two of three chunks
    await save_bundle_cache(
        db_session,
        chat_session=session,
        turn_index=3,
        snippets=snippets[:2],
        scoring={},
        visible_docs_hash="h1",
        replace=False,
    )
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(ChatSessionSnippet).where(ChatSessionSnippet.chat_session_id == session.id)
            )
        )
        .scalars()
        .all()
    )
    by_chunk = {r.chunk_id: r for r in rows}
    assert by_chunk[chunk_ids[0]].last_used_turn == 3
    assert by_chunk[chunk_ids[0]].first_seen_turn == 1
    assert by_chunk[chunk_ids[2]].last_used_turn == 1


@pytest.mark.integration
async def test_lru_eviction_respects_cap(db_session):
    n = BUNDLE_CACHE_MAX_CHUNKS + 5
    session, chunk_ids = await _seed_session_with_chunks(db_session, n)

    await save_bundle_cache(
        db_session,
        chat_session=session,
        turn_index=1,
        snippets=[_snippet(chunk_ids[0], score=0.1)],
        scoring={},
        visible_docs_hash="h1",
        replace=True,
    )
    await db_session.flush()
    rest = [_snippet(cid, score=0.9) for cid in chunk_ids[1:]]
    scoring = {str(cid): _scoring_row(cid, 0.9) for cid in chunk_ids[1:]}
    await save_bundle_cache(
        db_session,
        chat_session=session,
        turn_index=2,
        snippets=rest,
        scoring=scoring,
        visible_docs_hash="h1",
        replace=False,
    )
    await db_session.flush()

    rows = (
        (
            await db_session.execute(
                select(ChatSessionSnippet).where(ChatSessionSnippet.chat_session_id == session.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == BUNDLE_CACHE_MAX_CHUNKS
    # The stale low-score turn-1 chunk is the one evicted
    assert chunk_ids[0] not in {r.chunk_id for r in rows}


@pytest.mark.integration
async def test_corrupt_payload_degrades_to_cache_miss(db_session):
    session, chunk_ids = await _seed_session_with_chunks(db_session, 1)
    db_session.add(
        ChatSessionSnippet(
            chat_session_id=session.id,
            chunk_id=chunk_ids[0],
            original_score=0.5,
            first_seen_turn=1,
            last_used_turn=1,
            snippet_payload={"snippet": {"unexpected_field": True}},
        )
    )
    await db_session.flush()
    assert await load_bundle_cache(db_session, chat_session=session) is None


@pytest.mark.integration
async def test_non_dict_snippet_payload_is_skipped(db_session):
    session, chunk_ids = await _seed_session_with_chunks(db_session, 1)
    db_session.add(
        ChatSessionSnippet(
            chat_session_id=session.id,
            chunk_id=chunk_ids[0],
            original_score=0.5,
            first_seen_turn=1,
            last_used_turn=1,
            snippet_payload={"snippet": "not-a-dict"},
        )
    )
    await db_session.flush()
    assert await load_bundle_cache(db_session, chat_session=session) is None


@pytest.mark.integration
async def test_save_skips_non_integer_snippet_ids_and_bad_scores(db_session):
    session, chunk_ids = await _seed_session_with_chunks(db_session, 1)
    good = _snippet(chunk_ids[0])
    bad_id = _snippet(chunk_ids[0])
    bad_id.id = "not-an-int"

    await save_bundle_cache(
        db_session,
        chat_session=session,
        turn_index=1,
        snippets=[bad_id, good],
        scoring={str(chunk_ids[0]): {"score": "unparseable"}},
        visible_docs_hash="h1",
        replace=True,
    )
    await db_session.flush()

    cached = await load_bundle_cache(db_session, chat_session=session)
    assert cached is not None
    assert [sn.id for sn in cached.snippets] == [str(chunk_ids[0])]
    # Unparseable score degraded to 0.0 on the stored row
    rows = (
        (
            await db_session.execute(
                select(ChatSessionSnippet).where(ChatSessionSnippet.chat_session_id == session.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows[0].original_score == 0.0
