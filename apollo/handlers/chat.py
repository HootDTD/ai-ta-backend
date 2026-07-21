"""POST /apollo/sessions/{id}/chat — full teaching turn (V3).

V3 + Item #5: chat handler runs an intent state machine before parsing.
- If a pending_intent is set on the session, the new utterance is treated
  as a confirmation. Affirmation -> execute (e.g. dispatch to handle_done);
  rejection (or any non-affirmative reply) -> clear and proceed normally.
- Otherwise: classify intent. If a non-teaching intent lands above the
  confidence threshold, set pending_intent and reply with a confirmation
  prompt. All other cases fall through to the normal teaching path.

Intent execution is wired for `done` only — other intents currently log
their classification and fall through to teaching. Future patches add
explicit handlers for restart/next/return-to-hoot.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.errors import ParserCouldNotExtractError
from apollo.handlers.intent import (
    INTENT_CONFIDENCE_THRESHOLD,
    classify_intent,
    confirmation_prompt_for,
    detect_confirmation,
)
from apollo.knowledge_graph.store import KGStore
from apollo.ontology import KGGraph
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.parser.graph_context import build_graph_context
from apollo.parser.parser_llm import parse_utterance
from apollo.persistence.models import ProblemAttempt, TutoringMessage, TutoringSession
from apollo.persistence.neo4j_client import KG_DEGRADED_ERRORS, Neo4jClient
from apollo.schemas.problem import Problem
from apollo.smart_questions import plan_next_question
from apollo.subjects.curriculum_db import load_concept_definition

_LOG = logging.getLogger(__name__)

_UNIFIED_QUESTIONING_FLAG: str = "APOLLO_UNIFIED_QUESTIONING_ENABLED"


def _unified_questioning_enabled() -> bool:
    return os.environ.get(_UNIFIED_QUESTIONING_FLAG, "").lower() in ("1", "true", "yes")


async def _find_problem(
    db: AsyncSession, concept_id: int, problem_id: int, *, course_id: int
) -> Problem:
    """Locate a problem in the DB bank by concept_id + target surrogate id. Mirrors
    done.py's helper. Kept inline rather than hoisted into problem_selector to
    keep that module's contract (problem listing) narrow."""
    for p in await list_problems_for_concept(
        db, concept_id=concept_id, search_space_id=course_id
    ):
        if p.database_id == problem_id:
            return p
    raise RuntimeError(f"problem {problem_id!r} not in bank for cluster {concept_id!r}")


async def _next_turn_index(db: AsyncSession, session_id: int) -> int:
    result = await db.execute(
        select(TutoringMessage.turn_index)
        .where(TutoringMessage.session_id == session_id)
        .order_by(TutoringMessage.turn_index.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    return (latest + 1) if latest is not None else 0


async def _load_history(
    db: AsyncSession, session_id: int, attempt_id: int,
) -> list[dict[str, str]]:
    result = await db.execute(
        select(TutoringMessage)
        .where(TutoringMessage.session_id == session_id)
        .where(TutoringMessage.attempt_id == attempt_id)
        .order_by(TutoringMessage.turn_index)
    )
    rows = result.scalars().all()
    out = []
    for row in rows:
        role = "user" if row.role == "student" else "assistant"
        out.append({"role": role, "content": row.content})
    return out


async def _persist_turn(
    db: AsyncSession,
    *,
    session_id: int,
    course_id: int,
    attempt_id: int,
    student_msg: str,
    apollo_msg: str,
) -> None:
    """Append the (student, apollo) turn pair atomically."""
    next_idx = await _next_turn_index(db, session_id)
    db.add(
        TutoringMessage(
            session_id=session_id,
            course_id=course_id,
            attempt_id=attempt_id,
            role="student",
            content=student_msg,
            turn_index=next_idx,
        )
    )
    db.add(
        TutoringMessage(
            session_id=session_id,
            course_id=course_id,
            attempt_id=attempt_id,
            role="apollo",
            content=apollo_msg,
            turn_index=next_idx + 1,
        )
    )
    await db.commit()


async def _read_graph_or_empty(store: KGStore, *, attempt_id: int, stage: str):
    """Degraded-mode KG read: `store.read_graph` failing with a
    `KG_DEGRADED_ERRORS` member (Neo4j missing / unreachable / broken
    connection) degrades to an empty `KGGraph` instead of 500ing the chat
    turn — the conversational reply (Postgres + OpenAI) always proceeds.
    """
    try:
        return await store.read_graph(attempt_id=attempt_id)
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=%s attempt_id=%s error=%s",
            stage, attempt_id, exc,
        )
        return KGGraph()


async def _write_kg_or_skip(
    store: KGStore,
    *,
    attempt_id: int,
    nodes: list,
    edges: list,
    source: str,
) -> int:
    """Degraded-mode KG write: `write_nodes`/`write_edges` failing with a
    `KG_DEGRADED_ERRORS` member skips the write entirely (`nodes_added=0`)
    rather than 500ing the turn. Edges are only attempted when nodes wrote
    successfully (mirrors the healthy-path ordering: edges need their
    endpoints to exist)."""
    try:
        nodes_added = await store.write_nodes(
            attempt_id=attempt_id, nodes=nodes, source=source,
        )
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=write_nodes attempt_id=%s error=%s",
            attempt_id, exc,
        )
        return 0
    try:
        await store.write_edges(attempt_id=attempt_id, edges=edges, source=source)
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=write_edges attempt_id=%s error=%s",
            attempt_id, exc,
        )
    return nodes_added


async def _handle_pending_done(
    *,
    db: AsyncSession,
    neo: Neo4jClient | None,
    sess: TutoringSession,
    attempt_id: int,
    message: str,
    store: KGStore,
) -> dict[str, Any] | None:
    """Resolve a pending `done` intent. Returns a chat-shaped response with
    embedded done payload when the student affirms, None when the gate
    should fall through to the normal teaching path (rejection or
    ambiguous reply).
    """
    confirmation = detect_confirmation(message)
    # Either way, the pending state is consumed this turn.
    sess.pending_intent = None

    if not confirmation.affirmed:
        # Treat rejection or ambiguity as "keep teaching". Just clear the
        # pending state and continue — the teaching path takes over.
        await db.commit()
        return None

    # Affirmed -> dispatch handle_done. Imported lazily to avoid the
    # otherwise circular import (handle_done <- store <- chat).
    from apollo.handlers.done import handle_done

    done_result = await handle_done(db=db, neo=neo, session_id=sess.id)

    apollo_reply = "Okay — grading what you've taught me now."
    await _persist_turn(
        db,
        session_id=sess.id,
        course_id=sess.course_id,
        attempt_id=attempt_id,
        student_msg=message,
        apollo_msg=apollo_reply,
    )

    graph = await _read_graph_or_empty(
        store, attempt_id=attempt_id, stage="handle_pending_done",
    )
    return {
        "apollo_reply": apollo_reply,
        "kg_entries_added": 0,
        "kg": graph.model_dump(mode="json"),
        "intent_executed": {"intent": "done", "result": done_result},
    }


async def _maybe_intent_confirmation(
    *,
    db: AsyncSession,
    sess: TutoringSession,
    attempt_id: int,
    message: str,
    history: list[dict[str, str]],
    concept,
    store: KGStore,
) -> dict[str, Any] | None:
    """If the new utterance classifies as a non-teaching intent above the
    confidence threshold, persist a confirmation turn and return a
    chat-shaped response. Otherwise return None and let the caller fall
    through to teaching."""
    verdict = classify_intent(
        utterance=message,
        history=history,
        concept=concept,
    )
    if verdict.intent == "teaching":
        return None
    if verdict.intent == "off_topic":
        _LOG.info(
            "apollo_intent_off_topic_fallthrough intent=%s confidence=%.3f",
            verdict.intent,
            verdict.confidence,
        )
        return None
    if verdict.confidence < INTENT_CONFIDENCE_THRESHOLD:
        return None

    prompt = confirmation_prompt_for(verdict.intent)
    if not prompt:
        return None

    sess.pending_intent = verdict.intent
    await db.commit()

    await _persist_turn(
        db,
        session_id=sess.id,
        course_id=sess.course_id,
        attempt_id=attempt_id,
        student_msg=message,
        apollo_msg=prompt,
    )
    graph = await _read_graph_or_empty(
        store, attempt_id=attempt_id, stage="maybe_intent_confirmation",
    )
    return {
        "apollo_reply": prompt,
        "kg_entries_added": 0,
        "kg": graph.model_dump(mode="json"),
        "intent_pending": {
            "intent": verdict.intent,
            "confidence": verdict.confidence,
        },
    }


async def handle_chat(
    *,
    db: AsyncSession,
    neo: Neo4jClient | None,
    session_id: int,
    message: str,
) -> dict[str, Any]:
    store = KGStore(db, neo)

    sess = (
        await db.execute(select(TutoringSession).where(TutoringSession.id == session_id))
    ).scalar_one()
    current_attempt = (
        (
            await db.execute(
                select(ProblemAttempt)
                .where(ProblemAttempt.session_id == session_id)
                .where(ProblemAttempt.problem_id == sess.current_problem_id)
                .order_by(ProblemAttempt.id.desc())
            )
        )
        .scalars()
        .first()
    )
    if current_attempt is None:
        raise RuntimeError(f"no current ProblemAttempt for session {session_id}")

    concept = await load_concept_definition(
        db, concept_id=sess.concept_id, search_space_id=sess.course_id
    )

    # ---- Intent state machine (item #5) -------------------------------
    # Step 1: if a pending intent exists, see if this turn confirms it.
    if sess.pending_intent == "done":
        result = await _handle_pending_done(
            db=db,
            neo=neo,
            sess=sess,
            attempt_id=current_attempt.id,
            message=message,
            store=store,
        )
        if result is not None:
            return result
        # Fell through (rejection / ambiguous) -> continue to teaching.
    elif sess.pending_intent is not None:
        # Other pending intents are just cleared; full handlers come later.
        sess.pending_intent = None
        await db.commit()

    # Step 2: classify new utterance. Above-threshold non-teaching ->
    # confirmation prompt + pending_intent set.
    history_pre = await _load_history(db, session_id, int(current_attempt.id))
    intent_response = await _maybe_intent_confirmation(
        db=db,
        sess=sess,
        attempt_id=current_attempt.id,
        message=message,
        history=history_pre,
        concept=concept,
        store=store,
    )
    if intent_response is not None:
        return intent_response

    # ---- Normal teaching path -----------------------------------------
    # Cross-turn linking (WU-2B): read the CURRENT subgraph (everything taught
    # so far this attempt — the new turn's nodes aren't written until after
    # parsing) and project it into a GraphContext the parser threads in so it
    # can emit edges referencing prior-turn node ids.
    prior_graph = await _read_graph_or_empty(
        store, attempt_id=current_attempt.id, stage="prior_graph",
    )
    graph_context = build_graph_context(prior_graph)
    try:
        nodes, edges = parse_utterance(
            message,
            concept=concept,
            attempt_id=current_attempt.id,
            graph_context=graph_context,
        )
    except ParserCouldNotExtractError:
        # The student only ever converses with Apollo: a turn the parser
        # cannot structure contributes zero KG entries and falls through to
        # the conversational reply instead of surfacing a 422 error card.
        nodes, edges = [], []
        _LOG.info(
            "apollo_parser_no_extract_fallthrough attempt_id=%s message_len=%d",
            current_attempt.id,
            len(message),
        )
    # write_nodes de-dups cross-turn re-assertions by id (WU-2B): a node whose
    # id already exists is reused, not re-minted, so the returned count is the
    # genuinely-new entries only. Degraded Neo4j -> writes are skipped
    # entirely and nodes_added=0 (see `_write_kg_or_skip`); the conversational
    # reply below always proceeds regardless.
    nodes_added = await _write_kg_or_skip(
        store,
        attempt_id=current_attempt.id,
        nodes=nodes,
        edges=edges,
        source="parser",
    )

    student_graph = await _read_graph_or_empty(
        store, attempt_id=current_attempt.id, stage="student_graph",
    )
    problem = await _find_problem(
        db, sess.concept_id, sess.current_problem_id, course_id=sess.course_id
    )
    next_idx = await _next_turn_index(db, session_id)

    # One-call reference-driven question controller. The same model assesses
    # the full student transcript and writes Apollo's answer-safe next reply.
    # The opportunity ledger still caps each reference node at one question;
    # when no eligible target remains, grade automatically.
    if not _unified_questioning_enabled():
        _LOG.warning(
            "apollo_unified_questioning_flag_off_ignored session_id=%s",
            session_id,
        )
    full_transcript = [
        ("student" if item["role"] == "user" else "apollo", item["content"])
        for item in history_pre
    ] + [("student", message)]
    decision = await plan_next_question(
        db,
        course_id=int(sess.course_id),
        attempt_id=int(current_attempt.id),
        session_id=session_id,
        problem=problem,
        transcript=full_transcript,
        turn_index=next_idx,
    )
    covered_topics = [
        {"node_id": topic.node_id, "display_name": topic.display_name}
        for topic in decision.covered_topics
    ]
    if decision.action == "ask":
        validated = decision.question or "Can you explain that part one more time?"
    else:
        validated = "Thanks — I have enough to grade what you taught me."

    await _persist_turn(
        db,
        session_id=session_id,
        course_id=sess.course_id,
        attempt_id=int(current_attempt.id),
        student_msg=message,
        apollo_msg=validated,
    )
    if decision.action == "done":
        from apollo.handlers.done import handle_done  # noqa: PLC0415

        done_result = await handle_done(db=db, neo=neo, session_id=session_id)
        return {
            "apollo_reply": validated,
            "kg_entries_added": nodes_added,
            "kg": student_graph.model_dump(mode="json"),
            "covered_topics": covered_topics,
            "intent_executed": {"intent": "done", "result": done_result},
        }
    return {
        "apollo_reply": validated,
        "kg_entries_added": nodes_added,
        "kg": student_graph.model_dump(mode="json"),
        "covered_topics": covered_topics,
        "question_target": decision.target_node_id,
    }
