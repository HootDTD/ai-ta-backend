---
doc: apollo/persistence/_index
description: Router for Apollo's persistence sub-area — the ORM hub, scoped repositories, the DB-free Layer-1 seed converter, and the Neo4j seam.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# apollo / persistence

The Apollo Postgres + Neo4j persistence layer: one ORM hub declaring ~17 models,
a few small course-scoped repositories over them, the pure Layer-1 seed
converter, and the async Neo4j client. The live Layer-3 mastery *writer* is NOT
here — it is `apollo/projections/mastery.py` (see `apollo/projections/mastery`),
which does not use the belief filter's row-spec objects.

## Leaf docs

| Doc | One-liner | Owns |
|---|---|---|
| [models](models.md) | ~17 SQLAlchemy models for Apollo's `app`/`internal` tables (imported by 37 modules) | `apollo/persistence/models.py` (+`__init__.py` glue) |
| [learner-model-seed](learner-model-seed.md) | Pure DB/LLM-free Layer-1 seed conversion core + `validate_reference_graph` | `apollo/persistence/learner_model_seed.py` |
| [neo4j-client](neo4j-client.md) | Async Neo4j driver wrapper + degraded-error tuple + run-once Cypher DDL | `apollo/persistence/neo4j_client.py`, `neo4j_schema.cypher` |
| [progress-repo](progress-repo.md) | Course-scoped XP/level repository over `app.student_progress` | `apollo/persistence/progress_repo.py` |
| [done-write-linkage](done-write-linkage.md) | Prior-graded-attempt + durable problem-id resolvers for the Done write path | `apollo/persistence/attempt_history.py`, `problem_linkage.py` |

## Cross-cutting invariants

- **ORM declares NO CHECK constraints.** The migration SQL is the schema
  authority; `models.py` mirrors each CHECK as an app-layer tuple
  (`ENTITY_KINDS`, `ATTEMPT_RESULTS`, …) asserted equal to the DDL by tests.
- **Current `app`/`internal` table names only.** DB-13/DB-14 retargeted the
  learner-model + grading tables; the legacy `apollo_*` names survive only as
  frozen migration provenance (`database/legacy-migrations`).
- **Add a column → active supabase chain.** A new column on any tutoring/learner
  table needs a migration in the **active** supabase chain
  (`database/supabase-migrations`), never the frozen legacy chain; then reconcile
  the owning leaf. Full recipe in [models](models.md).

## Related

`apollo/learner-model/_index`, `apollo/schemas/_index`, `database/models`
(`Base`/`LearningActivity`), `database/supabase-migrations`,
`apollo/projections/mastery` (the live mastery writer).
