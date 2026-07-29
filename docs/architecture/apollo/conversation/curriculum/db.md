---
doc: apollo/conversation/curriculum/db
description: DB-backed curriculum loader — the live course-scoped selection path reading app.concepts.
owns:
  - apollo/subjects/curriculum_db.py
related:
  - apollo/conversation/curriculum/registry
  - apollo/conversation/handlers/browse
  - apollo/conversation/handlers/chat
  - apollo/conversation/session-init
  - apollo/overseer/concept-inference
  - apollo/persistence/models
last_verified: 2026-07-25
stub: false
---

`apollo/subjects/curriculum_db.py` (WU-3D §8A runtime cutover) is the **live**
curriculum path — it reads `app.concepts` directly by `course_id` instead of
globbing the filesystem registry.

## Interface

- `list_course_concepts(db, *, search_space_id) -> list[ConceptRow]` — the student
  browse/entry candidate set. Consumers: `api.py` `/concepts`, `handlers/browse`,
  `hoot_bridge/session_init`.
- `load_concept_definition(db, *, concept_id, search_space_id) -> ConceptDefinition`
  — consumer `handlers/chat`.
- `list_registered_concepts(db, *, search_space_id) -> list[RegisteredConcept]` —
  the closed-list matcher's list; consumer `provisioning/authored_sets/orchestrator`.
- `ConceptRow`, `RegisteredConcept` dataclasses; `ConceptNotFoundError`.

## Data flow

`list_course_concepts` returns teachable concepts — a correlated `EXISTS` drops any
concept with no tier-2, non-quarantined problem, using the **exact** predicate the
downstream pool query (`overseer.problem_selector.list_problems_for_concept`)
applies, so the inference candidate set and the selectable pool stay in lockstep.
`load_concept_definition` rebuilds a `ConceptDefinition` from a course-scoped
`Concept` row, re-validating the JSONB/TEXT columns through the same pydantic models
`load_concept` uses; `problems_dir` is a sentinel non-existent path (problems come
from the DB, never the FS). `list_registered_concepts` returns EVERY registered
concept (excluding the provisional-inventory slug `PROVISIONAL_CONCEPT_SLUG`), NOT
filtered to teachable.

## Invariants & gotchas

- **Every concept lookup is course-scoped** (`search_space_id == course_id`).
- Immutable: a NEW `ConceptDefinition` per row; the ORM row is never mutated. Async
  by design (every caller holds the request-scoped `AsyncSession`).
- `ConceptNotFoundError` is deliberately **not** registered as an HTTP handler —
  it is internal and fires only if a session's `concept_id` points at a deleted
  concept (which `ON DELETE RESTRICT` should make impossible), so it surfaces loudly.

## Related

Authoring source `curriculum/registry`; callers `handlers/browse`, `handlers/chat`,
`session-init`; candidate consumer `overseer/concept-inference`; `Concept`/`Problem`
models `persistence/models`.
