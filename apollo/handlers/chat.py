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

from apollo.agent.apollo_llm import draft_reply
from apollo.clarification import CandidateEmbeddingCache, default_embedder
from apollo.clarification.candidate_assembly import load_problem_candidates
from apollo.clarification.leak_guard import guard_clarification_reply
from apollo.clarification.rescorer import default_clarification_judge
from apollo.clarification.resolve_turn import resolve_pending_clarifications
from apollo.clarification.turn import run_clarification_detection
from apollo.handlers.done_inputs import _find_problem_payload
from apollo.handlers.history import load_windowed_history
from apollo.handlers.intent import (
    INTENT_CONFIDENCE_THRESHOLD,
    classify_intent,
    confirmation_prompt_for,
    detect_confirmation,
)
from apollo.knowledge_graph.store import KGStore
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.parser.graph_context import build_graph_context
from apollo.parser.parser_llm import parse_utterance
from apollo.persistence.models import ApolloSession, Message, ProblemAttempt
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.schemas.problem import Problem
from apollo.subjects.curriculum_db import load_concept_definition

_LOG = logging.getLogger(__name__)

_CLARIFICATION_CACHE = CandidateEmbeddingCache()

# The live clarification loop flag (default OFF everywhere, incl. prod + staging). When OFF,
# handle_chat is byte-identical to the pre-clarification path: the block is skipped, no
# extra LLM/embedding round-trips, draft_reply gets clarification_hints=None. Flip ON only
# after rollout/cost review (same posture as APOLLO_GRAPH_SIM_* in done.py).
_CLARIFICATION_ENABLED_FLAG: str = "APOLLO_CLARIFICATION_ENABLED"


def _clarification_enabled() -> bool:
    return os.environ.get(_CLARIFICATION_ENABLED_FLAG, "").lower() in ("1", "true", "yes")


async def _find_problem(db: AsyncSession, concept_id: int, problem_code: str) -> Problem:
    """Locate a problem in the DB bank by concept_id + problem_code. Mirrors
    done.py's helper. Kept inline rather than hoisted into problem_selector to
    keep that module's contract (problem listing) narrow."""
    for p in await list_problems_for_concept(db, concept_id=concept_id):
        if p.id == problem_code:
            return p
    raise RuntimeError(f"problem {problem_code!r} not in bank for cluster {concept_id!r}")


async def _next_turn_index(db: AsyncSession, session_id: int) -> int:
    result = await db.execute(
        select(Message.turn_index)
        .where(Message.session_id == session_id)
        .order_by(Message.turn_index.desc())
        .limit(1)
    )
    latest = result.scalar_one_or_none()
    return (latest + 1) if latest is not None else 0


async def _load_history(db: AsyncSession, session_id: int) -> list[dict[str, str]]:
    result = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.turn_index)
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
    attempt_id: int,
    student_msg: str,
    apollo_msg: str,
) -> None:
    """Append the (student, apollo) turn pair atomically."""
    next_idx = await _next_turn_index(db, session_id)
    db.add(
        Message(
            session_id=session_id,
            attempt_id=attempt_id,
            role="student",
            content=student_msg,
            turn_index=next_idx,
        )
    )
    db.add(
        Message(
            session_id=session_id,
            attempt_id=attempt_id,
            role="apollo",
            content=apollo_msg,
            turn_index=next_idx + 1,
        )
    )
    await db.commit()


async def _handle_pending_done(
    *,
    db: AsyncSession,
    neo: Neo4jClient,
    sess: ApolloSession,
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
        attempt_id=attempt_id,
        student_msg=message,
        apollo_msg=apollo_reply,
    )

    graph = await store.read_graph(attempt_id=attempt_id)
    return {
        "apollo_reply": apollo_reply,
        "kg_entries_added": 0,
        "kg": graph.model_dump(mode="json"),
        "intent_executed": {"intent": "done", "result": done_result},
    }


async def _maybe_intent_confirmation(
    *,
    db: AsyncSession,
    sess: ApolloSession,
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
        attempt_id=attempt_id,
        student_msg=message,
        apollo_msg=prompt,
    )
    graph = await store.read_graph(attempt_id=attempt_id)
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
    neo: Neo4jClient,
    session_id: int,
    message: str,
) -> dict[str, Any]:
    store = KGStore(db, neo)

    sess = (
        await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))
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

    concept = await load_concept_definition(db, concept_id=sess.concept_id)

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
    history_pre = await _load_history(db, session_id)
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
    prior_graph = await store.read_graph(attempt_id=current_attempt.id)
    graph_context = build_graph_context(prior_graph)
    nodes, edges = parse_utterance(
        message,
        concept=concept,
        attempt_id=current_attempt.id,
        graph_context=graph_context,
    )
    # write_nodes de-dups cross-turn re-assertions by id (WU-2B): a node whose
    # id already exists is reused, not re-minted, so the returned count is the
    # genuinely-new entries only.
    nodes_added = await store.write_nodes(
        attempt_id=current_attempt.id,
        nodes=nodes,
        source="parser",
    )
    # Edges must be written AFTER nodes — the CREATE needs both endpoints to
    # exist. write_edges validates endpoint existence + EDGE_ALLOWED_PAIRS and
    # logs any drop/invalid (no silent drop); the structured log is the
    # observable, so the result is intentionally not captured here.
    await store.write_edges(
        attempt_id=current_attempt.id,
        edges=edges,
        source="parser",
    )

    history_summary, raw_window = await load_windowed_history(
        db=db,
        session=sess,
    )

    # v1 (diff-at-Done): per turn = nodify + dumb reply. No sufficiency,
    # misconception, OLM-invite, or output filter. Apollo is fed only the
    # student's own KG + the problem, so it cannot leak an un-taught concept
    # (structural anti-leak replaces the deleted filter).
    student_graph = await store.read_graph(attempt_id=current_attempt.id)
    problem = await _find_problem(db, sess.concept_id, sess.current_problem_id)
    kg_summary = await store.summarize_for_apollo(attempt_id=current_attempt.id)
    history_for_llm = raw_window + [{"role": "user", "content": message}]

    # next_idx is needed before the clarification block (asked_turn = next_idx + 1).
    next_idx = await _next_turn_index(db, session_id)

    # ---- Clarification loop: detect ambiguous residual ideas, weave answer-blind
    # probes into Apollo's reply (spec §6). Gated (default OFF) + fail-safe — never blocks
    # teaching. A savepoint isolates the clarification writes so a DB fault here cannot poison
    # the outer transaction that commits the message pair below.
    clarification_hints: list[str] = []
    if _clarification_enabled():
        try:
            async with db.begin_nested():
                problem_payload = await _find_problem_payload(
                    db,
                    concept_id=sess.concept_id,
                    problem_code=sess.current_problem_id,
                )
                inputs = await load_problem_candidates(
                    db,
                    search_space_id=int(sess.search_space_id),
                    concept_id=sess.concept_id,
                    problem_payload=problem_payload,
                )
                await resolve_pending_clarifications(
                    db=db,
                    attempt_id=current_attempt.id,
                    student_message=message,
                    candidates=inputs.candidates,
                    judge=default_clarification_judge,
                    answered_turn=next_idx,
                )
                clarification_hints = await run_clarification_detection(
                    db=db,
                    parsed_nodes=nodes,
                    candidates=inputs.candidates,
                    symbolic_mappings=inputs.symbolic_mappings,
                    embedder=default_embedder,
                    cache=_CLARIFICATION_CACHE,
                    attempt_id=current_attempt.id,
                    session_id=session_id,
                    user_id=str(sess.user_id),
                    search_space_id=int(sess.search_space_id),
                    concept_id=sess.concept_id,
                    asked_turn=next_idx + 1,
                )
        except Exception as exc:  # noqa: BLE001 - savepoint rolled back; never block teaching
            _LOG.warning("clarification_setup_failed session_id=%s error=%s", session_id, exc)
            clarification_hints = []

    validated = draft_reply(
        history=history_for_llm,
        kg_summary=kg_summary,
        problem_text=problem.problem_text,
        history_summary=history_summary,
        clarification_hints=clarification_hints or None,
    )

    if clarification_hints:
        validated = guard_clarification_reply(
            draft=validated,
            concept=concept,
            history=history_for_llm,
            kg_summary=kg_summary,
            regenerate_without_probes=lambda: draft_reply(
                history=history_for_llm,
                kg_summary=kg_summary,
                problem_text=problem.problem_text,
                history_summary=history_summary,
            ),
        )

    # Persist the (student, apollo) pair in one commit. No filter → no
    # mid-turn rejection → no orphan risk.
    db.add(
        Message(
            session_id=session_id,
            attempt_id=current_attempt.id,
            role="student",
            content=message,
            turn_index=next_idx,
        )
    )
    db.add(
        Message(
            session_id=session_id,
            attempt_id=current_attempt.id,
            role="apollo",
            content=validated,
            turn_index=next_idx + 1,
        )
    )
    await db.commit()

    return {
        "apollo_reply": validated,
        "kg_entries_added": nodes_added,
        "kg": student_graph.model_dump(mode="json"),
    }
