---
doc: apollo/conversation/session-init
description: hoot_bridge session creation — both Apollo entry paths, first attempt, and optional session grounding.
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
last_verified: 2026-08-04
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
  for **exactly one thing**: `infer_concept_id` picks the concept, called via
  `await asyncio.to_thread(infer_concept_id, ...)` (2026-08-04) so this LLM call
  no longer blocks the event loop while session start waits on it.
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
building the FE payload. With `INTERACTION1` enabled and the problem concept
allowed by `INTERACTION_CONCEPTS`, the durable session schedules (but does not
await) one background `retrieve_for_question` call anchored by concept display
name + student-visible problem text (`top_k=8`, `token_budget=2500`). The task is
held in a module-level strong-reference set, opens its own loop-local session
from the database session factory after the creation commit, re-fetches the
`TutoringSession`, and uses a separate best-effort commit to store packed snippet
dicts + diagnostics in `grounding_bundle`. The POST response never includes or
waits for this bundle. The Hoot path's only transcript use is concept inference.

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
- `_create_session_with_problem` owns the creation `commit`; callers must not wrap
  it in an outer transaction. Grounding never widens that transaction or reuses
  the request-owned `AsyncSession`: its optional persistence commit starts in a
  fresh session only after the tutoring session is durable.
- Grounding is student-safe: authored solution-role / solution-kind snippets are
  dropped before persistence. Retrieval, filtering, or persistence failure is
  logged, rolled back, and leaves `grounding_bundle` NULL without failing creation.
- Chat intentionally tolerates a still-NULL bundle while the background task is
  running; there is no synchronization on the first turn.

## Env flags

`INTERACTION1` (default off) gates the entire retrieval path; flag-off makes no
retrieval call and preserves the prior session payload/behavior.
`INTERACTION_CONCEPTS` optionally restricts grounding to comma-separated concept
slugs. Slugs are stripped and casefolded; unset/empty means unrestricted, so
existing deployments retain their current behavior.

## Related

Routes `routing/router`; error taxonomy `routing/errors`; concept candidate set
`curriculum/db`; concept inference `overseer/concept-inference`; problem selection
`overseer/problem-selector`; ORM `persistence/models`; problem shape
`schemas/problem`.
