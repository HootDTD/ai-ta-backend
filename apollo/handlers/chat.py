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

from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.agent.apollo_llm import draft_reply
from apollo.agent.output_filter import validate_or_raise
from apollo.errors import FilterRejectedError
from apollo.handlers.history import load_windowed_history
from apollo.handlers.intent import (
    INTENT_CONFIDENCE_THRESHOLD,
    classify_intent,
    confirmation_prompt_for,
    detect_confirmation,
)
from apollo.handlers.olm_invite import (
    OlmInviteSignal,
    decide_invite,
    find_low_conf_new_nodes,
    is_enabled as olm_invite_is_enabled,
    signal_to_metadata as olm_invite_signal_to_metadata,
)
from apollo.knowledge_graph.store import KGStore
from apollo.ontology import KGGraph
from apollo.overseer.misconception import (
    MisconceptionSignal,
    infer_misconception,
    is_enabled as misconception_is_enabled,
)
from apollo.overseer.problem_selector import (
    cluster_to_concept,
    list_problems_for_cluster,
)
from apollo.parser.parser_llm import parse_utterance
from apollo.persistence.models import ApolloSession, Message, ProblemAttempt
from apollo.persistence.neo4j_client import Neo4jClient
from apollo.schemas.problem import Problem
from apollo.solver.sufficiency import SufficiencyVerdict, check_sufficiency
from apollo.subjects import ConceptDefinition, load_concept


def _find_problem(cluster_id: str, problem_id: str) -> Problem:
    """Locate a problem in the bank by cluster + id. Mirrors done.py's helper.
    Kept inline rather than hoisted into problem_selector to keep that
    module's contract (problem listing) narrow."""
    for p in list_problems_for_cluster(cluster_id):
        if p.id == problem_id:
            return p
    raise RuntimeError(
        f"problem {problem_id!r} not in bank for cluster {cluster_id!r}"
    )


def _build_sufficiency_verdict(
    *,
    student_graph: KGGraph,
    problem: Problem,
    concept: ConceptDefinition,
    attempt_id: int,
) -> SufficiencyVerdict:
    """Run the per-turn sufficiency check.

    Mirrors the augmentation logic in `done.py::handle_done` so the
    per-turn verdict and the final coverage call evaluate the same
    augmented system. The only LLM-free signal Apollo gets per turn —
    no network call, sub-100ms cost.

    Soft-fails the whole chain to a low-confidence `insufficient` verdict
    on any internal error, so a malformed equation never blocks the
    chat turn.
    """
    try:
        solver_kg = {
            "equation": [
                n.content.model_dump() for n in student_graph.by_type("equation")
            ],
        }
        augmented_givens = dict(problem.given_values)
        for k, v in concept.solver_hints.augmented_givens.items():
            augmented_givens.setdefault(k, v)
        for ref in problem.reference_solution:
            if ref.entry_type == "simplification":
                aw = (ref.content.get("applies_when") or "").lower().replace(" ", "")
                if "h1==h2" in aw:
                    augmented_givens.setdefault("h1", 0.0)
                    augmented_givens.setdefault("h2", 0.0)

        reference_graph = problem.to_kg_graph(attempt_id=attempt_id)
        return check_sufficiency(
            kg=solver_kg,
            problem={
                "id": problem.id,
                "given_values": augmented_givens,
                "target_unknown": problem.target_unknown,
            },
            reference_graph=reference_graph,
        )
    except Exception:  # noqa: BLE001 - sufficiency must never break chat
        return SufficiencyVerdict(state="insufficient", confidence=0.0)


def _signal_to_metadata(signal: MisconceptionSignal) -> dict:
    """Serialize a MisconceptionSignal for persistence in Message.metadata.
    Strips internal-only fields (description, bank_id) so the JSONB row
    is safe to surface in any future analytics view that joins messages.
    The bank_code is preserved — it's the durable handle the
    PROBE-then-confirm gate uses to recognize the same misconception
    across turns."""
    return {
        "fired": signal.fired,
        "state": signal.state,
        "bank_code": signal.bank_code,
        "confidence": signal.confidence,
    }


def _metadata_to_signal(payload: dict | None) -> MisconceptionSignal | None:
    """Reconstitute a previous-turn MisconceptionSignal from JSONB. Only
    the fields that drive the PROBE-then-confirm gate are needed —
    description / probe / rt_steps are not carried back."""
    if not isinstance(payload, dict) or "misconception" not in payload:
        return None
    raw = payload["misconception"]
    if not isinstance(raw, dict):
        return None
    state = raw.get("state", "default")
    if state not in {"default", "probe", "socratic"}:
        return None
    return MisconceptionSignal(
        fired=bool(raw.get("fired", False)),
        state=state,  # type: ignore[arg-type]
        bank_code=raw.get("bank_code"),
        confidence=float(raw.get("confidence", 0.0) or 0.0),
    )


async def _load_previous_signals(
    db: AsyncSession, *, session_id: int, k: int = 2,
) -> tuple[MisconceptionSignal, ...]:
    """Read the last `k` Apollo-side signals from message metadata.
    Used by the PROBE-then-confirm gate so the same code must surface
    twice before escalating to socratic."""
    rows = (await db.execute(
        select(Message.message_metadata)
        .where(Message.session_id == session_id)
        .where(Message.role == "apollo")
        .order_by(Message.turn_index.desc())
        .limit(k)
    )).scalars().all()
    out: list[MisconceptionSignal] = []
    for payload in rows:
        sig = _metadata_to_signal(payload)
        if sig is not None:
            out.append(sig)
    return tuple(out)


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
    db.add(Message(
        session_id=session_id,
        attempt_id=attempt_id,
        role="student",
        content=student_msg,
        turn_index=next_idx,
    ))
    db.add(Message(
        session_id=session_id,
        attempt_id=attempt_id,
        role="apollo",
        content=apollo_msg,
        turn_index=next_idx + 1,
    ))
    await db.commit()


async def _handle_pending_done(
    *,
    db: AsyncSession,
    neo: Neo4jClient,
    sess: ApolloSession,
    attempt_id: int,
    message: str,
    store: KGStore,
) -> Dict[str, Any] | None:
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
    history: list[Dict[str, str]],
    concept,
    store: KGStore,
) -> Dict[str, Any] | None:
    """If the new utterance classifies as a non-teaching intent above the
    confidence threshold, persist a confirmation turn and return a
    chat-shaped response. Otherwise return None and let the caller fall
    through to teaching."""
    verdict = classify_intent(
        utterance=message, history=history, concept=concept,
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
) -> Dict[str, Any]:
    store = KGStore(db, neo)

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

    subject_id, concept_id = cluster_to_concept(sess.concept_cluster_id)
    concept = load_concept(subject_id, concept_id)

    # ---- Intent state machine (item #5) -------------------------------
    # Step 1: if a pending intent exists, see if this turn confirms it.
    if sess.pending_intent == "done":
        result = await _handle_pending_done(
            db=db, neo=neo, sess=sess,
            attempt_id=current_attempt.id, message=message, store=store,
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
        db=db, sess=sess,
        attempt_id=current_attempt.id, message=message,
        history=history_pre, concept=concept, store=store,
    )
    if intent_response is not None:
        return intent_response

    # ---- Normal teaching path -----------------------------------------
    nodes, edges = parse_utterance(
        message,
        concept=concept,
        attempt_id=current_attempt.id,
    )
    nodes_added = await store.write_nodes(
        attempt_id=current_attempt.id, nodes=nodes, source="parser",
    )
    # Edges must be written AFTER nodes — the MATCH...CREATE pattern needs
    # both endpoints to exist at write time.
    await store.write_edges(
        attempt_id=current_attempt.id, edges=edges, source="parser",
    )

    history_summary, raw_window = await load_windowed_history(
        db=db, session=sess,
    )

    # P3.5 — OLM invite trigger. Computed BEFORE persisting the student
    # turn so `_count_past_low_conf_patterns` reads the prior state of
    # the session (the current low-conf flag rides on this turn's row,
    # which we add below).
    new_low_conf_nodes = find_low_conf_new_nodes(nodes)
    olm_invite_signal = await decide_invite(
        db=db,
        session_id=session_id,
        new_low_conf_nodes=new_low_conf_nodes,
    )

    # The (student, apollo) turn pair is persisted together AFTER the draft
    # clears the output filter — see the tail of this function. Deferring the
    # student write means a FilterRejectedError leaves no orphaned student turn
    # (a committed student row with no Apollo reply, which would desync the
    # history and the FE). The student turn's analytics flag is computed now
    # (it rides on the olm_invite signal) and applied at persist time.
    #
    # `low_conf_pattern` rides on every student turn, regardless of whether the
    # master flag is enabled — we need the data to calibrate the threshold
    # before flipping the flag globally.
    student_metadata = (
        {"low_conf_pattern": True}
        if olm_invite_signal.low_conf_pattern_this_turn else None
    )

    # The new student message goes through the LLM as the latest turn
    # without re-loading the full history.
    history_for_llm = raw_window + [{"role": "user", "content": message}]

    kg_summary = await store.summarize_for_apollo(attempt_id=current_attempt.id)

    # Per-turn sufficiency check (Class 2 Phase 1 / Apollo Gap D). Conditions
    # Apollo's confused-tutee voice on whether the student has taught enough
    # to solve the problem. Pure local computation — no LLM, no DB beyond
    # the read_graph that already happened.
    student_graph = await store.read_graph(attempt_id=current_attempt.id)
    problem = _find_problem(sess.concept_cluster_id, sess.current_problem_id)
    sufficiency = _build_sufficiency_verdict(
        student_graph=student_graph,
        problem=problem,
        concept=concept,
        attempt_id=current_attempt.id,
    )

    # Class 2 Phase 2: misconception inference. Runs even when the master
    # flag is off so the analytics column captures recall data while the
    # corpus is being calibrated; the persona shift only kicks in when
    # is_enabled() is true (gated inside apollo_llm and output_filter).
    # Subject-agnostic by contract: takes session.concept_id (DB FK), never
    # a subject/concept slug. When the session row pre-dates migration 018
    # (no concept_id), the call is skipped — defaults to no-op signal.
    misconception_signal: MisconceptionSignal = MisconceptionSignal.default(
        evidence="skip:no-concept-fk-on-session"
    )
    if sess.concept_id is not None:
        previous_signals = await _load_previous_signals(
            db, session_id=session_id, k=2,
        )
        try:
            misconception_signal = await infer_misconception(
                db=db,
                concept_id=int(sess.concept_id),
                utterance=message,
                parsed_nodes=nodes,
                sufficiency=sufficiency,
                previous_signals=previous_signals,
            )
        except Exception:  # noqa: BLE001 - never break chat on inference failure
            misconception_signal = MisconceptionSignal.default(
                evidence="skip:inference-error"
            )

    draft = draft_reply(
        history=history_for_llm,
        kg_summary=kg_summary,
        history_summary=history_summary,
        sufficiency=sufficiency,
        misconception=misconception_signal,
        olm_invite=olm_invite_signal,
    )

    # Reuse for the filter — it consumes the same history shape.
    history = history_for_llm

    try:
        validated = validate_or_raise(
            draft,
            concept=concept,
            history=history,
            kg_summary=kg_summary,
            sufficiency=sufficiency,
            misconception=misconception_signal,
        )
    except FilterRejectedError as exc:
        # Surface the live KG on the error so the FE refreshes "Apollo's
        # Understanding" instead of showing a stale/empty panel on a blocked
        # turn. The parsed nodes were already written this turn — they are
        # real taught content even though Apollo's reply was withheld.
        exc.kg = student_graph.model_dump(mode="json")
        raise

    # Persist the student + apollo turn pair together now that the reply has
    # cleared the filter. Single commit, contiguous turn indices.
    next_idx = await _next_turn_index(db, session_id)
    db.add(Message(
        session_id=session_id,
        attempt_id=current_attempt.id,
        role="student",
        content=message,
        turn_index=next_idx,
        message_metadata=student_metadata,
    ))
    db.add(Message(
        session_id=session_id,
        attempt_id=current_attempt.id,
        role="apollo",
        content=validated,
        turn_index=next_idx + 1,
        message_metadata={
            "misconception": _signal_to_metadata(misconception_signal),
            # P3.5: persist the invite outcome so the next turn's cooldown
            # check has the timestamp to read.
            "olm_invite": olm_invite_signal_to_metadata(olm_invite_signal),
        },
    ))
    await db.commit()

    # Reuse the student_graph read above — no KG writes happened between
    # the verdict and here, so re-reading would just round-trip Neo4j.
    return {
        "apollo_reply": validated,
        "kg_entries_added": nodes_added,
        "kg": student_graph.model_dump(mode="json"),
        # Class 2 Phase 1: surface the verdict in chat metadata so FE /
        # offline eval can correlate Apollo's tone with the signal that
        # produced it. Internal — not user-visible until P3 lands the
        # OLM Done-gate that consumes it.
        "sufficiency": {
            "state": sufficiency.state,
            "missing_variables": list(sufficiency.missing_variables),
            "missing_kg_nodes": list(sufficiency.missing_kg_nodes),
            "next_premise_hint": sufficiency.next_premise_hint,
            "confidence": sufficiency.confidence,
        },
        # Class 2 Phase 2: misconception signal envelope. Only the
        # analytics-safe fields are returned. The internal description
        # and bank_id are NEVER surfaced. Persona shift is invisible
        # per the research synthesis — there is no UI marker tied to
        # this payload; it exists for offline eval and the FE rubric panel.
        "misconception": {
            "fired": misconception_signal.fired,
            "state": misconception_signal.state,
            "bank_code": misconception_signal.bank_code,
            "confidence": misconception_signal.confidence,
            "enabled": misconception_is_enabled(),
        },
        # P3.5: OLM clarification invite. When fired=True, the FE pulses
        # the entry pill identified by entry_id under the chat reply.
        # When the master flag is off, fired stays False even if the
        # underlying pattern was detected; the analytics layer reads the
        # message metadata directly.
        "olm_invite": {
            "fired": olm_invite_signal.fired,
            "entry_id": olm_invite_signal.entry_id,
            "summary": olm_invite_signal.summary,
            "enabled": olm_invite_is_enabled(),
        },
    }
