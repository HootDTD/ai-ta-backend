"""POST /apollo/sessions/{id}/kg/{entry_id}/{move} — Negotiable OLM endpoints.

Three moves on a parser-authored KG entry:
    challenge  — student flags the entry as wrong / misheard
    paraphrase — student supplies their preferred surface form (preserves
                 the structural fields; sets student_belief)
    skip       — student passes the entry to the grader without a paraphrase

Plus:
    GET /apollo/sessions/{id}/kg/{entry_id}/trace
       — returns the chronological audit trail of moves on one entry plus
         the most recent student utterance (best-effort source for the FE
         "Apollo's wiring" trace card).

Research anchors for the move set:
    Bull & Pain 1995 (Mr Collins)        — dual-belief schema; structured moves
    Dimitrova 2003 (STyLE-OLM)           — challenge / offer-evidence / accept
    Kerly & Bull 2007 (CALMsystem)       — empirical n=30; ~40% reduction in
                                            self-assessment discrepancies

The 3-move v1 is the minimal subset that closes Gap A (parser
canonicalization tax) without forcing the student into a complex
dialogue. Future v2 adds OFFER-EVIDENCE and EDIT-DIRECT.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import InvalidPhaseError, KGUnavailableError
from apollo.knowledge_graph.store import KGStore
from apollo.persistence.models import TutoringSession, ProblemAttempt, SessionPhase
from apollo.persistence.neo4j_client import KG_DEGRADED_ERRORS, Neo4jClient

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request models — Pydantic enforces payload-length validation.
# ---------------------------------------------------------------------------

class ChallengeRequest(BaseModel):
    """The student's reason for disputing the entry. Max 500 chars keeps
    payloads small without crowding the FE textarea — matches the design
    doc's expected length for a one-paragraph dispute."""
    reason: str = Field(min_length=1, max_length=500)


class ParaphraseRequest(BaseModel):
    """The student's preferred surface form. Max 1000 chars accommodates
    multi-line equations or longer descriptive paraphrases without letting
    pasted essays leak in."""
    surface_form: str = Field(min_length=1, max_length=1000)


class SkipRequest(BaseModel):
    """Skip carries no payload. The FE may POST {} or omit the body."""
    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_active_attempt(
    *, db: AsyncSession, session_id: int,
) -> ProblemAttempt:
    """Find the active (latest) ProblemAttempt for a session.

    Negotiations target the entry inside the per-attempt KG subgraph, so
    we resolve the attempt before delegating to the store. Refuses if no
    attempt exists (the student hasn't started a problem yet) — that's
    the InvalidPhaseError contract the FE already understands.
    """
    sess = (await db.execute(
        select(TutoringSession).where(TutoringSession.id == session_id)
    )).scalar_one_or_none()
    if sess is None:
        raise InvalidPhaseError(session_id=session_id, phase="missing")

    attempt = (await db.execute(
        select(ProblemAttempt)
        .where(ProblemAttempt.session_id == session_id)
        .order_by(ProblemAttempt.id.desc())
        .limit(1)
    )).scalars().first()

    if attempt is None:
        raise InvalidPhaseError(session_id=session_id, phase=str(sess.phase))

    return attempt


async def _kg_snapshot(*, store: KGStore, attempt_id: int) -> Dict[str, Any]:
    """Re-read the per-attempt subgraph and shape it into the same envelope
    the chat handler returns. Frees the FE from a follow-up GET after every
    move."""
    graph = await store.read_graph(attempt_id=attempt_id)
    return {
        "nodes": [n.model_dump(mode="python") for n in graph.nodes],
        "edges": [
            {
                "edge_type": e.edge_type.value,
                "from_node_id": e.from_node_id,
                "to_node_id": e.to_node_id,
                "attempt_id": e.attempt_id,
                "source": e.source,
            }
            for e in graph.edges
        ],
    }


# ---------------------------------------------------------------------------
# Handlers — one per move + trace.
# ---------------------------------------------------------------------------

async def handle_challenge(
    *,
    db: AsyncSession,
    neo: Neo4jClient | None,
    session_id: int,
    entry_id: str,
    body: ChallengeRequest,
) -> Dict[str, Any]:
    """CHALLENGE: status -> DISPUTED. Returns updated entry + KG envelope."""
    attempt = await _resolve_active_attempt(db=db, session_id=session_id)
    store = KGStore(db, neo)
    try:
        updated = await store.mark_node_disputed(
            attempt_id=attempt.id, node_id=entry_id, reason=body.reason,
        )
        kg = await _kg_snapshot(store=store, attempt_id=attempt.id)
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=challenge attempt_id=%s error=%s",
            attempt.id, exc,
        )
        raise KGUnavailableError(stage="challenge", last_error=str(exc)) from exc
    return {
        "entry": updated.model_dump(mode="python"),
        "kg": kg,
        "move": "challenge",
    }


async def handle_paraphrase(
    *,
    db: AsyncSession,
    neo: Neo4jClient | None,
    session_id: int,
    entry_id: str,
    body: ParaphraseRequest,
) -> Dict[str, Any]:
    """SUPPLY-PARAPHRASE: status -> DUAL, student_belief = surface_form."""
    attempt = await _resolve_active_attempt(db=db, session_id=session_id)
    store = KGStore(db, neo)
    try:
        updated = await store.paraphrase_node(
            attempt_id=attempt.id, node_id=entry_id, surface_form=body.surface_form,
        )
        kg = await _kg_snapshot(store=store, attempt_id=attempt.id)
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=paraphrase attempt_id=%s error=%s",
            attempt.id, exc,
        )
        raise KGUnavailableError(stage="paraphrase", last_error=str(exc)) from exc
    return {
        "entry": updated.model_dump(mode="python"),
        "kg": kg,
        "move": "paraphrase",
    }


async def handle_skip(
    *,
    db: AsyncSession,
    neo: Neo4jClient | None,
    session_id: int,
    entry_id: str,
) -> Dict[str, Any]:
    """SKIP: status -> DUAL with no edits. Flags for grader."""
    attempt = await _resolve_active_attempt(db=db, session_id=session_id)
    store = KGStore(db, neo)
    try:
        updated = await store.skip_node(attempt_id=attempt.id, node_id=entry_id)
        kg = await _kg_snapshot(store=store, attempt_id=attempt.id)
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=skip attempt_id=%s error=%s",
            attempt.id, exc,
        )
        raise KGUnavailableError(stage="skip", last_error=str(exc)) from exc
    return {
        "entry": updated.model_dump(mode="python"),
        "kg": kg,
        "move": "skip",
    }


async def handle_get_trace(
    *,
    db: AsyncSession,
    neo: Neo4jClient | None,
    session_id: int,
    entry_id: str,
) -> Dict[str, Any]:
    """Read-only audit trail for one entry."""
    attempt = await _resolve_active_attempt(db=db, session_id=session_id)
    store = KGStore(db, neo)
    return await store.get_node_trace(attempt_id=attempt.id, node_id=entry_id)
