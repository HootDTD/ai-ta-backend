from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import numpy as np

from ai.router.embedding_router import EmbeddingRouter, Stage1Decision
from ai.router.llm_router import LLMRouter, Stage2Decision


@dataclass(frozen=True)
class RouteDecision:
    final_route: str
    retrieval_mode: str
    confidence: float
    stage1: Stage1Decision
    stage2: Stage2Decision | None
    stage2_invoked: bool
    reason: str

    @property
    def is_clarify(self) -> bool:
        return self.final_route == "clarify"


def _stage1_retrieval_mode(
    *, query_vec: Optional[np.ndarray], topic_centroid: Optional[np.ndarray], has_cache: bool
) -> str:
    if not has_cache:
        return "FRESH"
    if query_vec is None or topic_centroid is None:
        return "FRESH"
    qn = query_vec / (np.linalg.norm(query_vec) or 1.0)
    cn = topic_centroid / (np.linalg.norm(topic_centroid) or 1.0)
    sim = float(qn @ cn)
    if sim >= 0.85:
        return "NONE"
    if sim >= 0.65:
        return "AUGMENT"
    return "FRESH"


async def route(
    *,
    query: str,
    stage1: EmbeddingRouter,
    stage2: LLMRouter,
    recent_turns: list[dict],
    cached_titles: list[str],
    topic_centroid: Optional[np.ndarray],
    has_cache: bool,
    query_vec: Optional[np.ndarray] = None,
) -> RouteDecision:
    s1 = await stage1.classify(query)
    if s1.accepted:
        mode = _stage1_retrieval_mode(
            query_vec=query_vec, topic_centroid=topic_centroid, has_cache=has_cache
        )
        return RouteDecision(
            final_route=s1.route or "clarify",
            retrieval_mode=mode,
            confidence=s1.confidence,
            stage1=s1,
            stage2=None,
            stage2_invoked=False,
            reason=f"stage1 accept margin={s1.margin:.3f}",
        )
    s2 = await stage2.classify(
        query=query, recent_turns=recent_turns, cached_titles=cached_titles
    )
    return RouteDecision(
        final_route=s2.route,
        retrieval_mode=s2.retrieval_mode,
        confidence=s2.confidence,
        stage1=s1,
        stage2=s2,
        stage2_invoked=True,
        reason=s2.reason,
    )
