"""Session lifecycle handlers for Slice 0a: /retry, /end, GET /sessions/{id}."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.knowledge_graph.store import KGStore
from apollo.overseer.problem_selector import list_problems_for_cluster
from apollo.persistence.models import ApolloSession, Message, SessionPhase, SessionStatus


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
    store = KGStore(db)
    kg = await store.read_kg(session_id)

    msgs = (await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.turn_index)
    )).scalars().all()

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
