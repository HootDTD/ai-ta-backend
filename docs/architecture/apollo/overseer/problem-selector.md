---
doc: apollo/overseer/problem-selector
description: Tier-2 problem-bank selection from app.problems, plus the session-personalization master flag.
owns:
  - apollo/overseer/problem_selector.py
  - apollo/overseer/personalization_flag.py
related:
  - apollo/overseer/concept-inference
  - apollo/learner-model/personalization
  - apollo/schemas/problem
last_verified: 2026-07-25
stub: false
---

# Overseer problem selector — bank selection

Picks a problem from `app.problems` by course + concept, reassembling promoted
columns through the public `Problem` Pydantic schema.

## Interface

- `list_problems_for_concept(db, *, concept_id, search_space_id) ->
  list[ProblemSchema]` — the SOLE teachable-pool chokepoint (imported by
  `handlers/{browse,chat,done,lifecycle}` + `hoot_bridge`).
- `select_problem(db, *, concept_id, search_space_id, difficulty, attempted_ids)`
  — deterministic first-unattempted pick.
- `select_problem_personalized(...)` — the WU-6A3 wedge (imported by
  `handlers/next.py`).
- From `personalization_flag.py`: `is_enabled()`.

## Data flow

`list_problems_for_concept` queries Tier-2, non-quarantined problems joined to
`Concept`, validates each row through `schemas.problem.Problem`, and returns them
sorted by `Problem.id` (refreshed every call, no caching). `select_problem`
filters by difficulty + `attempted_ids` and raises `PoolExhaustedError` when none
remain. `select_problem_personalized` gates on `is_enabled()`: flag-OFF delegates
byte-identically to `select_problem`; flag-ON reads the pool + learner profile
once at the seam (no N+1) and hands scoring to the frozen WU-6A2
`personalize_selection`.

## Invariants & gotchas

- **Tier-1 is excluded** (auto-provisioned inventory, not yet teachable) and
  `quarantined_at IS NULL` drops anomaly-quarantined problems — one predicate
  gates both selection paths.
- **Invalid payloads are skipped, not fatal** — a row failing schema validation
  is logged and omitted.
- Personalization emits exactly one `event=personalized_selection` log when
  engaged; `PoolExhaustedError` is raised before that log.

## Env flags

- `APOLLO_SESSION_PERSONALIZATION_ENABLED` — `is_enabled()` master gate, default
  OFF everywhere incl. prod. Double-gated with `APOLLO_GRAPH_SIM_LAYER3_ENABLED`
  (which gates the `learner_state` WRITE): personalization is a no-op until that
  table is populated.
