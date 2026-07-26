---
doc: apollo/knowledge-graph/canon-projection
description: The WU-3C1 :Canon projection seeder — idempotently MERGE Layer-1 Postgres entities into Neo4j :Canon nodes.
owns:
  - apollo/knowledge_graph/canon_projection.py
related:
  - apollo/knowledge-graph/resolution-store
  - apollo/projections/mastery
  - apollo/persistence/neo4j-client
last_verified: 2026-07-25
stub: false
---

# Knowledge-graph — canon projection

Reads Layer-1 entities from Postgres (`apollo_kg_entities`, the
`app.learner_entities` surface) and idempotently `MERGE`s `:Canon` nodes in
Neo4j. This is a **write-only, node-only** projection — the `:Canon` reads and
`RESOLVES_TO` edges are a separate concern
([resolution-store](resolution-store.md)).

## Interface

- `project_canon(db, neo, *, search_space_id=None, concept_id=None)
  → CanonProjectionResult` — the top-level seed; called **live** by
  `provisioning/promote.py` inside its transaction.
- `load_entity_specs(db, *, search_space_id=None, concept_id=None)
  → list[CanonNodeSpec]` — the scoped Postgres read; called **live** by
  `projections/mastery.py` (entity-id lookups for the mastery ledger) and
  referenced by `subjects/curriculum_db.py`.
- `merge_specs(neo, specs) → int` — the Neo4j-write half (extracted as a
  testable seam).
- `CanonNodeSpec` / `CanonProjectionResult` (frozen), plus the pure mappers
  `entity_to_canon_spec` and `canon_spec_to_row`.

## Data flow

`project_canon` = `load_entity_specs` (read-only Postgres, join `Concept` to
recover `search_space_id`) → `merge_specs` (one `MERGE`-on-`key` batch in one
Neo4j session). No cross-store transaction: the projection holds none because it
is rebuildable at any time (also via `scripts/seed_canon_projection.py`).

## Invariants & gotchas

- **`:Canon` key is the BIGSERIAL surrogate `apollo_kg_entities.id`, never a
  synthesized `f"{search_space_id}:{canonical_key}"`.** Postgres uniqueness is
  `(concept_id, canonical_key)`, so two different concepts may legitimately mint
  the same `canonical_key`; a key derived from `canonical_key` would fuse them
  into one `:Canon` node and cross-contaminate every future `RESOLVES_TO` edge.
  The surrogate id makes fusion impossible and survives key renames.
- **`MERGE`-on-key makes re-runs idempotent** — a second projection over the
  same entities yields the same node count.
- **Scope is mandatory:** `concept_id` wins, else `search_space_id`, else
  `ValueError` — a course-blind projection is forbidden.
- **No misconceptions in `:Canon`** — DB-13 excludes `kind='misconception'` at
  the DDL, so no such row is ever returned; no special-casing needed.
- NO-FALLBACK: either side's failure surfaces as `CanonProjectionError` with the
  offending `stage` (`load_entities` / `merge_canon`).

## Related

- [knowledge-graph/resolution-store](resolution-store.md) — the `RESOLVES_TO`
  edges that target these `:Canon` nodes.
- [projections/mastery](../projections/mastery.md) — live consumer of
  `load_entity_specs`.
- [persistence/neo4j-client](../persistence/neo4j-client.md) — the Neo4j session
  source.
