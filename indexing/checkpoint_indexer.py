from __future__ import annotations

"""Checkpointed embed-and-persist for long indexing jobs.

Splits a document's chunks into per-page work units, embeds each page-batch with
a single batched OpenAI call, and commits each batch in its own short-lived DB
session while advancing a resume pointer. No DB session is ever held while an
OpenAI call is in flight — the fix for the connection-reap failure on long jobs.
"""

import os
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass

from sqlalchemy import delete

from database.models import AITAChunk
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
