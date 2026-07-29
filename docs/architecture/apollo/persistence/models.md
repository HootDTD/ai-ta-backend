---
doc: apollo/persistence/models
description: The Apollo ORM hub — every SQLAlchemy model for Apollo's app/internal Postgres tables, as a catalog grouped by concern.
owns:
  - apollo/persistence/models.py
  - apollo/persistence/__init__.py
related:
  - apollo/persistence/_index
  - apollo/persistence/learner-model-seed
  - apollo/persistence/progress-repo
  - apollo/persistence/done-write-linkage
  - apollo/schemas/problem
  - apollo/projections/mastery
  - apollo/overseer/topic-score
  - apollo/overseer/_index
  - database/models
  - database/supabase-migrations
last_verified: 2026-07-26
stub: false
---

# apollo/persistence/models

The Apollo ORM hub: every SQLAlchemy model for Apollo's Postgres tables under the
`app` and `internal` schemas (DB-04..DB-15). One ~1215-line module — a known
**monolith-hub** doc (a per-domain code split would let this doc fan out).
Imports `Base` + `LearningActivity` from `database/models`; the fast test suite
runs on SQLite through the `_JSONType` / `_RealArrayType` / `_TextArrayType`
`with_variant` columns (array-CHECK semantics are exercised only on real Postgres).

## Interface

**Enums & allowlist tuples** — app-layer mirrors of the SQL CHECKs, asserted equal
to the DDL by `apollo/persistence/tests`: `SessionPhase`, `SessionStatus`;
`ENTITY_KINDS` (mirrors `app.learner_entities.kind` — **`misconception`
deliberately absent**, DB-13); `ATTEMPT_RESULTS` + `GRADED_ATTEMPT_RESULTS`
(mirror `problem_attempts.result`; `abandoned` excluded from graded);
`MASTERY_EVENT_KINDS` + `FINDING_KINDS` (open enums, documentation-only). Helper
`promote_tutoring_message_metadata` splits legacy message-metadata signals.

**Model catalog** (grouped by concern; model → `schema.table` → responsibility):

| Model | Table | Responsibility / key cols |
|---|---|---|
| `Concept` | `app.concepts` | Course-scoped curriculum concept; folded `subject_slug`/`display_name`; unique `(course_id, subject_slug, slug)` |
| `Problem` | `app.problems` | Typed problem-bank row; promoted cols `problem_text`/`given_values`/`target_unknown`/`reference_solution`/`payload_extra` + `tier`/`quarantined_at`/`solution_source`/`provenance`; `from_pydantic_payload`/`from_inventory_payload`/`apply_*`/`to_pydantic_payload` converters; `solution_source`/`provenance` validators |
| `ProvisioningRun` | `app.provisioning_runs` | Teacher-gated run; `kind` authored_set\|generation; `authored_set()`/`generation()` factories; partial-unique authored `(course_id, set_index)` |
| `IngestRun` | `internal.content_ingest_runs` | Per-document provisioning telemetry (scrape/promote/reject/dedup counts + LLM cost) |
| `DedupDecision` | `internal.dedup_decisions` | method + similarity + verdict per dedup resolution |
| `IngestError` | `internal.content_ingest_errors` | stage + class + context per non-terminal error |
| `IngestPageEvidence` | `internal.ingest_page_evidence` | Per-page OCR evidence the S2 ingestion audit reads |
| `QuestionOpportunity` | `app.question_opportunities` | One tally/question row per reference node per attempt; unique `(attempt_id, reference_node_id)`; `state` (server_default `asked_waiting`) + `evidence`/`student_declined`/`times_asked` |
| `TutoringSession` | `app.learning_activities` | Polymorphic `LearningActivity` (`modality='tutoring'`); `search_space_id` synonym→`course_id`; nullable `grounding_bundle` JSONB; partial-unique active-session index; `messages`/`problem_attempts` relationships |
| `TutoringMessage` | `app.tutoring_messages` | Turn log; unique `(learning_activity_id, turn_index)`; `__init__` promotes legacy `metadata` into typed cols |
| `ProblemAttempt` | `app.problem_attempts` | Per-attempt row; `result` (CHECK); `learner_update_pending` + janitor backoff cols (`learner_update_attempts`/`_failed_at`/`_last_error`/`_next_attempt_at`) |
| `StudentProgress` | `app.student_progress` | Composite PK `(user_id, course_id)`; `xp_total`/`level`/`last_level_up_at` |
| `LearnerEntity` | `app.learner_entities` | Layer-1 skill inventory; denormalized `course_id` NOT NULL; `canonical_key` unique per concept; `aliases TEXT[]` |
| `EntityPrereq` | `internal.entity_prerequisites` | Layer-1 prereq edges; composite PK `(from_entity_id, to_entity_id)`; **`from` depends on `to`** (dependent→prereq, opposite of KG DEPENDS_ON) |
| `LearnerState` | `app.learner_state` | Layer-3 belief snapshot; `belief REAL[3]`/`mastery`/`confidence`; **no `misconception_code`** (DB-13) |
| `MasteryEvent` | `app.mastery_events` | Append-only evidence log; `event_kind`, `evidence_node_ids TEXT[]`, `negotiation_move` (nullable-live); unique NULLS NOT DISTINCT `(attempt_id, entity_id, event_kind)`; **no `misconception_code`** |
| `GradingRun` | `internal.grading_runs` | Canonical grade artifact; `role` canonical\|pair; unique `(attempt_id, role, grader_version)`; typed scores + `*_details` JSONB + `grader_payload` catch-all; `problem_id` FK `app.problems` ON DELETE RESTRICT |

## Data flow

`Base` + `LearningActivity` come from `database/models`; `TutoringSession` is the
tutoring-modality view of `app.learning_activities`. The Done write path stamps
`ProblemAttempt.result='graded'`, appends one canonical `GradingRun`, and — via
`apollo/projections/mastery` — writes `MasteryEvent` + `LearnerState`, while
`apollo/persistence/progress-repo` bumps `StudentProgress`. `Concept`/`Problem`
are consumed cross-domain by overseer, provisioning, and questioning.

## Invariants & gotchas

- **No ORM CHECK constraints** — the migration SQL is the authority; the enum
  tuples above are the app-layer mirror (test-asserted). `misconception` is
  absent from `ENTITY_KINDS` on purpose (DB-13); it is tracked via
  `MasteryEvent.event_kind`, and neither `LearnerState` nor `MasteryEvent` has a
  `misconception_code` column.
- **Python-name / physical-column remaps.** Internal-schema tables FK'd to
  `app.courses` keep the Python attribute `search_space_id` mapped onto the
  physical `course_id` column (`LearnerState`, `MasteryEvent`, `IngestRun`,
  `DedupDecision`, `GradingRun`, …); the tutoring child tables keep Python
  `session_id` mapped onto the physical `learning_activity_id` column.
- **`grounding_bundle` is optional and student-safe.** NULL means the pre-feature
  path; when present it holds packed snippets, diagnostics, build time, and
  retrieval version for the tutoring session.
- **`GradingRun` is append-only** (no update path in code). `composite_score`
  and `node_coverage_score` are **RETIRED** legacy columns — always written
  `None`; the live grade of record is `topic_score` (Appendix A #26; the
  cross-cutting invariant lives in `apollo/overseer/_index`). The Evidence-Graph-v2
  columns (`transcript_freeze_hash` … `band`) are a dormant DDL seam, never set by
  the current LLM-fallback writer.
- **Deleted models:** `GraphComparisonRun`/`GraphComparisonFinding` (graph-grader
  shadow chain, A7) and `KGNegotiation` (audit table, DB-13/A6) are gone;
  `FINDING_KINDS` survives only as a documentation tuple.
- **Add a column + migration (recipe, §4.0.14).** A new column on any
  tutoring/learner table normally needs a migration in the active supabase chain.
  Interaction-1 is the explicit sequential-number exception at frozen `048`;
  add the ORM column here and bump `last_verified` in the same commit.
- **DRIFT:** the module docstring (lines 1-10) is STALE — it claims Postgres owns
  only 4 tables and that `apollo_kg_entries` was dropped. That predates DB-07..15;
  the real inventory is the ~17 models above. Ignore the docstring's 4-table claim.

## Related

`database/models` (`Base`/`LearningActivity`), `database/supabase-migrations`
(add-a-column), `apollo/schemas/problem` (the Pydantic shape `Problem` promotes to
columns), `apollo/persistence/learner-model-seed`, `apollo/projections/mastery`
(writes the learner/grading rows), `apollo/overseer/topic-score` +
`apollo/overseer/_index` (grade of record / composite retirement).
