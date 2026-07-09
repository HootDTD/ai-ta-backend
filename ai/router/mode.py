"""Retrieval-mode decision for the orchestrator — v1, LLM-only.

Decides per turn whether the answer pipeline needs a FRESH retrieval, a small
AUGMENT top-up merged with the session's cached bundle, or NONE (answer from
the cached bundle alone, e.g. a check-your-understanding reply).

v1 deliberately skips the embedding stage (``EmbeddingRouter``): its seed
utterances are subject-specific and centroid thresholds are untuned, while the
gpt-4o-mini structured call is subject-agnostic and sees conversation context —
the only reliable signal for bare CYU replies like "B". Telemetry rows
(``chat_router_decisions``) collect the data needed to add an embedding
fast-path later.

Failure asymmetry: misrouting toward FRESH only costs efficiency; misrouting
toward NONE can cost correctness. Every error path here therefore returns
FRESH, and NONE/AUGMENT additionally require the model's confidence to clear
``ROUTER_MIN_CONFIDENCE``.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from ai.router.llm_router import LLMRouter

log = logging.getLogger(__name__)

VALID_MODES = {"NONE", "AUGMENT", "FRESH"}


def _min_confidence() -> float:
    try:
        return float(os.getenv("ROUTER_MIN_CONFIDENCE", "0.5"))
    except ValueError:
        return 0.5


@dataclass(frozen=True)
class ModeDecision:
    mode: str  # NONE | AUGMENT | FRESH
    route: str  # specialist route from the LLM — telemetry only in v1
    confidence: float
    reason: str
    llm_invoked: bool
    latency_ms: int


def _fresh(reason: str, *, llm_invoked: bool = False, latency_ms: int = 0) -> ModeDecision:
    return ModeDecision(
        mode="FRESH",
        route="",
        confidence=1.0,
        reason=reason,
        llm_invoked=llm_invoked,
        latency_ms=latency_ms,
    )


async def decide_retrieval_mode(
    *,
    question: str,
    has_cache: bool,
    recent_turns: list[dict[str, Any]],
    cached_titles: list[str],
    llm_router: LLMRouter,
) -> ModeDecision:
    """Classify the current turn's retrieval mode.

    ``question`` must be the raw student message for this turn (NOT the
    memory-prefixed q_effective — the memory prefix would dominate the
    classification evidence).
    """
    if not has_cache:
        return _fresh("no cached bundle for session")

    query = (question or "").strip()
    if not query:
        return _fresh("empty question")

    t0 = time.perf_counter()
    try:
        decision = await llm_router.classify(
            query=query,
            recent_turns=recent_turns,
            cached_titles=cached_titles,
        )
    except Exception:
        log.warning("retrieval-mode router failed — falling back to FRESH", exc_info=True)
        return _fresh(
            "router error — fail-open to FRESH",
            llm_invoked=True,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)

    mode = decision.retrieval_mode if decision.retrieval_mode in VALID_MODES else "FRESH"
    reason = decision.reason
    if mode != "FRESH" and decision.confidence < _min_confidence():
        reason = (
            f"confidence {decision.confidence:.2f} below floor "
            f"{_min_confidence():.2f} — downgraded {mode} to FRESH ({decision.reason})"
        )
        mode = "FRESH"

    return ModeDecision(
        mode=mode,
        route=decision.route,
        confidence=decision.confidence,
        reason=reason,
        llm_invoked=True,
        latency_ms=latency_ms,
    )
