---
doc: apollo/projections/mastery
description: Flat-EWMA mastery-ledger projection from the canonical grading artifact.
owns:
  - apollo/projections/mastery.py
  - apollo/projections/__init__.py
related:
  - apollo/knowledge-graph/canon-projection
  - apollo/grading/event-model
  - apollo/projections/classroom
  - apollo/persistence/models
  - apollo/conversation/handlers/done
last_verified: 2026-07-25
stub: false
---

# Projections mastery — flat-EWMA ledger write

`update_mastery_from_artifact` is the LIVE, simpler mastery write path — a flat
EWMA of the artifact's already-computed composite, separate from the dormant
WU-5A2 Bayesian `run_learner_update`. It writes nothing beyond what the artifact
carries. The namespace `__init__.py` is the package docstring only (no re-exports).

## Interface

- `update_mastery_from_artifact(db, *, artifact_row: GradingRun) -> None` — called
  live by `handlers/done.py` (after the artifact row is committed).
- `ewma_alpha()`, `ewma_mastery(*, composite, prior_mastery, alpha)` helpers.

## Data flow

Distinct credited/misconception `canonical_key`s from the artifact's
`node_ledger` resolve to entity ids via
`knowledge_graph.canon_projection.load_entity_specs` (exact namespaced map, with
a bare-suffix fallback for `llm_fallback` keys; ambiguous suffixes drop). For each
entity it EWMA-folds `GradingRun.composite_score` into `app.learner_state.mastery`
and appends a `composite`-kind `app.mastery_events` row.

## Invariants & gotchas

- **Reads the legacy `composite_score`**, not the topic score — mastery tracks the
  axis-rubric blend (see [scorecard](scorecard.md) / [_index](_index.md)).
- **FLUSH-ONLY:** the caller owns the txn boundary; a no-op when
  `concept_id is None` or the ledger names no resolvable key.
- **Idempotent per attempt:** an existing `(attempt_id, entity_id,
  event_kind='composite')` event short-circuits (no double EWMA on retry).
- `event_kind='composite'` is a distinct open enum value so this path never
  collides with WU-5A's covered/missing/… keys; the two are mutually exclusive at
  the `done.py` call site.
- **Stale docstring refs** cite `apollo.grading.composite.*` (nonexistent) — the
  `_env_float` reader is inline.

## Env flags

- `APOLLO_MASTERY_EWMA_ALPHA` — EWMA smoothing weight, read fresh (default 0.3).
