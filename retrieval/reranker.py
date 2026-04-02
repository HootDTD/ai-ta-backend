from __future__ import annotations

"""Optional reranking step for AI-TA retrieval.

Ported from SurfSense's RerankerService with AI-TA adaptations:
- Accepts chunk-level dicts (not document-grouped) from hybrid_search.py
- Preserves page_number, section_path, material_kind through reranking.
- Falls back to original RRF order when reranker is not configured.
- Enabled via RERANKERS_ENABLED=true + RERANKER_MODEL env vars.
"""

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

_reranker_instance = None
_reranker_loaded = False


def _get_reranker():
    """Lazily load the reranker model. Returns None if not configured."""
    global _reranker_instance, _reranker_loaded
    if _reranker_loaded:
        return _reranker_instance

    _reranker_loaded = True
    from config.settings import rerankers_enabled, get_reranker_model
    if not rerankers_enabled():
        return None

    try:
        from rerankers import Reranker
        model_name = get_reranker_model()
        _reranker_instance = Reranker(model_name)
        log.info("Reranker loaded: %s", model_name)
    except Exception as e:
        log.warning("Failed to load reranker (%s). Retrieval will use RRF order only.", e)
        _reranker_instance = None

    return _reranker_instance


class AITARerankerService:
    """Reranks hybrid search chunks by cross-encoder relevance score."""

    def __init__(self, reranker=None) -> None:
        self._reranker = reranker

    @classmethod
    def get_instance(cls) -> "AITARerankerService":
        return cls(reranker=_get_reranker())

    def rerank(
        self,
        query: str,
        chunks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Rerank chunks by cross-encoder score.

        Args:
            query: The original student question (not the expanded query).
            chunks: List of chunk dicts from AITAHybridSearchRetriever.hybrid_search().

        Returns:
            Reranked list of chunk dicts with updated 'score' field.
            Falls back to original RRF order on any error.
        """
        if not self._reranker or not chunks:
            return chunks

        try:
            from rerankers import Document as RerankerDocument

            reranker_docs = [
                RerankerDocument(
                    text=chunk.get("content", ""),
                    doc_id=chunk.get("chunk_id", i),
                    metadata={"original_index": i, "rrf_score": chunk.get("score", 0.0)},
                )
                for i, chunk in enumerate(chunks)
            ]

            results = self._reranker.rank(query=query, docs=reranker_docs)

            reranked: list[dict] = []
            for result in results.results:
                orig_idx = result.document.metadata.get("original_index")
                if orig_idx is not None and 0 <= orig_idx < len(chunks):
                    chunk_copy = dict(chunks[orig_idx])
                    chunk_copy["score"] = float(result.score)
                    reranked.append(chunk_copy)

            return reranked if reranked else chunks

        except Exception as e:
            log.error("Reranking failed: %s. Using RRF order.", e)
            return chunks
