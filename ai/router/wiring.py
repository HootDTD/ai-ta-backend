"""Server-side wiring for the retrieval-mode orchestrator.

Glue between the chat layer (session bundle cache), the mode router, and the
QA pipeline in server.py. Everything is gated behind ROUTER_ENABLED and every
failure degrades to the legacy FRESH path — with the flag off (default), the
request path is byte-identical to pre-router behavior.

Flow per /ask turn (flag on):
  1. ``prepare_router_context`` (after the user turn is persisted): load the
     session's cached bundle, check the visible-docs fingerprint, and run the
     mode decision. Attachments force FRESH without an LLM call.
  2. server.py picks the retrieval path: NONE → ``bundle_from_cache``;
     AUGMENT → small top-up retrieval merged via ``merge_augment_bundle``;
     FRESH → legacy ``_ask_pgvector``.
  3. ``persist_turn_outcome`` (after the answer): save the bundle + scoring
     rows back to the session cache and write a ``chat_router_decisions``
     telemetry row. Never raises.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from ai.router.llm_router import LLMRouter
from ai.router.mode import ModeDecision, decide_retrieval_mode
from chats.bundle_cache import (
    CachedBundle,
    compute_visible_docs_hash,
    load_bundle_cache,
    save_bundle_cache,
)
from chats.service import get_chat_session_for_user, list_recent_turns
from config.contracts import BundleSnippet, ResearchBundle, ResearchMetadata
from database.models import ChatRouterDecision, ChatTurn
from database.session import get_async_session

log = logging.getLogger(__name__)

CACHED_SCORES_KEY = "cached_citation_scores"

_ROUTER_RECENT_TURNS = int(os.getenv("ROUTER_RECENT_TURNS", "6"))
_ROUTER_TURN_CONTENT_CHARS = 300

_llm_router: Optional[LLMRouter] = None


def router_enabled() -> bool:
    return (os.getenv("ROUTER_ENABLED", "") or "").strip().lower() in {"1", "true", "yes", "on"}


def augment_top_k() -> int:
    return int(os.getenv("ROUTER_AUGMENT_TOP_K", "8"))


def augment_token_budget() -> int:
    return int(os.getenv("ROUTER_AUGMENT_TOKEN_BUDGET", "2000"))


def max_merged_snippets() -> int:
    return int(os.getenv("ROUTER_MAX_SNIPPETS", os.getenv("K_SEM", "20")))


def _get_llm_router() -> LLMRouter:
    global _llm_router
    if _llm_router is None:
        from openai import AsyncOpenAI

        _llm_router = LLMRouter(client=AsyncOpenAI())
    return _llm_router


@dataclass
class RouterTurnContext:
    chat_session_id: int
    decision: ModeDecision
    cached: Optional[CachedBundle]
    visible_docs_hash: str


async def prepare_router_context(
    *,
    chat_id: str,
    user_id: str,
    search_space_id: int,
    question: str,
    has_attachments: bool,
) -> Optional[RouterTurnContext]:
    """Load cache state and decide the retrieval mode for this turn.

    Must run AFTER the user turn is persisted (the chat session is guaranteed
    to exist and recent turns include the current question). Returns None on
    any failure — callers treat None as the legacy FRESH path.
    """
    try:
        async with get_async_session() as db_session:
            session = await get_chat_session_for_user(
                db_session, chat_id=chat_id, user_id=user_id
            )
            if session is None:
                return None
            chat_session_id = int(session.id)

            cached = await load_bundle_cache(db_session, chat_session=session)
            visible_hash = await compute_visible_docs_hash(
                db_session, int(search_space_id)
            )
            if cached is not None and cached.visible_docs_hash != visible_hash:
                log.info(
                    "router: visible docs changed for session=%s — cache invalidated",
                    chat_session_id,
                )
                cached = None

            turns = await list_recent_turns(
                db_session,
                chat_session_id=chat_session_id,
                limit=_ROUTER_RECENT_TURNS,
            )
            recent_turns = [
                {
                    "role": (t.role or "user"),
                    "content": (t.content or "")[:_ROUTER_TURN_CONTENT_CHARS],
                }
                for t in turns
            ]

        if has_attachments:
            decision = ModeDecision(
                mode="FRESH",
                route="",
                confidence=1.0,
                reason="image attachments present — forced FRESH",
                llm_invoked=False,
                latency_ms=0,
            )
        else:
            decision = await decide_retrieval_mode(
                question=question,
                has_cache=cached is not None,
                recent_turns=recent_turns,
                cached_titles=cached.titles if cached is not None else [],
                llm_router=_get_llm_router(),
            )

        log.info(
            "router: mode=%s llm=%s conf=%.2f latency=%dms reason=%s",
            decision.mode, decision.llm_invoked, decision.confidence,
            decision.latency_ms, decision.reason,
        )
        return RouterTurnContext(
            chat_session_id=chat_session_id,
            decision=decision,
            cached=cached,
            visible_docs_hash=visible_hash,
        )
    except Exception:
        log.warning("router: prepare_router_context failed — legacy path", exc_info=True)
        return None


def _assemble_bundle(
    snippets: List[BundleSnippet],
    *,
    q_effective: str,
    subject: str,
    provenance: Dict[str, Any],
    cached_scores: Dict[str, Dict[str, Any]],
) -> ResearchBundle:
    from retrieval.context_packer import _summarize_snippets

    equations, glossary, assumptions, _ = _summarize_snippets(snippets)
    allowed_markers = [sn.citation_marker for sn in snippets if sn.citation_marker]
    metadata = ResearchMetadata(
        final_query=q_effective,
        original_query=q_effective,
        attempted_terms=[q_effective],
        subject=subject,
        allowed_markers=allowed_markers,
    )
    if cached_scores:
        provenance = {**provenance, CACHED_SCORES_KEY: cached_scores}
    return ResearchBundle(
        metadata=metadata,
        snippets=snippets,
        equations=equations,
        assumptions=assumptions,
        glossary=glossary,
        coverage_gaps=[],
        refinement_queries=[],
        used_ids=[sn.id for sn in snippets],
        provenance=provenance,
        allowed_markers=allowed_markers,
        attempted_terms=[q_effective],
        subject=subject,
    )


def bundle_from_cache(
    cached: CachedBundle, *, q_effective: str, subject: str, reason: str
) -> ResearchBundle:
    """NONE mode: rebuild a ResearchBundle entirely from the session cache."""
    return _assemble_bundle(
        list(cached.snippets),
        q_effective=q_effective,
        subject=subject,
        provenance={
            "source": "session_cache",
            "retrieval_mode": "NONE",
            "router_reason": reason,
        },
        cached_scores=dict(cached.scoring),
    )


def merge_augment_bundle(
    cached: CachedBundle,
    fresh_bundle: ResearchBundle,
    *,
    q_effective: str,
    subject: str,
    reason: str,
) -> ResearchBundle:
    """AUGMENT mode: fresh top-up snippets first, then the best cached ones.

    Fresh snippets all survive (they answered the new query); cached snippets
    fill the remaining slots ordered by their cached citation score. Capped at
    ROUTER_MAX_SNIPPETS so the merged excerpt set stays within the same order
    of prompt size as a FRESH bundle.
    """
    merged: List[BundleSnippet] = list(fresh_bundle.snippets)
    seen_ids = {sn.id for sn in merged}

    def _cached_score(sn: BundleSnippet) -> float:
        row = cached.scoring.get(sn.id) or {}
        try:
            return float(row.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    cap = max_merged_snippets()
    for sn in sorted(cached.snippets, key=_cached_score, reverse=True):
        if len(merged) >= cap:
            break
        if sn.id in seen_ids:
            continue
        merged.append(sn)
        seen_ids.add(sn.id)

    cached_scores = {
        sn.id: cached.scoring[sn.id]
        for sn in merged
        if sn.id in cached.scoring
    }
    return _assemble_bundle(
        merged,
        q_effective=q_effective,
        subject=subject,
        provenance={
            "source": "pgvector+session_cache",
            "retrieval_mode": "AUGMENT",
            "router_reason": reason,
        },
        cached_scores=cached_scores,
    )


def scoring_rows_from_bundle(bundle: ResearchBundle) -> Dict[str, Dict[str, Any]]:
    """Extract per-snippet citation-scoring rows (set by _prepare_solver_inputs)
    keyed by snippet id, for persisting into the session cache."""
    rows: Dict[str, Dict[str, Any]] = {}
    try:
        rankings = (bundle.provenance or {}).get("citation_rankings") or []
        for row in rankings:
            if isinstance(row, dict) and row.get("snippet_id"):
                rows[str(row["snippet_id"])] = row
    except Exception:
        log.warning("router: failed to extract citation rankings", exc_info=True)
    return rows


async def persist_turn_outcome(
    *,
    chat_id: str,
    user_id: str,
    ctx: RouterTurnContext,
    bundle: Optional[ResearchBundle],
    question: str = "",
    latency_retrieval_ms: Optional[int] = None,
    latency_answer_ms: Optional[int] = None,
) -> None:
    """Save the answered bundle into the session cache + write telemetry.

    Never raises: cache/telemetry failures must not break the response.
    """
    try:
        async with get_async_session() as db_session:
            session = await get_chat_session_for_user(
                db_session, chat_id=chat_id, user_id=user_id
            )
            if session is None:
                return

            turn_index_result = await db_session.execute(
                select(func.coalesce(func.max(ChatTurn.turn_index), 0)).where(
                    ChatTurn.chat_session_id == session.id
                )
            )
            turn_index = int(turn_index_result.scalar_one() or 0)

            if bundle is not None and bundle.snippets:
                await save_bundle_cache(
                    db_session,
                    chat_session=session,
                    turn_index=turn_index,
                    snippets=list(bundle.snippets),
                    scoring=scoring_rows_from_bundle(bundle),
                    visible_docs_hash=ctx.visible_docs_hash,
                    replace=ctx.decision.mode == "FRESH",
                )

            decision = ctx.decision
            db_session.add(
                ChatRouterDecision(
                    turn_id=str(turn_index),
                    chat_session_id=session.id,
                    query=(question or "")[:2000],
                    stage1_top1=None,
                    stage1_margin=None,
                    stage1_route=None,
                    stage2_invoked=decision.llm_invoked,
                    stage2_route=decision.route or None,
                    stage2_confidence=decision.confidence,
                    retrieval_mode=decision.mode,
                    retrieval_top_score=None,
                    final_route=decision.route or "unrouted",
                    was_clarified=False,
                    clarify_cause=None,
                    latency_router_ms=decision.latency_ms,
                    latency_retrieval_ms=latency_retrieval_ms,
                    latency_answer_ms=latency_answer_ms,
                )
            )
            await db_session.commit()
    except Exception:
        log.warning("router: persist_turn_outcome failed (non-fatal)", exc_info=True)
