---
doc: ai-ta-backend/platform/ops-seed-scripts
description: The four one-shot Apollo/data seeder CLIs (concept registry, learner-model Layer-1, :Canon projection, premade concepts) run manually via python -m scripts.<name>
owns:
  - scripts/seed_apollo_concept_registry.py
  - scripts/seed_apollo_learner_model.py
  - scripts/seed_canon_projection.py
  - scripts/seed_premade_concepts.py
related:
  - ai-ta-backend/apollo/persistence/models
  - ai-ta-backend/apollo/knowledge-graph/canon-projection
  - ai-ta-backend/apollo/provisioning/concept-match
  - ai-ta-backend/database/models
last_verified: 2026-07-25
stub: false
---

# platform/ops-seed-scripts — one-shot data seeders

Non-imported ops tools grouped into one doc (R3): all four are idempotent CLIs
run manually (`python -m scripts.<name>`), never on the runtime or CI path.

## Interface

- **`seed_apollo_concept_registry.py`** — walks
  `apollo/subjects/<subject>/concepts/…` and upserts `app.concepts` +
  `app.problems` (keyed by `subject_slug` / `(subject_id, concept_slug)` /
  `(concept_id, problem_code)`). **The prerequisite** for the others; after it
  runs the on-disk files become deletable.
- **`seed_apollo_learner_model.py`** — layers Layer-1 rows on top: writes
  `apollo_kg_entities` (concept/variable/reference-derived
  equations/conditions/simplifications/procedures, deduped by `canonical_key`) +
  `apollo_entity_prereqs`, and annotates each problem's `reference_solution` with
  `entity_key` + `declared_paths`. `--subject-slug`/`--concept-slug` select the
  scope; `--write-disk` optionally writes back to the source JSON.
- **`seed_canon_projection.py`** — WU-3C1 `:Canon` rebuild CLI: reads Layer-1
  `apollo_kg_entities` and idempotently `MERGE`s `:Canon` Neo4j nodes via
  `apollo.knowledge_graph.canon_projection.project_canon`.
- **`seed_premade_concepts.py`** — registers a `concepts.json` into a course
  (reversed provisioning): upserts `app.concepts` by normalized slug
  (`concept_match.norm_slug`); **never writes problems**.

## Data flow

Registry → learner-model → canon-projection is the dependency order; each reads
what the prior wrote. Postgres targets are `app.concepts`/`app.problems`/
`apollo_kg_entities`/`apollo_entity_prereqs`; the canon seeder additionally
writes Neo4j `:Canon` nodes.

## Invariants & gotchas

- **All four are idempotent** — re-running updates existing rows / re-MERGEs the
  same nodes with no duplicates.
- **DB-13: `misc.*` misconception entities are NEVER seeded** by the
  learner-model seeder (the app-schema kind CHECK has no `misconception` kind).
- The canon projection is **always course-scoped** — with neither `--concept-id`
  nor `--search-space-id` it resolves a default `search_space_id =
  MIN(app.courses.id)` and passes it explicitly (the unscoped-refusal stays intact).
- `seed_premade_concepts` must not pre-seed problems: the authored-set upload path
  owns the problem bank, and duplicates would gate-8 reject the uploads.

## Env flags

Postgres via `--database-url`/`SUPABASE_DB_URL`; Neo4j via env only
(`Neo4jClient.from_env()`: `NEO4J_URI`/`NEO4J_USERNAME`/`NEO4J_PASSWORD`/
`NEO4J_DATABASE`).

## Related

`apollo/persistence/models` + `database/models` (the tables written),
`apollo/knowledge-graph/canon-projection` (the projection core),
`apollo/provisioning/concept-match` (`norm_slug`).
