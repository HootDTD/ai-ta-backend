---
doc: apollo/conversation/handlers/lifecycle
description: apollo/handlers/lifecycle.py — retry / end / get-session snapshot handlers
owns:
  - apollo/handlers/lifecycle.py
related:
  - apollo/conversation/routing/router
  - apollo/knowledge-graph/store
  - apollo/persistence/models
  - apollo/schemas/problem
last_verified: 2026-07-31
stub: false
---

# handlers/lifecycle — retry / end / snapshot

`apollo/handlers/lifecycle.py` holds the Slice-0a session lifecycle handlers.

## Interface

- `handle_retry(*, db, session_id) -> dict` — retry the CURRENT problem. When the
  current attempt is already resolved (post-grade retry), creates a NEW
  `ProblemAttempt` row for the same problem (returns its `attempt_id`) so the
  transcript/KG start empty and a second Done cannot collide with
  `uq_grading_artifact_attempt_role`; an in-flight attempt is a pure phase flip.
  Row-locks the session against double-click races.
- `handle_end(*, db, neo, session_id) -> dict` — mark the session ended. `neo` is
  retained for signature parity but unused (§7 retention: per-attempt subgraphs
  are PERSISTED, not deleted, at end).
- `handle_get_session(*, db, neo, session_id) -> dict` — GET `/sessions/{id}`
  snapshot: session fields + current problem + KG panel + messages; degrades the
  KG to `{nodes:[], edges:[]}` on `KG_DEGRADED_ERRORS`. Also returns
  `ask_hoot_available` — mirrors the chat-handler aside gate (INTERACTION4 +
  `interaction_allowed_for_concept(problem.concept_id)`, False with no current
  problem) so the student UI only renders the Ask Hoot button where the
  INTERACTION4 hint lane can actually run. Messages serialize via
  `_snapshot_message`: INTERACTION4 aside rows carry the **wire** intent
  `"reference_aside"` (translated from the stored `ASIDE_MESSAGE_INTENT_TAG`;
  the only intent value the student UI keys on — other rows' internal intents
  are not exposed) plus an `aside: {text, citations, in_scope}` payload
  rebuilt from the row's `message_metadata` (written by `handlers/chat` since
  2026-07-30; older aside rows come back with the intent but no payload, so
  the UI's card renders without chips).

## Data flow

All three are `Depends(require_session_owner)` routes in `routing/router`.
`handle_get_session` reads the current attempt's subgraph via `KGStore` and loads
only `current_problem_id` by its database id, joining its concept slug and keeping
the course/concept, tier-2, and non-quarantined eligibility guards. It rebuilds
the same public Pydantic problem shape without loading or validating the rest of
the concept's problem pool.

## Invariants & gotchas

- `handle_retry` clears `pending_intent` and resets `history_summary` /
  `history_summary_up_to_turn` to `None` — these columns are **legacy resets
  only, never populated live** (they were the vestigial `handlers/history` path).
- `restart_problem` is the only explicit student KG wipe; `handle_end` no longer
  deletes subgraphs.

## Related

Route wiring: `routing/router`; KG reads: `knowledge-graph/store`; problem ORM
and public schema: `persistence/models` and `schemas/problem`.
