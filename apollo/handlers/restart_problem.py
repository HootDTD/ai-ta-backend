"""POST /apollo/sessions/{id}/restart_problem — wipe current attempt's KG + messages.

Same ProblemAttempt row, same problem, same difficulty. Caller gets a clean
conversation and a clean KG on the same problem. Blocked during SOLVING.
INIT / BETWEEN raise InvalidPhaseError.

V3: KG wipe is now a Neo4j subgraph DETACH DELETE via KGStore.delete_subgraph.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import InvalidPhaseError, KGUnavailableError, SessionFrozenError
from apollo.knowledge_graph.store import KGStore
from apollo.persistence.models import (
    ApolloSession,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.persistence.neo4j_client import KG_DEGRADED_ERRORS, Neo4jClient

_LOG = logging.getLogger(__name__)


_ALLOWED_PHASES = {
    SessionPhase.TEACHING.value,
    SessionPhase.PROBLEM_REVEAL.value,
    SessionPhase.REPORT.value,
}
_FROZEN_PHASES = {SessionPhase.SOLVING.value}


async def handle_restart_problem(
    *,
    db: AsyncSession,
    neo: Neo4jClient | None,
    session_id: int,
) -> Dict[str, Any]:
    # Row lock on Postgres to serialize concurrent restart + chat writes.
    # SQLite silently ignores it.
    sess = (await db.execute(
        select(ApolloSession).where(ApolloSession.id == session_id).with_for_update()
    )).scalar_one()

    if sess.status != SessionStatus.active.value:
        raise InvalidPhaseError(session_id=session_id, phase=f"status={sess.status}")
    if sess.phase in _FROZEN_PHASES:
        raise SessionFrozenError(session_id=str(session_id))
    if sess.phase not in _ALLOWED_PHASES:
        raise InvalidPhaseError(session_id=session_id, phase=sess.phase)

    current_attempt = (await db.execute(
        select(ProblemAttempt)
        .where(ProblemAttempt.session_id == session_id)
        .where(ProblemAttempt.problem_id == sess.current_problem_id)
        .order_by(ProblemAttempt.id.desc())
    )).scalars().first()
    if current_attempt is None:
        raise RuntimeError(f"no current ProblemAttempt for session {session_id}")

    store = KGStore(db, neo)
    # Retention (§7, WU-3C1): restart_problem is the ONE explicit student wipe
    # that still deletes the subgraph — handle_end now PERSISTS (no delete).
    #
    # Degraded mode: NO silent skip here — the wipe targets the SAME
    # attempt_id (no new ProblemAttempt row is created), so silently skipping
    # `delete_subgraph` would resurface stale KG nodes once Neo4j returns.
    # Raise a structured KGUnavailableError -> 503 instead; the Postgres
    # message delete below never runs, so nothing is half-wiped (with
    # neo=None the store guard raises before any deletion is attempted).
    try:
        await store.delete_subgraph(attempt_id=current_attempt.id)
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=restart_problem attempt_id=%s error=%s",
            current_attempt.id, exc,
        )
        raise KGUnavailableError(stage="restart_problem", last_error=str(exc)) from exc
    await db.execute(delete(Message).where(Message.attempt_id == current_attempt.id))

    sess.phase = SessionPhase.TEACHING.value
    await db.commit()

    return {"ok": True}
