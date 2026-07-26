---
doc: apollo/conversation/questioning/controller
description: Persistence orchestration around the unified tally+question call (DB-11), writing QuestionOpportunity audit rows.
owns:
  - apollo/smart_questions/controller.py
  - apollo/smart_questions/__init__.py
related:
  - apollo/conversation/questioning/unified
  - apollo/conversation/handlers/chat
  - apollo/persistence/models
  - apollo/schemas/problem
last_verified: 2026-07-25
stub: false
---

`apollo/smart_questions/controller.py` is the DB-11 persistence layer around
`unified.evaluate_and_ask`. `smart_questions/__init__.py` re-exports the package
public API.

## Interface

- `plan_next_question(db, *, course_id, attempt_id, session_id, problem, transcript, turn_index) -> QuestionDecision`
  (async) — re-exported by `__init__` and imported by `handlers/chat`.
- `QuestionDecision`, `CoveredTopic` result dataclasses (re-exported).

## Data flow

Builds the reference graph via `problem.to_kg_graph(attempt_id)`; loads the
attempt's `QuestionOpportunity` rows; `_build_tally_state` merges rows onto the
reference nodes (`_node_label`, `_evidence_rows`); `budget.questions_asked =
sum(times_asked)`. Calls `evaluate_and_ask`. `_apply_tally_updates` writes/updates
rows (`_valid_update_evidence` re-checks the quote against the transcript;
`_new_opportunity_row` for a new node; evidence appended dedup'd). `_covered_topics`
collects nodes whose merged state is `understood`. On `ask`, the target row's
`times_asked`/`last_asked_turn` are bumped. `_write_opportunity_audit` records
`asked_turn`/`answered_turn` timing only.

## Invariants & gotchas

- **One `QuestionOpportunity` per `(attempt_id, reference_node_id)`**, scoped by
  `course_id` + `session_id` + `attempt_id`. (The row's course/session scope keys
  are these columns — not `learning_activity_id`.)
- `times_asked` is cumulative → **confirm-once after two asks** (enforced in
  `unified` via `tally_state`).
- Evidence must appear in the transcript (both `unified` and `_valid_update_evidence`
  re-check); an invalid quote is logged and skipped.
- `_write_opportunity_audit` changes only timing/question/ask counters — it **never
  overwrites** the merged tally/learner state.
- `CoveredTopic` is the full `understood` snapshot each turn; the UI diffs `node_id`s
  to celebrate a topic once per attempt. Grading is untouched — this only reads the
  tally the questioning call already produced.

## Related

Engine `questioning/unified`; caller `handlers/chat`; `QuestionOpportunity` model
`persistence/models`; reference-graph source `schemas/problem`.
