from __future__ import annotations

"""AI-TA hybrid search retriever using pgvector + PostgreSQL FTS with RRF.

Ported from SurfSense's ChucksHybridSearchRetriever with AI-TA adaptations:
- Returns chunk-level dicts (not document-grouped) to preserve page_number for citations.
- Filters by search_space_id (= class) and optional material_kind.
- Embeds query using OpenAI directly (not chonkie).
- Carries material_kind, page_number, section_path through results for citation building.
"""

import logging
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..db import AITAChunk, AITADocument
from ..indexing.document_embedder import embed_text

log = logging.getLogger(__name__)

# RRF constant (same as SurfSense and standard literature)
_RRF_K = 60


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
        query_embedding = embed_text(query_text)

        # How many candidate results to pull from each search before RRF fusion
        n_results = top_k * 5

        tsvector = func.to_tsvector("english", AITAChunk.content)
        tsquery = func.plainto_tsquery("english", query_text)

        # Base filter conditions (search space + optional material kind)
        base_conditions = [AITADocument.search_space_id == self.search_space_id]
        if material_kind:
            base_conditions.append(AITADocument.material_kind == material_kind)
        # Only return chunks from fully-indexed documents
        base_conditions.append(
            AITADocument.status["state"].astext == "ready"
        )

        # CTE 1: Semantic search (pgvector cosine distance, ascending = closer)
        semantic_cte = (
            select(
                AITAChunk.id,
                func.rank()
                .over(order_by=AITAChunk.embedding.op("<=>")(query_embedding))
                .label("rank"),
            )
            .join(AITADocument, AITAChunk.document_id == AITADocument.id)
            .where(*base_conditions)
            .order_by(AITAChunk.embedding.op("<=>")(query_embedding))
            .limit(n_results)
            .cte("semantic_search")
        )

        # CTE 2: Keyword search (PostgreSQL tsvector BM25-style ranking)
        keyword_cte = (
            select(
                AITAChunk.id,
                func.rank()
                .over(order_by=func.ts_rank_cd(tsvector, tsquery).desc())
                .label("rank"),
            )
            .join(AITADocument, AITAChunk.document_id == AITADocument.id)
            .where(*base_conditions)
            .where(tsvector.op("@@")(tsquery))
            .order_by(func.ts_rank_cd(tsvector, tsquery).desc())
            .limit(n_results)
            .cte("keyword_search")
        )

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

        result = await self.db_session.execute(final_query)
        rows = result.all()

        if not rows:
            return []

        chunks_out = []
        for chunk, score in rows:
            doc = chunk.document
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
            })

        return chunks_out
