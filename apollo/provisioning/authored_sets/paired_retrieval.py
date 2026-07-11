"""Doc-scoped grounding for authored sets (WU-AAS).

The returned ``retrieve(question)`` grounds against ONLY the paired solution doc
(when one was provided): label match first, then doc-scoped semantic top-k. Only
a label match is a confirmed printed solution for THIS problem, so those spans
are marked ``carries_solution=True`` (``find_or_generate`` takes its extract
branch); the semantic top-k fallback is an unconfirmed guess at relevant
context, so those spans are marked ``carries_solution=False`` (extract branch
skipped, but the spans still ride along as generation context). When no
solution document is paired (``solution_document_id is None``), the retrieve fn
returns no spans at all so the caller falls through to solution generation.
This module deliberately filters by ``aita_chunks.document_id`` and never uses
the student-RAG document visibility gate.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.provisioning.authored_sets.label_match import (
    SolutionChunk,
    extract_problem_label,
    match_solution_label,
)
from apollo.provisioning.scrape import chunk_content_hash
from apollo.provisioning.solution import GroundingSpan

DEFAULT_PAIRED_TOP_K = 6


async def load_solution_chunks(
    db: AsyncSession,
    *,
    solution_document_id: int | None,
) -> list[SolutionChunk]:
    """Load all chunks for one paired solution document in stable order.

    ``None`` (no solution doc paired) returns ``[]`` without a query."""
    if solution_document_id is None:
        return []
    from database.models import AITAChunk

    rows = (
        await db.execute(
            select(AITAChunk.id, AITAChunk.content, AITAChunk.page_number)
            .where(AITAChunk.document_id == solution_document_id)
            .order_by(AITAChunk.id.asc())
        )
    ).all()
    return [(int(row.id), row.content or "", row.page_number) for row in rows]


async def chunk_ocr_confidence(
    db: AsyncSession,
    *,
    document_id: int | None,
) -> dict[int | None, float | None]:
    """Return page_number -> OCR confidence from document metadata page_debug.

    ``None`` (no document, e.g. no solution paired) returns ``{}`` without a query."""
    if document_id is None:
        return {}
    from database.models import AITADocument

    doc = await db.get(AITADocument, document_id)
    meta = dict(getattr(doc, "document_metadata", None) or {})
    page_conf: dict[int | None, float | None] = {}
    for entry in meta.get("page_debug") or []:
        if not isinstance(entry, dict):
            continue
        try:
            page = int(entry.get("page"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        confidence = entry.get("ocr_confidence")
        page_conf[page] = float(confidence) if confidence is not None else None
    return page_conf


async def _doc_scoped_semantic(
    db: AsyncSession,
    solution_document_id: int,
    query_text: str,
    top_k: int,
) -> list[SolutionChunk]:
    """Semantic top-k over exactly one solution document's chunks."""
    from database.models import AITAChunk
    from indexing.document_embedder import embed_text
    from retrieval.hybrid_search import _halfvec_cosine_distance

    query_embedding = embed_text(query_text)
    distance = _halfvec_cosine_distance(query_embedding)
    rows = (
        await db.execute(
            select(AITAChunk.id, AITAChunk.content, AITAChunk.page_number)
            .where(AITAChunk.document_id == solution_document_id)
            .order_by(distance)
            .limit(top_k)
        )
    ).all()
    return [(int(row.id), row.content or "", row.page_number) for row in rows]


def _spans_from_chunks(
    chunks: Sequence[SolutionChunk],
    *,
    solution_document_id: int,
    page_conf: dict[int | None, float | None],
    carries_solution: bool,
) -> tuple[tuple[GroundingSpan, ...], float | None]:
    """Build grounding spans from retrieved chunks. ``carries_solution`` is the
    caller's honest signal: True only for a confirmed label match (a printed
    solution for THIS problem), False for the semantic top-k fallback (an
    unconfirmed guess that should still ride along as generation context, not
    be treated as an extractable worked solution)."""
    spans = tuple(
        GroundingSpan(
            text=content,
            document_id=solution_document_id,
            page=page,
            chunk_content_hash=chunk_content_hash(content),
            carries_solution=carries_solution,
        )
        for (_chunk_id, content, page) in chunks
        if (content or "").strip()
    )
    confidences = [
        conf for (_chunk_id, _content, page) in chunks if (conf := page_conf.get(page)) is not None
    ]
    min_conf = min(confidences) if confidences else None
    return spans, min_conf


def make_paired_solution_retrieve_fn(
    db: AsyncSession | None,
    *,
    solution_document_id: int | None,
    label_index: dict[str, list[SolutionChunk]],
    page_conf: dict[int | None, float | None],
    top_k: int = DEFAULT_PAIRED_TOP_K,
) -> Callable[[Any], Awaitable[tuple[GroundingSpan, ...]]]:
    """Build a retrieve_fn for ``find_or_generate``.

    The returned callable exposes ``last_min_conf`` and ``last_match_method`` for
    the most recent grounding. Empty results leave both as ``None``. When
    ``solution_document_id`` is ``None`` (no solution doc paired), the returned
    fn always yields no spans so ``find_or_generate`` falls through to its
    generate branch.
    """

    async def retrieve(question: Any) -> tuple[GroundingSpan, ...]:
        if solution_document_id is None:
            return ()

        label = extract_problem_label(question)
        matched = match_solution_label(label, label_index)
        if matched is not None:
            spans, min_conf = _spans_from_chunks(
                matched,
                solution_document_id=solution_document_id,
                page_conf=page_conf,
                carries_solution=True,
            )
            if spans:
                retrieve.last_min_conf = min_conf  # type: ignore[attr-defined]
                retrieve.last_match_method = "label"  # type: ignore[attr-defined]
                return spans

        query_text = getattr(question, "problem_text", "") or ""
        hits = await _doc_scoped_semantic(db, solution_document_id, query_text, top_k)  # type: ignore[arg-type]
        spans, min_conf = _spans_from_chunks(
            hits,
            solution_document_id=solution_document_id,
            page_conf=page_conf,
            carries_solution=False,
        )
        retrieve.last_min_conf = min_conf if spans else None  # type: ignore[attr-defined]
        retrieve.last_match_method = "retrieval" if spans else None  # type: ignore[attr-defined]
        return spans

    retrieve.last_min_conf = None  # type: ignore[attr-defined]
    retrieve.last_match_method = None  # type: ignore[attr-defined]
    return retrieve
