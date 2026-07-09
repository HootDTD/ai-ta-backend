from __future__ import annotations

"""AI-TA hybrid search retriever using pgvector + PostgreSQL FTS with RRF.

Ported from SurfSense's ChucksHybridSearchRetriever with AI-TA adaptations:
- Returns chunk-level dicts (not document-grouped) to preserve page_number for citations.
- Filters by search_space_id (= class) and optional material_kind.
- Embeds query using OpenAI directly (not chonkie).
- Carries material_kind, page_number, section_path through results for citation building.
"""

import logging
import os
import time
from typing import Optional

from sqlalchemy import Integer, cast, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from pgvector.sqlalchemy import HALFVEC

from database.models import AITAChunk, AITADocument, EMBEDDING_DIM
from indexing.document_embedder import embed_text
from .document_visibility import active_document_conditions, build_chunk_metadata

log = logging.getLogger(__name__)

# RRF constant (same as SurfSense and standard literature)
_RRF_K = 60

# Allowed values for hnsw.iterative_scan (pgvector >= 0.8). Anything else
# coming from the env is rejected — these strings are interpolated into SQL.
_ITERATIVE_SCAN_MODES = {"relaxed_order", "strict_order"}
_ITERATIVE_SCAN_OFF = {"", "off", "0", "false", "disabled", "none"}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Parse an int env var, clamped to [lo, hi]; fall back to default."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    return max(lo, min(hi, val))


def _iterative_scan_statements() -> list[str]:
    """``SET LOCAL`` statements that engage the HNSW index for the semantic arm.

    Without these, the document-visibility pre-filter blocks the HNSW index
    and the semantic arm brute-force detoasts every embedding in the class —
    measured 3,414 ms cold / 1,534 ms warm / 112 ms hot on the largest class
    (5,844 chunks, ~27,560 TOAST buffer pages). ``hnsw.iterative_scan``
    (pgvector >= 0.8) lets the index scan keep iterating until the post-filter
    LIMIT is satisfied, touching only the visited vectors.

    ``relaxed_order`` emits candidates slightly out of distance order, but the
    outer ``rank() OVER (ORDER BY distance)`` window re-sorts the materialized
    distances, so only top-N *membership* is approximate — ranking among the
    returned candidates stays exact. Recall is guarded by
    scripts/eval_iterative_scan_recall.py (3 query types, top-20 overlap).

    SET LOCAL is transaction-scoped: the caller must execute these on the same
    session (and therefore the same autobegun transaction) as the search query.
    They reset at commit/rollback, so nothing leaks through the asyncpg pool.

    Env knobs:
      HNSW_ITERATIVE_SCAN  relaxed_order (default) | strict_order | off
      HNSW_EF_SEARCH       initial candidate count, 1..1000 (default 300 —
                           sized to the n_results=300 candidate LIMIT)
      HNSW_MAX_SCAN_TUPLES iteration budget, 1000..1000000 (default 20000,
                           pgvector's own default; raise if recall dips)
    """
    mode = (os.getenv("HNSW_ITERATIVE_SCAN", "relaxed_order") or "").strip().lower()
    if mode in _ITERATIVE_SCAN_OFF:
        return []
    if mode not in _ITERATIVE_SCAN_MODES:
        log.warning("Invalid HNSW_ITERATIVE_SCAN=%r; iterative scan disabled", mode)
        return []
    ef_search = _env_int("HNSW_EF_SEARCH", 300, 1, 1000)
    max_scan = _env_int("HNSW_MAX_SCAN_TUPLES", 20000, 1000, 1_000_000)
    return [
        f"SET LOCAL hnsw.iterative_scan = {mode}",
        f"SET LOCAL hnsw.ef_search = {ef_search}",
        f"SET LOCAL hnsw.max_scan_tuples = {max_scan}",
    ]


def _halfvec_cosine_distance(query_embedding):
    """Cosine distance computed in ``halfvec(EMBEDDING_DIM)``.

    The production vector index is ``idx_aita_chunks_embedding_hnsw`` on
    ``(embedding::halfvec(3072)) halfvec_cosine_ops``. Casting BOTH operands to
    halfvec makes the query expression match that index and, more importantly,
    runs the distance math in 16-bit (6 KB/vector) instead of 32-bit
    (12 KB/vector) — measured 3,529 ms -> 107 ms on the largest class, with
    identical ranking order. RRF fuses on rank, not raw distance, so fusion is
    unaffected.
    """
    return AITAChunk.embedding.cast(HALFVEC(EMBEDDING_DIM)).op("<=>")(
        cast(query_embedding, HALFVEC(EMBEDDING_DIM))
    )


def _build_semantic_cte(query_embedding, visible_doc_ids, n_results: int):
    """Semantic candidates CTE filtered by a materialized visible-doc-id array.

    Two design choices, both proven on the live DB with EXPLAIN (ANALYZE):

    1. LIMIT inside an inner subquery. The old shape computed
       ``rank() OVER (ORDER BY <distance>)`` in the same scope as the LIMIT;
       window functions evaluate before LIMIT, so Postgres computed the halfvec
       distance for every candidate. Moving the LIMIT into an inner subquery
       makes the plan ``Limit -> Index Scan`` (or ``Limit -> Sort`` when the
       index is off).

    2. The filter is a CHUNK-LOCAL ``document_id = ANY(:ids)`` over a
       materialized id array, NOT a join to ``aita_documents`` and NOT an
       ``IN (subquery)``. This is the only form that lets the HNSW index engage
       under ``hnsw.iterative_scan`` (pgvector >= 0.8): the planner pushes the
       array membership test into the index scan and iterates in distance order
       until n_results post-filter matches are found. Both the join form and
       the ``IN (subquery)`` form make the planner fall back to a brute-force
       ``Sort`` that detoasts every embedding in the class (measured 3,414 ms
       cold / 27,560 buffer pages on the largest class) — engaging the index
       drops that to ~750 ms / 5,783 pages. The caller (:meth:`hybrid_search`)
       resolves ``visible_doc_ids`` from the document-visibility conditions in a
       separate cheap query, then issues the iterative-scan SET LOCALs in the
       same transaction. With HNSW_ITERATIVE_SCAN=off the scan stays exact
       (recall 100%) at brute-force cost. See P3 in
       docs/superpowers/specs/2026-06-09-halfvec-retrieval-speedup-design.md.
    """
    distance = _halfvec_cosine_distance(query_embedding)
    # ``= ANY(CAST(:ids AS integer[]))`` — one bound array param. This exact
    # materialized-array form is what lets the HNSW index engage (verified via
    # EXPLAIN); a join or an ``IN (subquery)`` does not.
    doc_filter = AITAChunk.document_id == func.any(
        cast(list(visible_doc_ids), ARRAY(Integer))
    )
    inner = (
        select(AITAChunk.id.label("id"), distance.label("distance"))
        .where(doc_filter)
        .order_by(distance)
        .limit(n_results)
        .subquery("semantic_candidates")
    )
    return (
        select(
            inner.c.id,
            func.rank().over(order_by=inner.c.distance).label("rank"),
        )
        .cte("semantic_search")
    )


def _build_keyword_cte(tsvector, tsquery, base_conditions, n_results: int):
    """Index-friendly FTS candidates CTE (same inner-LIMIT-then-rank shape)."""
    text_rank = func.ts_rank_cd(tsvector, tsquery)
    inner = (
        select(AITAChunk.id.label("id"), text_rank.label("text_rank"))
        .join(AITADocument, AITAChunk.document_id == AITADocument.id)
        .where(*base_conditions)
        .where(tsvector.op("@@")(tsquery))
        .order_by(text_rank.desc())
        .limit(n_results)
        .subquery("keyword_candidates")
    )
    return (
        select(
            inner.c.id,
            func.rank().over(order_by=inner.c.text_rank.desc()).label("rank"),
        )
        .cte("keyword_search")
    )


class AITAHybridSearchRetriever:
    """Hybrid search over aita_chunks using pgvector cosine + PostgreSQL FTS, fused with RRF."""

    def __init__(self, db_session: AsyncSession, search_space_id: int) -> None:
        self.db_session = db_session
        self.search_space_id = search_space_id

    async def hybrid_search(
        self,
        query_text: str,
        top_k: int = 60,
        material_kind: Optional[str] = None,
    ) -> list[dict]:
        """Run hybrid RRF search and return chunk-level result dicts.

        Returns list of dicts with keys:
            chunk_id, content, score, page_number, section_path, chunk_type,
            figure_id, document_id, doc_title, material_kind
        """
        _t_embed = time.perf_counter()
        query_embedding = embed_text(query_text)
        log.info("[timing] embed=%.3fs", time.perf_counter() - _t_embed)

        # How many candidate results to pull from each search before RRF fusion
        n_results = top_k * 5

        tsvector = func.to_tsvector("english", AITAChunk.content)
        tsquery = func.plainto_tsquery("english", query_text)

        # Base filter conditions (search space + optional material kind)
        base_conditions = active_document_conditions(self.search_space_id)
        if material_kind:
            base_conditions.append(AITADocument.material_kind == material_kind)

        # Resolve the visible document ids once (cheap index scan on
        # aita_documents). The semantic arm filters chunks by this materialized
        # id array so the HNSW index can engage under hnsw.iterative_scan — a
        # join or IN (subquery) makes the planner brute-force the scan instead.
        visible_doc_rows = await self.db_session.execute(
            select(AITADocument.id).where(*base_conditions)
        )
        visible_doc_ids = [row[0] for row in visible_doc_rows.all()]
        if not visible_doc_ids:
            return []

        # CTE 1: Semantic search (HNSW-friendly: chunk-local doc filter, LIMIT
        # inside, rank outside)
        semantic_cte = _build_semantic_cte(query_embedding, visible_doc_ids, n_results)

        # CTE 2: Keyword search (PostgreSQL FTS, joins aita_documents — FTS is
        # GIN-indexed and not the cold-cache bottleneck, so it stays as-is)
        keyword_cte = _build_keyword_cte(tsvector, tsquery, base_conditions, n_results)

        # Final query: FULL OUTER JOIN + RRF scoring
        # score = 1/(k + sem_rank) + 1/(k + kw_rank)
        final_query = (
            select(
                AITAChunk,
                (
                    func.coalesce(1.0 / (_RRF_K + semantic_cte.c.rank), 0.0)
                    + func.coalesce(1.0 / (_RRF_K + keyword_cte.c.rank), 0.0)
                ).label("score"),
            )
            .select_from(
                semantic_cte.outerjoin(
                    keyword_cte,
                    semantic_cte.c.id == keyword_cte.c.id,
                    full=True,
                )
            )
            .join(
                AITAChunk,
                AITAChunk.id == func.coalesce(semantic_cte.c.id, keyword_cte.c.id),
            )
            .options(joinedload(AITAChunk.document))
            .order_by(text("score DESC"))
            .limit(top_k)
        )

        # Engage the HNSW index for the semantic arm. SET LOCAL is
        # transaction-scoped: the AsyncSession autobegins on the first execute,
        # so these GUCs cover final_query below and reset at commit/rollback.
        for stmt in _iterative_scan_statements():
            await self.db_session.execute(text(stmt))

        _t_search = time.perf_counter()
        result = await self.db_session.execute(final_query)
        log.info("[timing] hybrid_search_sql=%.3fs", time.perf_counter() - _t_search)
        rows = result.all()

        if not rows:
            return []

        chunks_out = []
        for chunk, score in rows:
            doc = chunk.document
            doc_meta = dict(getattr(doc, "document_metadata", None) or {})
            chunk_meta = build_chunk_metadata(doc_meta, chunk.page_number)
            chunk_meta.update(
                {
                    "document_id": doc.id if doc else None,
                    "material_kind": doc.material_kind if doc else "other",
                    "kind": chunk_meta.get("kind") or (doc.material_kind if doc else "other"),
                }
            )
            chunks_out.append({
                "chunk_id": chunk.id,
                "content": chunk.content,
                "score": float(score),
                "page_number": chunk.page_number,
                "section_path": chunk.section_path,
                "chunk_type": chunk.chunk_type or "body",
                "figure_id": chunk.figure_id,
                "document_id": doc.id if doc else None,
                "doc_title": doc.title if doc else "",
                "material_kind": doc.material_kind if doc else "other",
                "source_path": chunk_meta.get("source_pdf") or "",
                "week": chunk_meta.get("week"),
                "teacher_upload_id": chunk_meta.get("teacher_upload_id"),
                "ocr_provider": chunk_meta.get("ocr_provider"),
                "ocr_confidence": chunk_meta.get("ocr_confidence"),
                "page_asset": chunk_meta.get("page_asset"),
                "raw_latex": chunk_meta.get("raw_latex"),
                "metadata": chunk_meta,
            })

        return chunks_out
