"""Session lifecycle handlers for Slice 0a: /retry, /end, GET /sessions/{id}."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.store import KGStore
from apollo.overseer.problem_selector import list_problems_for_cluster
from apollo.persistence.models import (
    ApolloSession,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.persistence.neo4j_client import Neo4jClient


async def handle_retry(*, db: AsyncSession, session_id: int) -> dict[str, Any]:
    """Student clicked 'Teach more and retry' — unfreeze KG, return to TEACHING."""
    sess = (
        await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    sess.phase = SessionPhase.TEACHING.value
    await db.commit()
    return {"ok": True}


async def handle_end(
    *,
    db: AsyncSession,
    neo: Neo4jClient,
    session_id: int,
) -> dict[str, Any]:
    """Student clicked 'End session' — mark the session ended.

    Retention (§7 retention change, WU-3C1): per-attempt Neo4j subgraphs are
    PERSISTED — `handle_end` no longer deletes them. Cross-attempt connectivity
    comes free (two attempts resolving to the same :Canon node are two hops
    apart). A future janitor prunes old subgraphs via
    `KGStore.delete_subgraph`; `restart_problem` is the ONLY explicit student
    wipe. The `neo` param is retained for signature parity with the other
    lifecycle handlers (api.py threads it positionally-by-keyword); it is
    unused here now that no Neo4j write happens at end-of-session.
    """
    sess = (
        await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    sess.status = SessionStatus.ended.value
    await db.commit()

    return {"ok": True}


async def handle_get_session(
    *,
    db: AsyncSession,
    neo: Neo4jClient,
    session_id: int,
) -> dict[str, Any]:
    sess = (
        await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()

    current_attempt_id: int | None = None
    if sess.current_problem_id:
        current_attempt_id = (
            (
                await db.execute(
                    select(ProblemAttempt.id)
                    .where(ProblemAttempt.session_id == session_id)
                    .where(ProblemAttempt.problem_id == sess.current_problem_id)
                    .order_by(ProblemAttempt.id.desc())
                )
            )
            .scalars()
            .first()
        )

    store = KGStore(db, neo)
    if current_attempt_id is not None:
        graph = await store.read_graph(attempt_id=current_attempt_id)
        kg = graph.model_dump(mode="json")
        msgs = (
            (
                await db.execute(
                    select(Message)
                    .where(Message.attempt_id == current_attempt_id)
                    .order_by(Message.turn_index)
                )
            )
            .scalars()
            .all()
        )
    else:
        kg = {"nodes": [], "edges": []}
        msgs = []

    problem = None
    if sess.current_problem_id:
        for p in list_problems_for_cluster(sess.concept_cluster_id):
            if p.id == sess.current_problem_id:
                problem = {
                    "id": p.id,
                    "concept_id": p.concept_id,
                    "difficulty": p.difficulty,
                    "problem_text": p.problem_text,
                    "given_values": p.given_values,
                    "target_unknown": p.target_unknown,
                }
                break

    return {
        "session_id": sess.id,
        "user_id": sess.user_id,
        "search_space_id": sess.search_space_id,
        "concept_cluster_id": sess.concept_cluster_id,
        "status": sess.status,
        "phase": sess.phase,
        "problem": problem,
        "kg": kg,
        "messages": [
            {"role": m.role, "content": m.content, "turn_index": m.turn_index} for m in msgs
        ],
    }
