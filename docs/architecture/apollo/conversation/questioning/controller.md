---
doc: apollo/conversation/questioning/controller
description: Persistence orchestration around the unified tally+question call (DB-11), writing QuestionOpportunity audit rows.
owns:
  - apollo/smart_questions/controller.py
  - apollo/smart_questions/__init__.py
related:
  - apollo/conversation/questioning/unified
  - apollo/conversation/questioning/selection
  - apollo/conversation/handlers/chat
  - apollo/persistence/models
  - apollo/schemas/problem
last_verified: 2026-08-07
stub: false
---

`apollo/smart_questions/controller.py` is the DB-11 persistence layer around
`unified.evaluate_and_ask`. `smart_questions/__init__.py` re-exports the package
public API.

## Interface

- `plan_next_question(db, *, course_id, attempt_id, session_id, problem, transcript, turn_index) -> QuestionDecision`
  (async) — re-exported by `__init__` and imported by `handlers/chat`.
- `QuestionDecision` (`action`, `question`, `target_node_id`, `covered_topics`,
  `graded_topic_total`, `open_graded_topics`), `CoveredTopic` (re-exported).

## Data flow

Builds the reference graph via `problem.to_kg_graph(attempt_id)`; loads the
attempt's `QuestionOpportunity` rows; `_build_tally_state` merges rows onto the
reference nodes (`_node_label`, `_evidence_rows`); `budget.questions_asked =
sum(times_asked)`. Calls `evaluate_and_ask`. `_apply_tally_updates` writes/updates
rows (`_new_opportunity_row` for a new node; evidence appended dedup'd).
`_covered_topics` collects nodes whose merged state is `understood`, and
`build_selection_policy` (`questioning/selection`) over the post-update rows yields
the `graded_topic_total` / `open_graded_topics` counts the chat response serves to
the student-ui coverage meter. On `ask`, the target row's
`times_asked`/`last_asked_turn` are bumped. `_write_opportunity_audit` records
`asked_turn`/`answered_turn` timing only.

## Invariants & gotchas

- **One `QuestionOpportunity` per `(attempt_id, reference_node_id)`**, scoped by
  `course_id` + `session_id` + `attempt_id`. (The row's course/session scope keys
  are these columns — not `learning_activity_id`.)
- `times_asked` is cumulative → **two asks per node max**, enforced in code by
  `questioning/selection` (`MAX_ASKS_PER_NODE`), no longer prompt-only.
- **One evidence validator (P2.4).** The raw case/punctuation-sensitive
  `_valid_update_evidence` re-check is DELETED: `unified._decode_updates` already
  rejects (and logs) a quote that is not a normalized verbatim match in the cited
  student turn, and the duplicate raw check silently dropped valid updates — the
  defect that left tally rows stuck `missing` and made Apollo re-probe.
- `student_declined` is no longer read or written here; the column keeps its
  `false` default (see `questioning/unified` for the removal rationale).
- `_write_opportunity_audit` changes only timing/question/ask counters — it **never
  overwrites** the merged tally/learner state.
- `CoveredTopic` is the full `understood` snapshot each turn; the UI diffs `node_id`s
  to celebrate a topic once per attempt. Grading is untouched — this only reads the
  tally the questioning call already produced.

## Related

Engine `questioning/unified`; target policy `questioning/selection`; caller
`handlers/chat`; `QuestionOpportunity` model `persistence/models`; reference-graph
source `schemas/problem`.
