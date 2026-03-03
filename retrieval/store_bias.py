from __future__ import annotations

"""Post-rerank store kind bias adjustment.

Preserves AI-TA's educationally meaningful material-kind weights
(textbook=0.12, slides=0.06, etc.) as a score boost applied AFTER reranking.

Applying bias post-rerank is correct:
- Reranker first orders chunks by semantic relevance to the query.
- Then textbook chunks get a +0.12 score boost relative to their reranked position.
- Final sort by 'final_score' ensures authoritative sources surface correctly.
"""

from typing import Any, Optional

from ..config.weights import get_env_weight


def apply_store_biases(
    chunks: list[dict[str, Any]],
    weight_overrides: Optional[dict[str, float]] = None,
) -> list[dict[str, Any]]:
    """Add per-material-kind score bias and re-sort chunks.

    Args:
        chunks: Chunk dicts from hybrid_search or reranker (with 'score' and 'material_kind').
        weight_overrides: Per-workspace overrides for material kind weights.
                          From workspace.weight_overrides dict.

    Returns:
        Chunks sorted descending by 'final_score' (score + kind bias).
    """
    for chunk in chunks:
        kind = chunk.get("material_kind") or "other"
        bias = get_env_weight(kind)
        if weight_overrides and kind in weight_overrides:
            bias = float(weight_overrides[kind])
        chunk["final_score"] = chunk.get("score", 0.0) + bias

    return sorted(chunks, key=lambda c: c["final_score"], reverse=True)
