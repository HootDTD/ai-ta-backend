---
doc: apollo/learner-model/personalization
description: The WU-6A session-personalization wedge — a course-scoped learner-profile DB read plus the pure problem-selection algorithm.
owns:
  - apollo/learner_model/personalization_read.py
  - apollo/learner_model/personalization_select.py
related:
  - apollo/learner-model/_index
  - apollo/persistence/models
  - apollo/persistence/learner-model-seed
  - apollo/schemas/problem
  - apollo/overseer/problem-selector
last_verified: 2026-07-25
stub: false
---

# apollo/learner-model/personalization

The WU-6A wedge — the one **live-wired** half of the learner-model package
(consumed by `apollo/overseer/problem-selector`). A DB read (6A1) feeds a pure
selection algorithm (6A2); the dependency arrow is strictly one-way (6A2 imports
the frozen 6A1 dataclasses, 6A1 imports nothing from 6A2 — no cycle).

## Interface

**`personalization_read.py` (6A1, the read):** `read_learner_profile(db, *,
user_id, search_space_id, concept_id) → LearnerProfile` — a pure, no-lock,
course-scoped read of at most 3 scoped queries (no N+1): this concept's
`app.learner_entities` id↔`canonical_key` maps, the `app.learner_state` rows
scoped by `user_id` AND `search_space_id` AND `entity_id IN (concept entities)`
(§1.4 per-classroom isolation), and the within-concept
`internal.entity_prerequisites` edges. Returns frozen `EntityProfile` (raw
`entity_id`/`canonical_key`/`mastery`/`confidence`, VERBATIM — `belief.py` not
imported) and `LearnerProfile` (`by_canonical_key` holds only entities with a
state row; `is_empty` True iff zero state rows = the PROD cold-start path).

**`personalization_select.py` (6A2, the pure algorithm):** LOCKED constants
`TEACHABLE_BAND_LO=0.3`/`HI=0.7`, `MASTERED_THRESHOLD=0.7`, `UNSEEN_MASTERY=0.50`,
`REPROBE_CONFIDENCE=0.4`; fns `reference_entity_keys(problem)` (reconstructs a
problem's canonical-key set IN-MEMORY via `_ENTRY_TYPE_TO_KIND_PREFIX` +
`f"{prefix}.{id}"`, because `Problem.model_validate` drops the seeded per-step
`entity_key` — no per-problem DB round-trip), `prereqs_mastered`, `weak_teachable`
(present entities in the inclusive band with prereqs mastered; unseen prereq reads
0.50 and blocks; low confidence soft-holds, never a hard negative),
`coverage_score` (deficit-weighted), and `personalize_selection(profile, pool, *,
concept_id, difficulty, attempted_ids) → Problem`.

## Data flow

`overseer/problem_selector` calls `read_learner_profile` then
`personalize_selection`. Selection: clamp within difficulty + not-attempted
filter; empty pool → `PoolExhaustedError`; cold-start / empty-weak →
`candidates[0]` (byte-identical to `overseer/problem_selector.select_problem`);
else max deficit-weighted coverage, ties broken to the LOWEST `Problem.id`.

## Invariants & gotchas

- **`is_empty` = the PROD cold-start path.** `app.learner_state` is populated only
  while `APOLLO_GRAPH_SIM_LAYER3_ENABLED` is ON (OFF in prod), so the read is
  empty in prod and selection falls to the `candidates[0]` anchor.
- `personalize_selection`'s `concept_id` arg exists SOLELY to reconstruct the
  `PoolExhaustedError(concept_cluster_id=str(concept_id))` message — it never
  filters or scores.
- DRIFT: `personalization_read.py`'s docstring references a nonexistent
  `apollo/learner_model/persistence.py::_lock_prior_state` and uses legacy
  `apollo_*` table names — the live tables are `app.learner_state` /
  `app.learner_entities` / `internal.entity_prerequisites`.

## Env flags

`APOLLO_GRAPH_SIM_LAYER3_ENABLED` (gates whether `learner_state` is populated;
OFF in prod → `is_empty` cold-start).

## Related

`apollo/persistence/learner-model-seed` (`_ENTRY_TYPE_TO_KIND_PREFIX`),
`apollo/schemas/problem` (`Problem`), `apollo/persistence/models`
(`LearnerEntity`/`LearnerState`/`EntityPrereq`), `apollo/overseer/problem-selector`
(the live consumer).
