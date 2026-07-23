from __future__ import annotations

"""Top-level retrieval entry point for the pgvector path.

retrieve_for_question() is the single function called from orchestrator.py
when USE_PGVECTOR_RETRIEVAL=true. It replaces the batch_lookup_terms() loop.

Call chain:
    AITAHybridSearchRetriever.hybrid_search(combined_query)
    → AITARerankerService.rerank(original_question)
    → apply_store_biases(weight_overrides)
    → pack_context(token_budget)
    → (snippets, diagnostics)
"""

import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config.contracts import BundleSnippet
from .hybrid_search import AITAHybridSearchRetriever
from .reranker import AITARerankerService
from .store_bias import apply_store_biases
from .context_packer import pack_context

log = logging.getLogger(__name__)


async def retrieve_for_question(
    query: str,
    keywords: list[str],
    search_space_id: int,
    db_session: AsyncSession,
    weight_overrides: Optional[dict[str, float]] = None,
    top_k: int = 20,
    token_budget: int = 6000,
    citation_label: Optional[str] = None,
) -> tuple[list[BundleSnippet], dict[str, Any]]:
    """Run the full pgvector retrieval pipeline for a student question.

    Args:
        query: The original (normalized) student question.
        keywords: Top keywords from extract_and_filter_keywords() — appended to query
                  as hints, NOT used as standalone search targets.
        search_space_id: The Course.id for this class (from workspace lookup).
        db_session: Async SQLAlchemy session.
        weight_overrides: Per-material-kind score biases from workspace config.
        top_k: Number of chunks to return after reranking + bias.
        token_budget: Max tokens for context window.
        citation_label: Override for citation label text.

    Returns:
        (snippets, diagnostics) — snippets are BundleSnippet objects ready for
        solve_with_bundle(); diagnostics is a dict for logging/debugging.
    """
    # Build combined query: original question + keywords as hints
    # Keywords are appended (not substituted) so bad keyword extraction
    # doesn't kill retrieval — the original question anchors semantic search.
    combined_parts = [query.strip()]
    if keywords:
        combined_parts.append(" ".join(keywords[:6]))
    combined_query = " ".join(combined_parts).strip()

    # --- Hybrid search (pgvector CTE + PostgreSQL FTS + RRF) ---
    retriever = AITAHybridSearchRetriever(db_session, search_space_id)
    raw_chunks = await retriever.hybrid_search(
        query_text=combined_query,
        top_k=top_k * 3,  # Fetch extra for reranker to re-order
    )

    sem_hits = sum(1 for c in raw_chunks if c.get("score", 0) > 0)

    # --- Optional reranking ---
    reranker = AITARerankerService.get_instance()
    reranked = reranker.rerank(query, raw_chunks)  # Uses original question, not combined

    # --- Store kind bias (textbook +0.12, slides +0.06, etc.) ---
    biased = apply_store_biases(reranked, weight_overrides)

    # --- Context packing (token budget → BundleSnippet list) ---
    top_chunks = biased[:top_k]
    snippets = pack_context(top_chunks, token_budget=token_budget, citation_label=citation_label)

    diagnostics: dict[str, Any] = {
        "hit_count_raw": len(raw_chunks),
        "hit_count_sem": sem_hits,
        "hit_count_after_rerank": len(reranked),
        "chunks_in_context": len(snippets),
        "search_space_id": search_space_id,
        "combined_query": combined_query,
    }

    log.debug(
        "retrieve_for_question: space=%d raw=%d reranked=%d context=%d",
        search_space_id, len(raw_chunks), len(reranked), len(snippets),
    )

    return snippets, diagnostics
