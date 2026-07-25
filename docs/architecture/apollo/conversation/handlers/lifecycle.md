---
doc: ai-ta-backend/apollo/conversation/handlers/lifecycle
description: apollo/handlers/lifecycle.py — retry / end / get-session snapshot handlers
owns:
  - apollo/handlers/lifecycle.py
related:
  - ai-ta-backend/apollo/conversation/routing/router
  - ai-ta-backend/apollo/knowledge-graph/store
  - ai-ta-backend/apollo/overseer/problem-selector
last_verified: 2026-07-25
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
  KG to `{nodes:[], edges:[]}` on `KG_DEGRADED_ERRORS`.

## Data flow

All three are `Depends(require_session_owner)` routes in `routing/router`.
`handle_get_session` reads the current attempt's subgraph via `KGStore` and lists
the teachable problem via `list_problems_for_concept` (`overseer/problem-selector`).

## Invariants & gotchas

- `handle_retry` clears `pending_intent` and resets `history_summary` /
  `history_summary_up_to_turn` to `None` — these columns are **legacy resets
  only, never populated live** (they were the vestigial `handlers/history` path).
- `restart_problem` is the only explicit student KG wipe; `handle_end` no longer
  deletes subgraphs.

## Related

Route wiring: `routing/router`; KG reads: `knowledge-graph/store`; problem
listing: `overseer/problem-selector`.
