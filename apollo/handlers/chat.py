"""POST /apollo/sessions/{id}/chat — full teaching turn."""
from __future__ import annotations

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.agent.apollo_llm import draft_reply
from apollo.agent.output_filter import validate_or_raise
from apollo.knowledge_graph.store import KGStore
from apollo.parser.parser_llm import parse_utterance
from apollo.persistence.models import ApolloSession, Message, ProblemAttempt


async def _next_turn_index(db: AsyncSession, session_id: int) -> int:
    result = await db.execute(
        select(Message.turn_index)
        .where(Message.session_id == session_id)
        .order_by(Message.turn_index.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    return (latest + 1) if latest is not None else 0


async def _load_history(db: AsyncSession, session_id: int) -> list[Dict[str, str]]:
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.turn_index)
    )
    rows = result.scalars().all()
    out = []
    for row in rows:
        role = "user" if row.role == "student" else "assistant"
        out.append({"role": role, "content": row.content})
    return out


async def handle_chat(*, db: AsyncSession, session_id: int, message: str) -> Dict[str, Any]:
    store = KGStore(db)

    sess = (await db.execute(
        select(ApolloSession).where(ApolloSession.id == session_id)
    )).scalar_one()
    current_attempt = (await db.execute(
        select(ProblemAttempt)
        .where(ProblemAttempt.session_id == session_id)
        .where(ProblemAttempt.problem_id == sess.current_problem_id)
        .order_by(ProblemAttempt.id.desc())
    )).scalars().first()
    if current_attempt is None:
        raise RuntimeError(f"no current ProblemAttempt for session {session_id}")

    entries = parse_utterance(message)
    added = await store.write_entries(attempt_id=current_attempt.id, entries=entries, source="parser")

    history = await _load_history(db, session_id)

    next_idx = await _next_turn_index(db, session_id)
    db.add(Message(
        session_id=session_id,
        attempt_id=current_attempt.id,
        role="student",
        content=message,
        turn_index=next_idx,
    ))
    await db.commit()

    history = history + [{"role": "user", "content": message}]

    kg_summary = await store.summarize_for_apollo(attempt_id=current_attempt.id)
    draft = draft_reply(history=history, kg_summary=kg_summary)

    kg = await store.read_kg(attempt_id=current_attempt.id)
    validated = validate_or_raise(draft, kg, history)

    next_idx = await _next_turn_index(db, session_id)
    db.add(Message(
        session_id=session_id,
        attempt_id=current_attempt.id,
        role="apollo",
        content=validated,
        turn_index=next_idx,
    ))
    await db.commit()

    return {
        "apollo_reply": validated,
        "kg_entries_added": added,
        "kg": kg,
    }
