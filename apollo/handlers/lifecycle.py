"""Session lifecycle handlers for Slice 0a: /retry, /end, GET /sessions/{id}."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.store import KGStore
from apollo.overseer.problem_selector import list_problems_for_cluster
from apollo.persistence.models import ApolloSession, Message, ProblemAttempt, SessionPhase, SessionStatus


async def handle_retry(*, db: AsyncSession, session_id: int) -> Dict[str, Any]:
    """Student clicked 'Teach more and retry' — unfreeze KG, return to TEACHING."""
    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.TEACHING.value
    await db.commit()
    return {"ok": True}


async def handle_end(*, db: AsyncSession, session_id: int) -> Dict[str, Any]:
    """Student clicked 'End session' — mark ended, keep row for history."""
    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.status = SessionStatus.ended.value
    await db.commit()
    return {"ok": True}


async def handle_get_session(*, db: AsyncSession, session_id: int) -> Dict[str, Any]:
    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()

    current_attempt_id: int | None = None
    if sess.current_problem_id:
        current_attempt_id = (await db.execute(
            select(ProblemAttempt.id)
            .where(ProblemAttempt.session_id == session_id)
            .where(ProblemAttempt.problem_id == sess.current_problem_id)
            .order_by(ProblemAttempt.id.desc())
        )).scalars().first()

    store = KGStore(db)
    if current_attempt_id is not None:
        kg = await store.read_kg(attempt_id=current_attempt_id)
        msgs = (await db.execute(
            select(Message)
            .where(Message.attempt_id == current_attempt_id)
            .order_by(Message.turn_index)
        )).scalars().all()
    else:
        kg = {t: [] for t in ("equation", "definition", "condition", "simplification", "variable_mapping", "procedure_step")}
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
        "student_id": sess.student_id,
        "concept_cluster_id": sess.concept_cluster_id,
        "status": sess.status,
        "phase": sess.phase,
        "problem": problem,
        "kg": kg,
        "messages": [
            {"role": m.role, "content": m.content, "turn_index": m.turn_index}
            for m in msgs
        ],
    }
