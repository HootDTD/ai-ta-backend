---
doc: apollo/conversation/handlers/chat
description: apollo/handlers/chat.py — handle_chat, the full V3 teaching turn (intent gate, parse, KG write, questioning)
owns:
  - apollo/handlers/chat.py
  - apollo/handlers/__init__.py
related:
  - apollo/conversation/handlers/intent
  - apollo/conversation/handlers/done
  - apollo/conversation/hoot-bridge-reference-answer
  - apollo/conversation/parser/parser-llm
  - apollo/conversation/parser/graph-context
  - apollo/conversation/questioning/controller
  - apollo/conversation/curriculum/db
  - apollo/knowledge-graph/store
  - apollo/overseer/problem-selector
  - apollo/persistence/neo4j-client
last_verified: 2026-08-10
stub: false
---

# handlers/chat — the V3 teaching turn

`handle_chat` is `POST /apollo/sessions/{id}/chat`. `apollo/handlers/__init__.py`
is empty namespace glue riding here (§4.0.7).

## Interface

- `handle_chat(*, db, neo, session_id, message, ask_hoot=False) -> dict` — the
  only public entry (called by `routing/router`). Returns `{apollo_reply,
  kg_entries_added, kg, covered_topics, graded_topic_total, open_graded_topics,
  question_target?}`, an `intent_*` variant when the intent gate fires, or the
  `reference_aside` variant (see below) when the request explicitly sets
  `ask_hoot=true`.

## Data flow

Ordered turn:

1. Load session + latest `ProblemAttempt` + `ConceptDefinition`
   (`load_concept_definition`, `curriculum/db`) + the current `Problem`
   (`_find_problem`, hoisted to the top of `handle_chat` so the explicit
   Ask Hoot lane can use it for the leakage-exclusion lookup).
2. **Explicit hint lane**: only `ask_hoot=true` can enter
   `_maybe_execute_reference_aside`. `INTERACTION4` and the current
   `Problem.concept_id` must pass `interaction_allowed_for_concept`; otherwise
   the same utterance falls through to the normal teaching turn.
3. **Intent state machine** (`handlers/intent`): if `pending_intent == "done"`,
   `_handle_pending_done` treats this utterance as a confirmation — an affirmation
   dispatches `handle_done` (`handlers/done`, lazy import) and returns; any other
   pending intent is just cleared. Otherwise `_maybe_intent_confirmation`
   classifies the utterance and, above threshold, persists a confirmation prompt
   and returns. The classifier never triggers the hint lane.
4. **Teaching path**: FIRST persist the student message in its own commit
   (`_persist_student_message`, 2026-08-07 P0.3 done-race fix — the turn's LLM
   chain runs 10-17s and a Done clicked mid-turn used to grade a transcript
   missing the student's last message; `history_pre` is loaded before this
   persist so nothing double-counts). Then read the current subgraph
   (`_read_graph_or_empty`), project it via `build_graph_context`
   (`parser/graph-context`), then `parse_utterance` (`parser/parser-llm`).

Both LLM calls on this path — `classify_intent` (step 3) and `parse_utterance`
(step 4) — run via `await asyncio.to_thread(...)` (2026-08-04); as direct sync
calls inside this `async def` they blocked the uvicorn event loop, stalling
every other in-flight Apollo request on that worker.
5. **KG write** (`_write_kg_or_skip`): `write_nodes`/`write_edges` on `KGStore`;
   returns genuinely-new node count.
6. **Questioning**: build the full transcript and call `plan_next_question`
   (`questioning/controller`), which produces Apollo's reply + `covered_topics`.
   `_graded_topic_counts` derives the P2.2 meter from that same snapshot.
   When it decides `done`, `handle_done` is dispatched with `auto_done=True`
   (2026-08-07 P0.4 — the engine, not the student, triggered grading; the
   stamp lands in `diagnostic_report` for grade forensics).
7. `_persist_apollo_reply` appends Apollo's reply (the student row is already
   durable from step 4; the pair lands with the same consecutive indexes as
   the old pair-persist). The SHORT lanes — intent confirmations, aside
   cap/apology — still use the atomic `_persist_turn` pair (their race window
   is one classify call, not the teaching chain). The question ledger's
   `turn_index` is the student row's index, byte-identical to the pre-P0.3
   bookkeeping.

## INTERACTION4 "ask Hoot" hint lane

`_maybe_execute_reference_aside` is the sole entry to
`_execute_reference_question`:

1. The caller must set request field `ask_hoot=true`. The helper checks
   `INTERACTION4` and
   `interaction_allowed_for_concept(problem.concept_id)` before entering the
   executor. An unset/empty `INTERACTION_CONCEPTS` preserves flag-only
   behavior; either rejected rollout gate returns `None`, so the utterance
   continues through the ordinary teaching turn.
2. Empty/whitespace question → instant aside-shaped reply ("Type your
   question above first, then click Ask."), logged at INFO as
   `apollo_reference_question_empty`, persisted through the shared
   `_persist_reference_aside_turn` envelope. No LLM, no retrieval, no
   exception, and the aside counter is NOT incremented — refusing a
   non-question is correct behavior and must cost nothing.
3. Per-session cap: `sess.metadata_[ASIDE_COUNT_SESSION_METADATA_KEY]` (default
   0) at or above `MAX_ASIDES_PER_SESSION` (3) → a persona redirect turn, no
   bridge call.
4. Otherwise calls `hoot_bridge.reference_answer.answer_reference_question`.
   Any exception → logged, `db.rollback()` FIRST (the failure may have aborted
   the transaction — persisting on it escaped as a 500 in the 2026-08-01
   halfvec schema-drift incident), then the persona apology turn persisted
   best-effort (its own failure is logged, never raised) — **never a 5xx**. The
   "failure ⇒ persona apology + fall through as a teaching turn" contract lives
   here, not in the bridge (the bridge raises on genuine failure by design).
5. On success: increments the session's aside counter, then persists via the
   shared `_persist_reference_aside_turn` helper — the student question
   (untagged — the adjudicator keeps it), the aside text tagged
   `intent=ASIDE_MESSAGE_INTENT_TAG` (`handlers/done._full_transcript`
   excludes this row from grading) with the structured payload stored in the
   row's `message_metadata` as `{"aside": {citations, in_scope}}` (text is
   the row content) so `handlers/lifecycle`'s snapshot can replay citations
   after a reload, and the persona resume line (untagged). The resume line is
   scope-dependent: `in_scope=True` asks how the answer fits into the lesson
   (`_REFERENCE_QUESTION_RESUME_LINE`); `in_scope=False` (the bridge's
   out-of-scope refusal) redirects back to the teaching thread instead
   (`_REFERENCE_QUESTION_OUT_OF_SCOPE_RESUME_LINE`) — there is nothing to
   "fit in". Returns `message_kind: "reference_aside"` plus an
   `aside: {text, citations, in_scope}` payload — the serializer shape the
   student-UI types against (see `hoot-bridge-reference-answer`).

## Invariants & gotchas

- **`chat` has its OWN `_load_history`** (full per-attempt transcript, role-mapped
  student→user / apollo→assistant) — it does **not** use the vestigial
  `handlers/history`.
- **A parse miss still proceeds**: `ParserCouldNotExtractError` is caught, the
  turn contributes zero KG entries, and the conversational reply is generated
  anyway (no 422 card to the student).
- **A failed teaching turn keeps the student row** (P0.3): a mid-chain raise
  after the early persist leaves a student message with no Apollo reply. That is
  deliberate — the transcript retains the student's words (grading-favorable)
  and the next turn's history includes them; never "clean up" the dangling row.
- **The P2.2 meter counts GRADED node types only** (2026-08-07):
  `_GRADED_NODE_TYPES` imported from `overseer/topic-score`, never re-listed
  here. `definition` / `variable_mapping` never enter the grade, so a celebrated
  ungraded topic must not make the meter read "done"; `open_graded_topics` =
  graded ids minus the tally's `understood` ids. Both keys ship on the ask AND
  the auto-done branch (the student-UI types against them). The derivation adds
  no query, and BOTH halves — the reference-solution walk and the covered-topics
  walk — sit inside one guard that soft-fails to `0/0`
  (`apollo_graded_topic_counts_failed`): a display-only meter must never 500 a
  teaching turn, including on a `QuestionDecision` shape change.
- The residual done-race window is the intent-classify call (~1-2s) before the
  early persist — accepted for P0; full elimination is the P3.4 concurrency
  review.
- **Neo4j is optional**: every KG read/write degrades on `KG_DEGRADED_ERRORS`
  (`_read_graph_or_empty` → empty `KGGraph`; `_write_kg_or_skip` → `nodes_added=0`);
  the Postgres + LLM reply always ships.
- `_handle_pending_done` / the questioning `done` branch import `handle_done`
  lazily to break the `handle_done ← store ← chat` import cycle.
- `_find_problem` now runs unconditionally near the top of `handle_chat`
  (previously resolved later, only on the teaching path) because an explicit
  Ask Hoot request needs it before intent classification.
- **Typed turns cannot trigger an aside**: `ask_hoot` defaults to false, and
  `_execute_reference_question` is reachable only through
  `_maybe_execute_reference_aside`.

## Related

Intent gate: `handlers/intent`; grading dispatch: `handlers/done`; hint-lane
bridge: `hoot-bridge-reference-answer`; parse: `parser/parser-llm` +
`parser/graph-context`; reply: `questioning/controller`; concept load:
`curriculum/db`; KG: `knowledge-graph/store` + `persistence/neo4j-client`;
problem lookup: `overseer/problem-selector`.
