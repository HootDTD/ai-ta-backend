from __future__ import annotations

import contextlib

import pytest
from sqlalchemy import func, select

from database.models import AITAChunk, AITADocument, SearchSpace
from indexing import checkpoint_indexer as ci
from tests.fakes.embeddings import fake_embedding

pytestmark = pytest.mark.integration


def _pairs(pages: list[int]) -> list[tuple[str, dict]]:
    out = []
    for p in pages:
        out.append(
            (
                f"page-{p}-text",
                {"page_number": p, "chunk_type": "body", "section_path": "", "figure_id": None},
            )
        )
    return out


def _fake_embed(texts):
    return [fake_embedding(t) for t in texts]


async def _make_document(db_session) -> AITADocument:
    space = SearchSpace(name="Fluids", slug="fluids-ckpt", subject_name="ME")
    db_session.add(space)
    await db_session.flush()
    doc = AITADocument(
        title="Textbook",
        content="Pending...",
        content_hash="ckpt-hash",
        unique_identifier_hash="ckpt-uid",
        search_space_id=space.id,
    )
    db_session.add(doc)
    await db_session.commit()
    return doc


def _session_factory(db_session):
    """A factory yielding the test's transactional session (savepoint-isolated)."""

    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _count_chunks(db_session, doc_id) -> int:
    res = await db_session.execute(
        select(func.count()).select_from(AITAChunk).where(AITAChunk.document_id == doc_id)
    )
    return int(res.scalar_one())


async def test_embed_and_persist_writes_all_pages(db_session):
    doc = await _make_document(db_session)
    progress = []

    last = await ci.embed_and_persist_chunks(
        session_factory=_session_factory(db_session),
        document_id=doc.id,
        chunk_pairs=_pairs([1, 2, 3]),
        after_page=0,
        batch_size=2,
        on_progress=lambda p: progress.append(p),
        embed_fn=lambda texts: [fake_embedding(t) for t in texts],
    )

    assert last == 3
    assert progress == [2, 3]  # batch [1,2] then [3]
    assert await _count_chunks(db_session, doc.id) == 3


async def test_embed_and_persist_resumes_after_pointer(db_session):
    doc = await _make_document(db_session)
    embedded_pages = []

    def spy_embed(texts):
        embedded_pages.extend(texts)
        return [fake_embedding(t) for t in texts]

    last = await ci.embed_and_persist_chunks(
        session_factory=_session_factory(db_session),
        document_id=doc.id,
        chunk_pairs=_pairs([1, 2, 3]),
        after_page=2,  # pages 1,2 already done
        batch_size=10,
        embed_fn=spy_embed,
    )

    assert last == 3
    assert embedded_pages == ["page-3-text"]  # pages <= 2 NOT re-embedded
    assert await _count_chunks(db_session, doc.id) == 1


async def test_embed_and_persist_is_idempotent_per_page(db_session):
    doc = await _make_document(db_session)
    fn = _fake_embed
    kwargs = dict(
        session_factory=_session_factory(db_session),
        document_id=doc.id,
        chunk_pairs=_pairs([1, 2]),
        after_page=0,
        batch_size=10,
        embed_fn=fn,
    )

    await ci.embed_and_persist_chunks(**kwargs)
    await ci.embed_and_persist_chunks(**kwargs)  # re-run same pages

    assert await _count_chunks(db_session, doc.id) == 2  # no duplicates


async def test_batch_failure_rolls_back_and_resume_completes(db_session):
    """A mid-batch failure must not commit that page; a resume finishes the job."""
    doc = await _make_document(db_session)
    fn = _fake_embed

    calls = {"n": 0}

    def explode_on_second_batch(texts):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("connection was closed in the middle of operation")
        return [fake_embedding(t) for t in texts]

    # First run: page 1 commits, page 2 batch raises before persist.
    with pytest.raises(RuntimeError):
        await ci.embed_and_persist_chunks(
            session_factory=_session_factory(db_session),
            document_id=doc.id,
            chunk_pairs=_pairs([1, 2, 3]),
            after_page=0,
            batch_size=1,
            embed_fn=explode_on_second_batch,
        )
    assert await _count_chunks(db_session, doc.id) == 1  # only page 1 persisted

    # Resume from page 1: pages 2,3 complete, no duplicate of page 1.
    last = await ci.embed_and_persist_chunks(
        session_factory=_session_factory(db_session),
        document_id=doc.id,
        chunk_pairs=_pairs([1, 2, 3]),
        after_page=1,
        batch_size=1,
        embed_fn=fn,
    )
    assert last == 3
    assert await _count_chunks(db_session, doc.id) == 3


async def test_finalize_document_sets_ready_and_writes_null_page_chunks(db_session):
    from database.models import DocumentStatus
    from indexing.checkpoint_indexer import build_doc_content, finalize_document

    doc = await _make_document(db_session)
    pairs = _pairs([1, 2]) + [
        (
            "intro-no-page",
            {"page_number": None, "chunk_type": "heading", "section_path": "", "figure_id": None},
        )
    ]

    content = build_doc_content(pairs, fallback_title="Textbook")
    assert "page-1-text" in content

    async with _session_factory(db_session)() as s:
        await finalize_document(
            s,
            document_id=doc.id,
            chunk_pairs=pairs,
            doc_content=content,
            doc_embedding=fake_embedding(content),
            page_count=2,
            embed_fn=lambda t: [fake_embedding(x) for x in t],
        )
        await s.commit()

    refreshed = await db_session.get(AITADocument, doc.id)
    await db_session.refresh(refreshed)
    assert DocumentStatus.is_state(refreshed.status, DocumentStatus.READY)
    assert refreshed.page_count == 2
    # the one null-page chunk was written in finalize
    assert await _count_chunks(db_session, doc.id) == 1
