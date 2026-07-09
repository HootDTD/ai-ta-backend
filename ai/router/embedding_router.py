from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, Sequence

import numpy as np


class Embedder(Protocol):
    async def embed(self, text: str) -> np.ndarray: ...


@dataclass(frozen=True)
class Stage1Decision:
    accepted: bool
    route: str | None
    confidence: float
    margin: float
    top1_score: float
    top2_score: float
    top2_route: str | None


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v if n == 0 else v / n


class EmbeddingRouter:
    def __init__(
        self,
        embedder: Embedder,
        utterance_matrix: np.ndarray,
        utterance_routes: Sequence[str],
        margin_threshold: float,
        score_floor: float,
    ) -> None:
        self._embedder = embedder
        self._mat = utterance_matrix  # (N, D), L2-normalized
        self._labels = list(utterance_routes)  # length N
        self._margin = margin_threshold
        self._floor = score_floor

    @classmethod
    async def build(
        cls,
        embedder: Embedder,
        seeds: dict[str, list[str]],
        *,
        margin_threshold: float | None = None,
        score_floor: float | None = None,
    ) -> "EmbeddingRouter":
        margin_threshold = (
            margin_threshold
            if margin_threshold is not None
            else float(os.environ.get("ROUTER_STAGE1_MARGIN_THRESHOLD", "0.10"))
        )
        score_floor = (
            score_floor
            if score_floor is not None
            else float(os.environ.get("ROUTER_STAGE1_SCORE_FLOOR", "0.55"))
        )
        rows: list[np.ndarray] = []
        labels: list[str] = []
        for route_name, utterances in seeds.items():
            for u in utterances:
                v = await embedder.embed(u)
                rows.append(_normalize(v))
                labels.append(route_name)
        mat = np.vstack(rows)
        return cls(embedder, mat, labels, margin_threshold, score_floor)

    async def classify(self, query: str) -> Stage1Decision:
        q = _normalize(await self._embedder.embed(query))
        sims = self._mat @ q  # cosine similarity
        # Per-route best similarity:
        best: dict[str, float] = {}
        for label, sim in zip(self._labels, sims, strict=True):
            if sim > best.get(label, -1.0):
                best[label] = float(sim)
        ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
        top_route, top_score = ranked[0]
        second_route, second_score = ranked[1] if len(ranked) > 1 else (None, 0.0)
        margin = top_score - second_score
        accepted = (margin >= self._margin) and (top_score >= self._floor)
        return Stage1Decision(
            accepted=accepted,
            route=top_route if accepted else None,
            confidence=top_score if accepted else 0.0,
            margin=margin,
            top1_score=top_score,
            top2_score=second_score,
            top2_route=second_route,
        )
