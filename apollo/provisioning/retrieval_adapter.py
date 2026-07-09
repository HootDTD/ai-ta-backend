"""WU-3B2g+ — the course-scoped retrieval-grounding adapter for auto-provisioning.

``make_course_retrieve_fn(db, *, search_space_id, top_k)`` returns the async
``retrieve(question)`` closure that ``find_or_generate`` / ``validate_pair`` call.
It runs the existing hybrid search (pgvector + FTS over ``aita_chunks``) scoped to
``search_space_id`` and maps each chunk dict into an immutable ``GroundingSpan`` —
real course grounding for the generator and the stage-3 faithfulness judge
(replacing the v1 no-span stub that rejected every candidate).

v1 always RAG-generates: ``carries_solution=False`` (printed-solution detection is
a follow-up). Course scoping is enforced INSIDE ``AITAHybridSearchRetriever`` via
``active_document_conditions`` — grounding never crosses courses. Empty retrieval
is NOT an error (the faithfulness judge honestly rejects); a real DB/embedding
failure propagates to the orchestrator's terminal-error handler, never masked as
empty grounding.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.provisioning.scrape import chunk_content_hash
from apollo.provisioning.solution import GroundingSpan
from retrieval.hybrid_search import AITAHybridSearchRetriever

__all__ = ["make_course_retrieve_fn", "DEFAULT_GROUNDING_TOP_K"]

_LOG = logging.getLogger(__name__)

# Small bound on grounding spans (token/cost control; the MeteredChat ceiling is
# the backstop). RRF fusion already ranks; reranking is a documented follow-up.
DEFAULT_GROUNDING_TOP_K = 6


def make_course_retrieve_fn(
    db: AsyncSession,
    *,
    search_space_id: int,
    top_k: int = DEFAULT_GROUNDING_TOP_K,
) -> Callable[[Any], Awaitable[tuple[GroundingSpan, ...]]]:
    """Build the course-scoped ``retrieve(question)`` adapter bound to ``db`` +
    ``search_space_id``. The returned closure preserves the ``retrieve_fn(question)``
    call contract that ``find_or_generate``/``validate_pair`` depend on."""

    async def retrieve(question: Any) -> tuple[GroundingSpan, ...]:
        query_text = getattr(question, "problem_text", "") or ""
        retriever = AITAHybridSearchRetriever(db, search_space_id)
        rows: Sequence[dict] = await retriever.hybrid_search(query_text, top_k=top_k)
        spans = tuple(
            GroundingSpan(
                text=content,
                document_id=row.get("document_id"),
                page=row.get("page_number"),
                chunk_content_hash=chunk_content_hash(content),
                carries_solution=False,
            )
            for row in rows
            # Skip a row with no usable text rather than KeyError on row["content"]:
            # an unexpected exception here aborts the WHOLE document; a missing
            # chunk is a per-span no-op, not a doc failure.
            if (content := (row.get("content") or "").strip())
        )
        if not spans:
            _LOG.info(
                "provisioning_retrieval_empty",
                extra={
                    "event": "provisioning_retrieval_empty",
                    "search_space_id": search_space_id,
                    "chunk_content_hash": getattr(question, "chunk_content_hash", None),
                },
            )
        return spans

    return retrieve
