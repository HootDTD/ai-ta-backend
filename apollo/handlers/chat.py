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

import asyncio
import logging
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
from apollo.hoot_bridge.reference_answer import (
    ASIDE_COUNT_SESSION_METADATA_KEY,
    ASIDE_MESSAGE_INTENT_TAG,
    MAX_ASIDES_PER_SESSION,
    MESSAGE_KIND_REFERENCE_ASIDE,
    ReferenceAsideResult,
    answer_reference_question,
)
from apollo.hoot_bridge.reference_answer import (
    is_enabled as _interaction4_enabled,
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
from config.settings import interaction_allowed_for_concept

_LOG = logging.getLogger(__name__)

# INTERACTION4 "ask Hoot" hint lane (apollo/hoot_bridge/reference_answer.py).
# Persona-side framing lives here, not in the bridge: the bridge returns a
# subject-agnostic Hoot answer, and Apollo never claims it as its own
# knowledge (brief: the aside is "visually and structurally outside the
# persona").
_REFERENCE_QUESTION_RESUME_LINE = "Okay, so how does that fit into what you were teaching me?"
# When the relevance guard rejects the aside (in_scope=False, the "That's
# outside what's covered…" refusal), asking how it "fits into" the lesson
# makes no sense — redirect back to the teaching thread instead.
_REFERENCE_QUESTION_OUT_OF_SCOPE_RESUME_LINE = (
    "Okay, let's get back on topic then. What's the next thing you wanted to teach me?"
)
_REFERENCE_QUESTION_CAP_REDIRECT = (
    "I think I've looked enough things up for this session — let's keep teaching so I can "
    "really learn it. What were you saying?"
)
_REFERENCE_QUESTION_APOLOGY = (
    "Hmm, I couldn't look that up right now. Let's keep going — what were you saying?"
)
_REFERENCE_QUESTION_EMPTY = "Type your question above first, then click Ask."


async def _find_problem(
    db: AsyncSession, concept_id: int, problem_id: int, *, course_id: int
) -> Problem:
    """Locate a problem in the DB bank by concept_id + target surrogate id. Mirrors
    done.py's helper. Kept inline rather than hoisted into problem_selector to
    keep that module's contract (problem listing) narrow."""
    for p in await list_problems_for_concept(db, concept_id=concept_id, search_space_id=course_id):
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
    db: AsyncSession,
    session_id: int,
    attempt_id: int,
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
    """Append the (student, apollo) turn pair atomically.

    Used by the SHORT lanes only (intent confirmations, aside cap/apology) —
    the normal teaching path persists the student message up front instead
    (`_persist_student_message`, bimodal-fix P0.3) because its LLM chain runs
    10-17s and a Done clicked mid-turn would grade a transcript missing the
    student's last message."""
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


async def _persist_student_message(
    db: AsyncSession,
    *,
    session_id: int,
    course_id: int,
    attempt_id: int,
    content: str,
) -> int:
    """Persist the student's message in its own commit; returns its turn index.

    Done-race fix (2026-08-07 bimodal-fix P0.3, defect I3): the teaching path
    used to persist the (student, apollo) pair only at the END of the turn,
    after 10-17s of LLM work — a Done clicked mid-turn graded a transcript
    missing the student's last (usually best) message. Persisting the student
    row BEFORE the parse/questioning chain closes that window. If the turn
    later fails, the dangling student row is kept deliberately: the transcript
    retains the student's words (grading-favorable), and the next turn's
    history simply includes them."""
    next_idx = await _next_turn_index(db, session_id)
    db.add(
        TutoringMessage(
            session_id=session_id,
            course_id=course_id,
            attempt_id=attempt_id,
            role="student",
            content=content,
            turn_index=next_idx,
        )
    )
    await db.commit()
    return next_idx


async def _persist_apollo_reply(
    db: AsyncSession,
    *,
    session_id: int,
    course_id: int,
    attempt_id: int,
    apollo_msg: str,
) -> None:
    """Append Apollo's reply for a teaching turn whose student message was
    already persisted up front by `_persist_student_message` (P0.3)."""
    next_idx = await _next_turn_index(db, session_id)
    db.add(
        TutoringMessage(
            session_id=session_id,
            course_id=course_id,
            attempt_id=attempt_id,
            role="apollo",
            content=apollo_msg,
            turn_index=next_idx,
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
            stage,
            attempt_id,
            exc,
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
            attempt_id=attempt_id,
            nodes=nodes,
            source=source,
        )
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=write_nodes attempt_id=%s error=%s",
            attempt_id,
            exc,
        )
        return 0
    try:
        await store.write_edges(attempt_id=attempt_id, edges=edges, source=source)
    except KG_DEGRADED_ERRORS as exc:
        _LOG.warning(
            "apollo_neo4j_degraded stage=write_edges attempt_id=%s error=%s",
            attempt_id,
            exc,
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
        store,
        attempt_id=attempt_id,
        stage="handle_pending_done",
    )
    return {
        "apollo_reply": apollo_reply,
        "kg_entries_added": 0,
        "kg": graph.model_dump(mode="json"),
        "intent_executed": {"intent": "done", "result": done_result},
    }


async def _execute_reference_question(
    *,
    db: AsyncSession,
    sess: TutoringSession,
    attempt_id: int,
    message: str,
    store: KGStore,
    problem: Problem,
) -> dict[str, Any]:
    """Direct-execute a `reference_question` intent: route the utterance
    through Hoot's QA bridge and return it as an aside, or a plain
    persona reply on cap / failure. Never confirmation-gated (unlike
    `done`) and never raises — a failure in the composed path apologizes
    and falls through as an ordinary teaching-shaped turn, never a 5xx.
    """
    current_count = int((sess.metadata_ or {}).get(ASIDE_COUNT_SESSION_METADATA_KEY, 0))
    if not message.strip():
        _LOG.info(
            "apollo_reference_question_empty session_id=%s attempt_id=%s",
            sess.id,
            attempt_id,
        )
        return await _persist_reference_aside_turn(
            db=db,
            sess=sess,
            attempt_id=attempt_id,
            message=message,
            store=store,
            result=ReferenceAsideResult(
                in_scope=True,
                text=_REFERENCE_QUESTION_EMPTY,
                citations=[],
            ),
            aside_count=current_count,
            graph_stage="reference_question_empty",
        )

    if current_count >= MAX_ASIDES_PER_SESSION:
        await _persist_turn(
            db,
            session_id=sess.id,
            course_id=sess.course_id,
            attempt_id=attempt_id,
            student_msg=message,
            apollo_msg=_REFERENCE_QUESTION_CAP_REDIRECT,
        )
        graph = await _read_graph_or_empty(
            store, attempt_id=attempt_id, stage="reference_question_capped"
        )
        return {
            "apollo_reply": _REFERENCE_QUESTION_CAP_REDIRECT,
            "kg_entries_added": 0,
            "kg": graph.model_dump(mode="json"),
        }

    try:
        result = await answer_reference_question(
            db=db,
            course_id=int(sess.course_id),
            question=message,
            problem=problem,
        )
    except Exception:  # noqa: BLE001 - never a 5xx on a chat turn
        _LOG.warning(
            "apollo_reference_question_failed session_id=%s attempt_id=%s",
            sess.id,
            attempt_id,
            exc_info=True,
        )
        # The bridge failure may have aborted the session's transaction (e.g.
        # a retrieval SQL error): roll back so the apology persists on a clean
        # transaction, and keep the persist itself best-effort — a dead
        # connection must still produce the apology reply, not a 5xx
        # (2026-08-01 halfvec schema-drift incident escaped here as a 500).
        try:
            await db.rollback()
            await _persist_turn(
                db,
                session_id=sess.id,
                course_id=sess.course_id,
                attempt_id=attempt_id,
                student_msg=message,
                apollo_msg=_REFERENCE_QUESTION_APOLOGY,
            )
        except Exception:  # noqa: BLE001 - apology persistence is best-effort
            _LOG.warning(
                "apollo_reference_question_apology_persist_failed session_id=%s attempt_id=%s",
                sess.id,
                attempt_id,
                exc_info=True,
            )
        graph = await _read_graph_or_empty(
            store, attempt_id=attempt_id, stage="reference_question_failed"
        )
        return {
            "apollo_reply": _REFERENCE_QUESTION_APOLOGY,
            "kg_entries_added": 0,
            "kg": graph.model_dump(mode="json"),
        }

    next_count = current_count + 1
    sess.metadata_ = {**(sess.metadata_ or {}), ASIDE_COUNT_SESSION_METADATA_KEY: next_count}

    return await _persist_reference_aside_turn(
        db=db,
        sess=sess,
        attempt_id=attempt_id,
        message=message,
        store=store,
        result=result,
        aside_count=next_count,
        graph_stage="reference_question",
    )


async def _persist_reference_aside_turn(
    *,
    db: AsyncSession,
    sess: TutoringSession,
    attempt_id: int,
    message: str,
    store: KGStore,
    result: ReferenceAsideResult,
    aside_count: int,
    graph_stage: str,
) -> dict[str, Any]:
    """Persist and return the shared response envelope for an aside turn."""

    resume_line = (
        _REFERENCE_QUESTION_RESUME_LINE
        if result.in_scope
        else _REFERENCE_QUESTION_OUT_OF_SCOPE_RESUME_LINE
    )
    next_idx = await _next_turn_index(db, sess.id)
    db.add(
        TutoringMessage(
            session_id=sess.id,
            course_id=sess.course_id,
            attempt_id=attempt_id,
            role="student",
            content=message,
            turn_index=next_idx,
        )
    )
    db.add(
        TutoringMessage(
            session_id=sess.id,
            course_id=sess.course_id,
            attempt_id=attempt_id,
            role="apollo",
            content=result.text,
            turn_index=next_idx + 1,
            intent=ASIDE_MESSAGE_INTENT_TAG,
            # The structured payload rides with the row so the session
            # snapshot can replay the aside card WITH its citations after a
            # reload (text is `content`, so only citations/in_scope persist).
            message_metadata={
                "aside": {
                    "citations": result.citations,
                    "in_scope": result.in_scope,
                }
            },
        )
    )
    db.add(
        TutoringMessage(
            session_id=sess.id,
            course_id=sess.course_id,
            attempt_id=attempt_id,
            role="apollo",
            content=resume_line,
            turn_index=next_idx + 2,
        )
    )
    await db.commit()

    graph = await _read_graph_or_empty(store, attempt_id=attempt_id, stage=graph_stage)
    return {
        "apollo_reply": resume_line,
        "kg_entries_added": 0,
        "kg": graph.model_dump(mode="json"),
        "message_kind": MESSAGE_KIND_REFERENCE_ASIDE,
        "aside": {
            "text": result.text,
            "citations": result.citations,
            "in_scope": result.in_scope,
        },
        "intent_executed": {"intent": "reference_question", "aside_count": aside_count},
    }


async def _maybe_execute_reference_aside(
    *,
    db: AsyncSession,
    sess: TutoringSession,
    attempt_id: int,
    message: str,
    store: KGStore,
    problem: Problem,
    ask_hoot: bool,
) -> dict[str, Any] | None:
    """Execute the hint lane only for an explicit Ask Hoot request.

    Requests rejected by either rollout gate fall through to the ordinary
    teaching turn. The utterance is still classified for the other existing
    intents, but normal typed turns can never enter the aside executor.
    """
    if not ask_hoot:
        return None
    if not (_interaction4_enabled() and interaction_allowed_for_concept(problem.concept_id)):
        return None
    return await _execute_reference_question(
        db=db,
        sess=sess,
        attempt_id=attempt_id,
        message=message,
        store=store,
        problem=problem,
    )


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
    verdict = await asyncio.to_thread(
        classify_intent,
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
        store,
        attempt_id=attempt_id,
        stage="maybe_intent_confirmation",
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
    ask_hoot: bool = False,
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
    # Resolved up front so an explicit Ask Hoot request can use it for the
    # leakage-exclusion lookup before the normal teaching path.
    problem = await _find_problem(
        db, sess.concept_id, sess.current_problem_id, course_id=sess.course_id
    )

    aside_response = await _maybe_execute_reference_aside(
        db=db,
        sess=sess,
        attempt_id=current_attempt.id,
        message=message,
        store=store,
        problem=problem,
        ask_hoot=ask_hoot,
    )
    if aside_response is not None:
        return aside_response

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
    # Done-race fix (P0.3): persist the student message NOW, before the long
    # parse + questioning LLM chain (10-17s), so a Done clicked mid-turn
    # grades a transcript that already contains it. `history_pre` was loaded
    # above, so the transcript handed to the question planner below does not
    # double-count this message. Apollo's reply is appended at the end of the
    # turn by `_persist_apollo_reply`.
    student_turn_index = await _persist_student_message(
        db,
        session_id=session_id,
        course_id=sess.course_id,
        attempt_id=int(current_attempt.id),
        content=message,
    )

    # Cross-turn linking (WU-2B): read the CURRENT subgraph (everything taught
    # so far this attempt — the new turn's nodes aren't written until after
    # parsing) and project it into a GraphContext the parser threads in so it
    # can emit edges referencing prior-turn node ids.
    prior_graph = await _read_graph_or_empty(
        store,
        attempt_id=current_attempt.id,
        stage="prior_graph",
    )
    graph_context = build_graph_context(prior_graph)
    try:
        nodes, edges = await asyncio.to_thread(
            parse_utterance,
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
        store,
        attempt_id=current_attempt.id,
        stage="student_graph",
    )
    # The question ledger's turn bookkeeping keys off the STUDENT message's
    # index — before P0.3 this was computed here (the index the pair-persist
    # would assign); the early persist already fixed that same value.
    next_idx = student_turn_index

    # One-call reference-driven question controller. The same model assesses
    # the full student transcript and writes Apollo's answer-safe next reply.
    # The opportunity ledger still caps each reference node at one question;
    # when no eligible target remains, grade automatically.
    full_transcript = [
        ("student" if item["role"] == "user" else "apollo", item["content"]) for item in history_pre
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

    await _persist_apollo_reply(
        db,
        session_id=session_id,
        course_id=sess.course_id,
        attempt_id=int(current_attempt.id),
        apollo_msg=validated,
    )
    if decision.action == "done":
        from apollo.handlers.done import handle_done  # noqa: PLC0415

        # This Done was decided by the questioning engine (budget exhaustion /
        # coverage-sufficient), not clicked by the student — stamp it for
        # grade forensics (bimodal-fix P0.4).
        done_result = await handle_done(db=db, neo=neo, session_id=session_id, auto_done=True)
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
