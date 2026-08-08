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
last_verified: 2026-08-08
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
the student-ui coverage meter. On `ask`, the target row's `last_asked_turn` is
stamped and `times_asked` is bumped **unless the engine flagged `fallback_served`
AND this is the node's first serve** (`asked_turn` still NULL).
`_write_opportunity_audit` records `asked_turn`/`answered_turn` timing only.

## Invariants & gotchas

- **One `QuestionOpportunity` per `(attempt_id, reference_node_id)`**, scoped by
  `course_id` + `session_id` + `attempt_id`. (The row's course/session scope keys
  are these columns — not `learning_activity_id`.)
- `times_asked` is cumulative → **two asks per node max**, enforced in code by
  `questioning/selection` (`MAX_ASKS_PER_NODE`), no longer prompt-only. It also
  drives `budget.questions_asked` (`sum(times_asked)`).
- **A fallback reply costs no probe — ONCE per node.**
  `UnifiedQuestionResult.fallback_served` marks a `*_exhausted` turn, where the
  served text is a verbatim public clause rather than a question about the target.
  On the node's FIRST serve `times_asked` is left alone so it stays askable:
  charging it exhausted thin rubrics' graded nodes into a forced `done` →
  `handle_done(auto_done=True)` → topic scored 0 (the bimodal-F mode). Every later
  serve on the same node charges, degenerate or not — `budget.questions_asked` is
  `sum(times_asked)`, so a permanently off-policy model would otherwise re-serve
  the same clipped clause forever without `budget_exhausted` ever getting closer.
  `last_asked_turn`, `question` and `asked_turn` are always recorded — the student
  really did see that turn.
- **A row is not evidence the node was probed.** A free fallback (and a bare
  `missing` tally update) leaves a row with `times_asked = 0`, state `missing` and
  no evidence. `handlers/done._probed_node_ids` is the reader that decides what
  counts as engagement for the P1.2b denominator — never `SELECT reference_node_id`
  over the whole ledger, which put the un-probed node straight back in at credit 0.
- **`open_graded_topics` is "not yet `understood`", NOT "still askable".** A graded
  node at `times_asked == MAX_ASKS_PER_NODE` and still `tentative` keeps counting,
  and no further conversation can clear it. This is the cross-repo contract's pinned
  definition; UI copy must not promise that Apollo will ask about those topics.
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
