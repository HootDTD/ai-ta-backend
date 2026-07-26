---
doc: apollo/conversation/session-init
description: hoot_bridge session creation — the two Apollo entry paths (Hoot handoff + standalone) that mint a TEACHING session and first attempt.
owns:
  - apollo/hoot_bridge/session_init.py
  - apollo/hoot_bridge/__init__.py
related:
  - apollo/conversation/routing/router
  - apollo/conversation/routing/errors
  - apollo/conversation/curriculum/db
  - apollo/overseer/concept-inference
  - apollo/overseer/problem-selector
  - apollo/persistence/models
  - apollo/schemas/problem
last_verified: 2026-07-25
stub: false
---

# session-init — the two Apollo entry paths

`apollo/hoot_bridge/session_init.py` is where an Apollo tutoring session is born.
Both entries end an already-active session for the same `(user_id,
search_space_id)`, create a `TEACHING`-phase `TutoringSession` + its first
`ProblemAttempt`, commit, and return the front-end problem payload.

## Interface

- `init_session_from_hoot(*, db, user_id, search_space_id, hoot_transcript,
  difficulty) -> dict` — the original Hoot→Apollo handoff. The transcript is used
  for **exactly one thing**: `infer_concept_id` picks the concept.
- `init_session_direct(*, db, user_id, search_space_id, concept_id, difficulty,
  problem_id=None) -> dict` — the WU-E2E standalone entry: the student explicitly
  picks `concept_id` (validated against the course's teachable set) and optionally
  a specific `problem_id` (validated against the concept's pool). No LLM, no
  transcript.
- `_create_session_with_problem(...)` — the shared tail both entries delegate to.

Consumers: the `/apollo` router (`routing/router`) mounts both as session-start
endpoints.

## Data flow

Both paths resolve the teachable candidate set via
`curriculum/db.list_course_concepts`, pick a `Problem`
(`overseer/problem-selector.select_problem_personalized`, or the explicit pool
lookup `list_problems_for_concept` in the direct path), then call the shared tail.
The tail flips any active session to `ended`, `flush`es, adds the new
`TutoringSession` (`phase=TEACHING`, `current_problem_id=problem.database_id`) and
the first `ProblemAttempt`, `flush`es, captures `attempt.id`, and `commit`s before
building the FE payload. The Hoot path's only transcript use is concept inference.

## Invariants & gotchas

- **Concept inference is transcript-only** — the transcript never seeds the KG or
  reaches the parser; it only chooses `concept_id`.
- **Membership is gated at the route**, not here — these functions trust the
  authenticated `user_id`/`search_space_id` the auth deps resolved.
- `difficulty` must be one of `{intro, standard, hard}` or it raises `ValueError`.
- The direct path validates `concept_id` against the course's teachable concepts
  (`NoMatchingConceptError`, 409) and a supplied `problem_id` against the pool
  (`ProblemNotFoundError`, 404); the Hoot path can raise
  `NoMatchingConceptError`/`PoolExhaustedError` from inference + selection.
- `_create_session_with_problem` owns the single `commit`; callers must not wrap it
  in an outer transaction.

## Related

Routes `routing/router`; error taxonomy `routing/errors`; concept candidate set
`curriculum/db`; concept inference `overseer/concept-inference`; problem selection
`overseer/problem-selector`; ORM `persistence/models`; problem shape
`schemas/problem`.
