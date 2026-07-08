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
import os
from collections.abc import Sequence
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
from apollo.clarification.v2_config import clarification_v2_ranker_enabled
from apollo.graph_compare import build_reference_canonical
from apollo.graph_compare.soundness import is_misconception_key
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
from apollo.resolution.resolver import resolve_attempt
from apollo.resolver_v2.config import grayzone_enabled, load_params, resolver_v2_enabled
from apollo.resolver_v2.grayzone import main_chat_grayzone
from apollo.resolver_v2.incremental import score_turn
from apollo.resolver_v2.incremental import seed as _seed_incremental_state
from apollo.resolver_v2.incremental_types import IncrementalSnapshot, IncrementalState
from apollo.resolver_v2.integration import load_student_turns
from apollo.resolver_v2.nli_provider import get_adjudicator
from apollo.resolver_v2.prefilter import select_windows
from apollo.resolver_v2.views import build_ref_nodes, load_views
from apollo.schemas.problem import Problem
from apollo.subjects.curriculum_db import load_concept_definition

_LOG = logging.getLogger(__name__)

_CLARIFICATION_CACHE = CandidateEmbeddingCache()

# The live clarification loop flag (default OFF everywhere, incl. prod + staging). When OFF,
# handle_chat is byte-identical to the pre-clarification path: the block is skipped, no
# extra LLM/embedding round-trips, draft_reply gets clarification_hints=None. Flip ON only
# after rollout/cost review (same posture as APOLLO_GRAPH_SIM_* in done.py).
_CLARIFICATION_ENABLED_FLAG: str = "APOLLO_CLARIFICATION_ENABLED"
# Per-turn NLI node budget: when more than this many nodes are parsed in a
# single utterance, NLI is skipped for that turn (degrades to lexical-only).
# Synchronous model inference runs per residual node, so uncapped utterances
# can block the event loop under load.  Raise the cap only after profiling.
_NLI_CHAT_NODE_CAP_FLAG: str = "APOLLO_NLI_CHAT_MAX_NODES"
_NLI_CHAT_NODE_CAP_DEFAULT: int = 15


def _clarification_enabled() -> bool:
    return os.environ.get(_CLARIFICATION_ENABLED_FLAG, "").lower() in ("1", "true", "yes")


def _nli_chat_node_cap() -> int:
    """Read ``APOLLO_NLI_CHAT_MAX_NODES`` from env; default 15 on missing or malformed."""
    raw = os.environ.get(_NLI_CHAT_NODE_CAP_FLAG)
    try:
        return int(raw) if raw is not None else _NLI_CHAT_NODE_CAP_DEFAULT
    except (ValueError, TypeError):
        return _NLI_CHAT_NODE_CAP_DEFAULT


def _v2_ranker_active() -> bool:
    """H1 gating predicate for the ENTIRE background V2-incremental kick
    (integration spec §2.2/§8.2): only when ALL THREE flags -- clarification,
    resolver_v2, and the v2 ranker -- are ON does chat.py start a background
    thread, mutate session state, or spend any NLI budget. Fresh-read, no
    caching. Mirrors ``apollo.clarification.turn._v2_ranker_active`` exactly
    (duplicated rather than imported: that name is module-private there, and
    turn.py already imports this module's sibling collaborators)."""
    return (
        clarification_v2_ranker_enabled()
        and resolver_v2_enabled()
        and _clarification_enabled()
    )


def _confirmed_seed_keys(outcomes: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """B-HIGH-2 / spec §6: the V2 seed path is gated ONLY on a content-verified
    `confirmed` outcome (see the precondition pinned on
    ``rescorer.default_clarification_judge``) -- never on `refuted` or `vague`,
    and never on a bare student self-report. Explicitly asserts the gate on
    every key it selects rather than trusting the filter alone."""
    keys: list[str] = []
    for candidate_key, outcome in outcomes:
        if outcome != "confirmed":
            continue
        assert outcome == "confirmed"  # seed path MUST be gated on this outcome
        keys.append(candidate_key)
    return tuple(keys)


def _empty_incremental_state() -> IncrementalState:
    """The cold-start / turn-1 state (spec §5.1): no windows scored yet, no
    running credit, no budget spent."""
    return IncrementalState(
        window_cursor=0,
        global_window_count=0,
        running_node_max={},
        node_source={},
        running_edge_evidence={},
        seeded_keys=frozenset(),
        pair_count_total=0,
    )


class _IncrementalHolder:
    """Process-local, per-attempt accelerator for the V2 incremental score
    (spec §5.1). Session state, NOT the DB -- intra-worker only, never shared
    across Railway replicas; a cold worker simply has no completed snapshot
    and falls back to v1 (fail-open, not a correctness mechanism).

    Single writer per attempt: at most one incremental job is in flight per
    ``attempt_id`` at a time (``try_acquire``/``release``), so the
    read-modify-write in ``write`` is always serialized for a given attempt.
    ``write`` is additionally monotone-guarded: a state whose
    ``window_cursor`` is not strictly greater than the one already stored is
    discarded (defensive against reordering).
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._entries: dict[int, dict[str, Any]] = {}

    def _entry(self, attempt_id: int) -> dict[str, Any]:
        entry = self._entries.get(attempt_id)
        if entry is not None:
            return entry
        entry = {"state": None, "snapshot": None, "in_flight": False}
        self._entries[attempt_id] = entry
        if len(self._entries) > self._maxsize:
            for oldest_key in self._entries:
                if oldest_key != attempt_id:
                    self._entries.pop(oldest_key, None)
                break
        return entry

    def latest_snapshot(self, attempt_id: int) -> IncrementalSnapshot | None:
        return self._entry(attempt_id)["snapshot"]

    def latest_state(self, attempt_id: int) -> IncrementalState | None:
        return self._entry(attempt_id)["state"]

    def try_acquire(self, attempt_id: int) -> bool:
        """One-in-flight-per-attempt (spec §5.5 M1/M2): returns True iff this
        call claimed the slot (no prior job for this attempt is still
        running); returns False when a prior job is still in flight, in
        which case the caller must kick nothing this turn."""
        entry = self._entry(attempt_id)
        if entry["in_flight"]:
            return False
        entry["in_flight"] = True
        return True

    def release(self, attempt_id: int) -> None:
        self._entry(attempt_id)["in_flight"] = False

    def write(
        self,
        attempt_id: int,
        new_state: IncrementalState,
        snapshot: IncrementalSnapshot,
    ) -> None:
        entry = self._entry(attempt_id)
        prior_state: IncrementalState | None = entry["state"]
        if prior_state is not None and new_state.window_cursor <= prior_state.window_cursor:
            return
        if prior_state is not None:
            missed_seeds = prior_state.seeded_keys - new_state.seeded_keys
            if missed_seeds:
                # A `confirmed` clarification called `seed_state` (below) for
                # these keys AFTER this now-completing job was kicked, so the
                # state the job started from (and its own `new_state`)
                # predates the seed. Reapply the freeze on top of the
                # completed state so a content-verified seed is never
                # silently lost across a stale in-flight job (§6 guarantee)
                # -- this mirrors `seed_state`'s own freeze exactly (full
                # credit + seeded_keys), it just also folds in this job's
                # otherwise-current running scores/edges.
                new_state = _seed_incremental_state(new_state, sorted(missed_seeds))
        entry["state"] = new_state
        entry["snapshot"] = snapshot

    def seed_state(self, attempt_id: int, keys: Sequence[str]) -> None:
        """Freeze ``keys`` into this attempt's persisted state (spec §6,
        T12): a content-verified `confirmed` clarification outcome calls this
        so the background job kicked later THIS SAME TURN
        (``_maybe_kick_incremental_v2``) starts from a state where those keys
        are already ``running_node_max=1.0`` / ``seeded_keys`` -- the next
        completed snapshot reflects the freeze (and the seeded node's own
        edge-credit lift) naturally via the normal ``score_turn`` recompute.
        No-op on an empty ``keys`` (never touches the holder when nothing was
        confirmed this turn).

        Unlike ``write``, this does NOT gate on ``window_cursor`` -- seeding
        is an out-of-band credit grant, not a new window score, so it must
        apply even when no job has completed yet (cold worker / turn 1) and
        must never regress an existing state's cursor. The stored snapshot is
        left untouched; only the next completed job replaces it.
        """
        if not keys:
            return
        entry = self._entry(attempt_id)
        state = entry["state"] or _empty_incremental_state()
        entry["state"] = _seed_incremental_state(state, keys)


_INCREMENTAL_HOLDER = _IncrementalHolder()
# Keeps a strong reference to fire-and-forget background tasks so they are
# never garbage-collected mid-flight; each task discards itself on completion.
_INCREMENTAL_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _v1_resolved_keys_for_turn(student_graph, candidates, symbolic_mappings) -> frozenset[str]:
    """The turn's v1 resolution (spec §5.3: "v1_resolved_keys ... IS
    available at turn time"), reusing the existing (non-misconception) v1
    resolver over the full taught-so-far graph rather than building a new
    matcher or a from-scratch student canonical inline (A-MAJOR-4 keeps the
    edge triples empty; this is the node-side counterpart that IS cheap
    enough to compute per turn). Mirrors
    ``integration.v1_inputs_from_canonical``'s node-key derivation
    (misconception keys excluded -- a resolved misconception must never floor
    a reference node's credit)."""
    result = resolve_attempt(student_graph, candidates, symbolic_mappings=symbolic_mappings)
    return frozenset(
        node.resolved_key
        for node in result.resolved
        if node.resolution == "resolved"
        and node.resolved_key is not None
        and not is_misconception_key(node.resolved_key)
    )


async def _run_incremental_v2_job(
    *,
    attempt_id: int,
    state: IncrementalState,
    all_student_turns: tuple[str, ...],
    reference_graph,
    problem_payload: dict,
    v1_resolved_keys: frozenset[str],
    ref_nodes,
    params,
    nli,
    grayzone_fn,
) -> None:
    """The background per-turn incremental V2 score (spec §3.1/§5.5).

    Fire-and-forget: nothing in ``handle_chat``'s reply path ever awaits this
    coroutine, so an exception here can NEVER propagate into the reply (H3).
    On success the new state + completed snapshot are persisted to the
    process-local holder (single-writer, monotone-guarded); on any exception
    the holder is left at its last good value (prior snapshot, or none) and
    the failure is logged.
    """
    try:
        new_state, snapshot = await asyncio.to_thread(
            score_turn,
            state,
            all_student_turns=all_student_turns,
            reference_graph=reference_graph,
            problem_payload=problem_payload,
            v1_resolved_keys=v1_resolved_keys,
            nli=nli,
            grayzone_fn=grayzone_fn,
            select_fn=select_windows,
            params=params,
            ref_nodes=ref_nodes,
        )
        _INCREMENTAL_HOLDER.write(attempt_id, new_state, snapshot)
    except Exception as exc:  # noqa: BLE001 - H3: never let this escape; keep last-good snapshot
        _LOG.warning(
            "clarification_v2_incremental_failed attempt_id=%s exception_class=%s",
            attempt_id,
            type(exc).__name__,
        )
    finally:
        _INCREMENTAL_HOLDER.release(attempt_id)


async def _maybe_kick_incremental_v2(
    db: AsyncSession,
    *,
    attempt_id: int,
    message: str,
    student_graph,
    candidates,
    symbolic_mappings,
    problem_payload: dict,
) -> IncrementalSnapshot | None:
    """Caller must have already checked ``_v2_ranker_active()`` (H1) -- this
    function assumes the gate is ON.

    Returns the most-recent COMPLETED snapshot (never the current turn's --
    spec §5.5: the reply must never await/block on this turn's job). Kicks a
    new background job only when no prior job for this attempt is still
    running (one-in-flight-per-attempt, M1/M2) -- a still-running job just
    means this turn kicks nothing, and the running job's result becomes
    available to a later turn. Any setup failure here is caught and logged
    (H3); the prior snapshot is returned unchanged and teaching is never
    blocked.
    """
    prior_snapshot = _INCREMENTAL_HOLDER.latest_snapshot(attempt_id)

    if not _INCREMENTAL_HOLDER.try_acquire(attempt_id):
        return prior_snapshot

    try:
        state = _INCREMENTAL_HOLDER.latest_state(attempt_id) or _empty_incremental_state()
        prior_turns = await load_student_turns(db, attempt_id)
        all_student_turns = tuple(prior_turns) + (message,)
        v1_resolved_keys = _v1_resolved_keys_for_turn(student_graph, candidates, symbolic_mappings)
        reference_graph = build_reference_canonical(problem_payload)
        concept_id = str(problem_payload.get("concept_id") or "")
        problem_id = str(problem_payload.get("id") or "")
        views_by_key = load_views(concept_id, problem_id)
        ref_nodes = build_ref_nodes(reference_graph, problem_payload, views_by_key)
        params = load_params()
        nli = get_adjudicator()
        grayzone_fn = main_chat_grayzone if grayzone_enabled() else None
    except Exception as exc:  # noqa: BLE001 - H3: setup failure must never block teaching
        _LOG.warning(
            "clarification_v2_incremental_failed attempt_id=%s exception_class=%s",
            attempt_id,
            type(exc).__name__,
        )
        _INCREMENTAL_HOLDER.release(attempt_id)
        return prior_snapshot

    task = asyncio.create_task(
        _run_incremental_v2_job(
            attempt_id=attempt_id,
            state=state,
            all_student_turns=all_student_turns,
            reference_graph=reference_graph,
            problem_payload=problem_payload,
            v1_resolved_keys=v1_resolved_keys,
            ref_nodes=ref_nodes,
            params=params,
            nli=nli,
            grayzone_fn=grayzone_fn,
        )
    )
    _INCREMENTAL_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_INCREMENTAL_BACKGROUND_TASKS.discard)

    return prior_snapshot


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
                clarification_outcomes = await resolve_pending_clarifications(
                    db=db,
                    attempt_id=current_attempt.id,
                    student_message=message,
                    candidates=inputs.candidates,
                    judge=default_clarification_judge,
                    answered_turn=next_idx,
                )

                # ---- V2 feedback seeding (spec §6, T12). Gated on BOTH the H1
                # ranker-active predicate (no session-state mutation at all when
                # OFF) AND, within that, only `confirmed` outcomes (B-HIGH-2 --
                # see the precondition pinned on
                # rescorer.default_clarification_judge). `refuted`/`vague` are
                # never seeded: the node stays in the unresolved pool, but its
                # topic key is already asked so the existing dedup prevents a
                # re-probe. Seeding only freezes the node itself -- it must NOT
                # be read by chat.py as a signal to resolve any OTHER node (M5).
                if _v2_ranker_active():
                    _seed_keys = _confirmed_seed_keys(clarification_outcomes)
                    if _seed_keys:
                        _INCREMENTAL_HOLDER.seed_state(current_attempt.id, _seed_keys)

                # ---- V2 incremental score (H1: gated on ALL THREE flags). When
                # OFF (including the RESOLVER_V2=ON/RANKER=OFF row), this branch
                # never runs: no background thread starts, no session state is
                # mutated, no NLI budget is spent (integration spec §2.2/§8.2).
                # When ON, this kicks a background job for the NEXT turn's
                # selection and returns the MOST-RECENT COMPLETED snapshot from a
                # prior turn (never this turn's — §5.5, the reply never awaits
                # it). Turn 1 / a cold worker -> no completed snapshot -> v1.
                v2_snapshot: IncrementalSnapshot | None = None
                if _v2_ranker_active():
                    v2_snapshot = await _maybe_kick_incremental_v2(
                        db,
                        attempt_id=current_attempt.id,
                        message=message,
                        student_graph=student_graph,
                        candidates=inputs.candidates,
                        symbolic_mappings=inputs.symbolic_mappings,
                        problem_payload=problem_payload,
                    )

                # NLI context (budget-gated): reuse done_grading's process
                # singleton so the transformer model loads once per process.
                # Per-turn cost is synchronous model inference per residual
                # node; cap node count via APOLLO_NLI_CHAT_MAX_NODES (default
                # 15) to avoid blocking the event loop for large utterances.
                from apollo.handlers import done_grading as _dg  # noqa: PLC0415

                _nli_ctx = _dg._nli_context()
                _nli_cap = _nli_chat_node_cap()
                if _nli_ctx is not None and len(nodes) > _nli_cap:
                    _LOG.info(
                        "nli_chat_skipped_budget nodes=%d cap=%d session_id=%s",
                        len(nodes),
                        _nli_cap,
                        session_id,
                    )
                    _nli_ctx = None
                # H1: only reads (never creates/mutates) the holder's entry when the
                # ranker is fully active -- mirrors the `_maybe_kick_incremental_v2`
                # gate above so a gate-OFF turn leaves `_INCREMENTAL_HOLDER._entries`
                # untouched (`latest_state` lazily creates an entry on first access).
                _incr_state = (
                    _INCREMENTAL_HOLDER.latest_state(current_attempt.id)
                    if _v2_ranker_active()
                    else None
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
                    nli_ctx=_nli_ctx,
                    snapshot=v2_snapshot,
                    # Trace-only (spec §7, T13): _maybe_kick_incremental_v2 never
                    # returns THIS turn's job result (fire-and-forget, §5.5), so a
                    # non-None snapshot here is always a prior turn's completed
                    # score. pair_count_total/seeded_keys mirror the process-local
                    # holder's current state for the attempt, defaulting to the
                    # cold-start values when no state has been written yet.
                    snapshot_source="prior_turn",
                    pair_count_total=(
                        _incr_state.pair_count_total if _incr_state is not None else 0
                    ),
                    seeded_keys=(
                        _incr_state.seeded_keys if _incr_state is not None else frozenset()
                    ),
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
