---
doc: apollo/conversation/handlers/navigation
description: apollo/handlers/next.py + restart_problem.py — the two phase-transition problem-navigation handlers
owns:
  - apollo/handlers/next.py
  - apollo/handlers/restart_problem.py
related:
  - apollo/conversation/routing/router
  - apollo/conversation/routing/errors
  - apollo/overseer/problem-selector
  - apollo/knowledge-graph/store
last_verified: 2026-08-12
stub: false
---

# handlers/navigation — next / restart_problem

Two phase-transition handlers, both lazy-imported by `routing/router`.

## Interface

- `handle_next(*, db, session_id, difficulty) -> dict` (`next.py`) — POST
  `/sessions/{id}/next`. Advances to a NEW problem at the chosen difficulty;
  unified for post-Done advance (`phase=REPORT`) and mid-problem abandon
  (`TEACHING`/`PROBLEM_REVEAL`). Selects via `select_problem_personalized`
  (`overseer/problem-selector`) and creates a new `ProblemAttempt`.
- `handle_restart_problem(*, db, neo, session_id) -> dict` (`restart_problem.py`)
  — POST `/sessions/{id}/restart_problem`. Wipes the current attempt's KG
  subgraph (`store.delete_subgraph`) + `TutoringMessage` rows + its
  `QuestionOpportunity` ledger rows (2026-08-07, bimodal-fix defect I4: a
  surviving ledger carried stale `times_asked` across the wipe, so a capped-out
  pre-restart attempt auto-graded on its first post-restart message) but keeps
  the SAME `ProblemAttempt` / problem / difficulty.

## Data flow

Both row-lock the `TutoringSession` (`with_for_update`) against double-click
races, validate phase, then mutate. `handle_next` marks an abandoned in-flight
attempt `result="abandoned"` before selecting the next problem.

## Invariants & gotchas

- **Both are blocked during `SOLVING`** (`SessionFrozenError`); `INIT`/`BETWEEN`
  raise `InvalidPhaseError`.
- **The restart-vs-Done window is closed** (M1/P3.4): a lock only excludes
  parties that take it, and Done takes none — the phase check does the work.
  The hole was `store.freeze`'s transient `PROBLEM_REVEAL`, which is not in
  `_FROZEN_PHASES`; Done's claim now writes `SOLVING` as its first write, so
  restart 409s for the whole grading window. `_FROZEN_PHASES` is unchanged.
- `handle_restart_problem` is **KG-native with no silent skip**: because the wipe
  targets the SAME `attempt_id`, a `KG_DEGRADED_ERRORS` is re-raised as
  `KGUnavailableError` (503) so stale nodes can't resurface — the Postgres
  message- and ledger-deletes then never run (nothing half-wiped).
- Both reset `history_summary` / `history_summary_up_to_turn` to `None` (legacy
  columns only; never populated live).

## Related

Route wiring + error semantics: `routing/router`, `routing/errors`; problem
selection: `overseer/problem-selector`; KG wipe: `knowledge-graph/store`.
