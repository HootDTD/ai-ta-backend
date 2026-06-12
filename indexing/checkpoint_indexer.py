"""Checkpointed embed-and-persist for long indexing jobs.

Splits a document's chunks into per-page work units, embeds each page-batch with
a single batched OpenAI call, and commits each batch in its own short-lived DB
session while advancing a resume pointer. No DB session is ever held while an
OpenAI call is in flight — the fix for the connection-reap failure on long jobs.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete

from database.models import AITAChunk, AITADocument, DocumentStatus

from .document_embedder import embed_texts

EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "128"))

ChunkPair = tuple[str, dict]


@dataclass(frozen=True)
class PageGroup:
    """All chunks belonging to one page, in document order."""

    page_number: int
    items: list[ChunkPair]


def group_pages(chunk_pairs: list[ChunkPair]) -> tuple[list[PageGroup], list[ChunkPair]]:
    """Split chunk pairs into per-page groups (ascending page) + null-page items.

    Chunks whose ``page_number`` is None are returned separately; they are
    persisted once in the finalize step (they are not resumable units).
    """
    by_page: dict[int, list[ChunkPair]] = {}
    null_items: list[ChunkPair] = []
    for text, meta in chunk_pairs:
        page = meta.get("page_number")
        if page is None:
            null_items.append((text, meta))
            continue
        by_page.setdefault(int(page), []).append((text, meta))
    page_groups = [PageGroup(page_number=p, items=by_page[p]) for p in sorted(by_page)]
    return page_groups, null_items


def plan_batches(
    page_groups: list[PageGroup],
    *,
    batch_size: int = EMBED_BATCH_SIZE,
    after_page: int = 0,
) -> Iterator[list[PageGroup]]:
    """Yield batches of *whole* pages with page_number > after_page.

    A batch accumulates complete pages until adding the next would exceed
    ``batch_size`` chunks. A single page larger than ``batch_size`` is emitted as
    its own batch (never split — the page is the commit/idempotency unit).
    """
    batch: list[PageGroup] = []
    count = 0
    for pg in page_groups:
        if pg.page_number <= after_page:
            continue
        size = len(pg.items)
        if batch and count + size > batch_size:
            yield batch
            batch, count = [], 0
        batch.append(pg)
        count += size
    if batch:
        yield batch


async def embed_and_persist_chunks(
    *,
    session_factory: Callable[[], object],
    document_id: int,
    chunk_pairs: list[ChunkPair],
    after_page: int = 0,
    batch_size: int = EMBED_BATCH_SIZE,
    on_progress: Callable[[int], object] | Callable[[int], Awaitable[None]] | None = None,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
) -> int:
    """Embed and persist chunks page-batch by page-batch, each in its own session.

    Returns the highest page number committed. ``on_progress(page)`` is invoked
    after each batch commits (may be sync or async). Pages with
    ``page_number <= after_page`` are skipped (resume). Re-running a page deletes
    and reinserts that page's chunks (idempotent).

    ``embed_fn`` defaults to the module-level :func:`embed_texts`, resolved at
    call time so tests can monkeypatch ``checkpoint_indexer.embed_texts``.
    """
    if embed_fn is None:
        embed_fn = embed_texts
    page_groups, _null_items = group_pages(chunk_pairs)
    last_page = after_page
    for batch in plan_batches(page_groups, batch_size=batch_size, after_page=after_page):
        page_numbers = [pg.page_number for pg in batch]
        items = [pair for pg in batch for pair in pg.items]
        vectors = embed_fn([text for text, _ in items])

        async with session_factory() as session:
            await session.execute(
                delete(AITAChunk).where(
                    AITAChunk.document_id == document_id,
                    AITAChunk.page_number.in_(page_numbers),
                )
            )
            for (text, meta), vector in zip(items, vectors, strict=True):
                session.add(
                    AITAChunk(
                        content=text,
                        embedding=vector,
                        page_number=meta.get("page_number"),
                        section_path=meta.get("section_path") or None,
                        chunk_type=meta.get("chunk_type") or "body",
                        figure_id=meta.get("figure_id"),
                        document_id=document_id,
                    )
                )
            await session.commit()

        last_page = max(page_numbers)
        if on_progress is not None:
            result = on_progress(last_page)
            if hasattr(result, "__await__"):
                await result
    return last_page


def build_doc_content(chunk_pairs: list[ChunkPair], *, fallback_title: str) -> str:
    """Document-level text for coarse retrieval: body/heading/ocr text, capped 2000 chars."""
    body = [
        text
        for text, meta in chunk_pairs
        if (meta.get("chunk_type") in ("body", "heading", "ocr", None))
    ]
    return (" ".join(body)[:2000]) or fallback_title


async def finalize_document(
    session,
    *,
    document_id: int,
    chunk_pairs: list[ChunkPair],
    doc_content: str,
    doc_embedding: list[float],
    page_count: int | None,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
) -> None:
    """Terminal write: persist null-page chunks + doc-level fields; mark READY.

    Runs in the caller's short-lived session. Does NOT commit (caller commits),
    so it composes with the rest of the upload/job finalize in one transaction.

    ``embed_fn`` defaults to the module-level :func:`embed_texts`, resolved at
    call time so tests can monkeypatch ``checkpoint_indexer.embed_texts``.
    """
    if embed_fn is None:
        embed_fn = embed_texts
    _page_groups, null_items = group_pages(chunk_pairs)
    if null_items:
        # Embed all null-page texts in ONE batched call before touching the
        # session — never run N serial embeds while a session is open.
        null_vectors = embed_fn([text for text, _ in null_items])
        await session.execute(
            delete(AITAChunk).where(
                AITAChunk.document_id == document_id,
                AITAChunk.page_number.is_(None),
            )
        )
        for (text, meta), vector in zip(null_items, null_vectors, strict=True):
            session.add(
                AITAChunk(
                    content=text,
                    embedding=vector,
                    page_number=None,
                    section_path=meta.get("section_path") or None,
                    chunk_type=meta.get("chunk_type") or "body",
                    figure_id=meta.get("figure_id"),
                    document_id=document_id,
                )
            )
    document = await session.get(AITADocument, document_id)
    document.content = doc_content
    document.embedding = doc_embedding
    if page_count is not None:
        document.page_count = page_count
    document.updated_at = datetime.now(UTC)
    document.status = DocumentStatus.ready()
