---
doc: ai-ta-backend/apollo
description: Apollo learning-by-teaching subsystem and permanent transcript/topic grading path
owns:
  - apollo/**
related:
  - ai-ta-backend/domain-data
  - shared/supabase
  - shared/product-context
last_verified: 2026-07-21
stub: false
---

# Apollo — learning by teaching

Apollo asks a student to teach a concept to a deliberately confused learner.
Postgres stores sessions, messages, attempts, progress, reference content, and
optional canonical grading artifacts. Neo4j stores the student's per-attempt
knowledge graph and the rebuildable reference projection.

## Live teaching loop

1. Session creation selects a tier-2, non-quarantined problem from the requested
   course and concept. Reference solutions remain private.
2. Each student message is persisted before parsing. The parser turns supported
   claims into typed nodes and edges, and `KGStore` writes them to the attempt's
   Neo4j subgraph. A parse miss contributes no nodes and still proceeds to the
   conversational response.
3. Unified questioning reads the private reference content, the full attempt
   transcript, and durable per-node tally state. It emits one acknowledgement
   and one naive question, while the output belt records possible private-data
   leakage as telemetry without rewriting a valid response.
4. Challenge, paraphrase, skip, trace, restart, next-problem, and progress routes
   retain their existing ownership and course-membership boundaries.

Neo4j is optional for conversation: unavailable graph reads degrade to an empty
panel and graph writes are skipped, but the Postgres transcript and questioning
path continue. KG-native mutation routes fail with a structured 503 when a safe
degraded operation is impossible.

## DB-09 tutoring persistence

Tutoring sessions are the `modality = 'tutoring'` subtype of
`app.learning_activities`; the ORM name is `TutoringSession`. Tutoring turns use
`app.tutoring_messages`, and attempts use `app.problem_attempts`. Both children
carry `course_id`, while their Python `session_id` property maps to the physical
`learning_activity_id` foreign key. Problem payload codes remain the public
identifier; persistence resolves them to the numeric `app.problems.id`.
Both the baseline and personalized selectors exclude attempted problems by
either public problem code or numeric database id, because route-level attempt
queries now return the persisted numeric identity.

**A1 is applied:** XP and level are per course. `app.student_progress` is keyed
by `(user_id, course_id)`, `/apollo/progress` requires `search_space_id`, and
the response echoes that course identifier. Session ownership and course
membership are checked before tutoring state is returned or mutated.

## DB-10 curriculum and problem bank

The live curriculum is fully target-native. `Concept` maps to `app.concepts`
and carries `course_id` plus the folded `subject_slug` and
`subject_display_name`; there is no runtime Subject ORM or subject-table join.
Every exposed concept lookup is constrained by course. `Problem` maps to
`app.problems`, where the public problem payload is promoted into typed columns
(`problem_text`, `given_values`, `target_unknown`, `reference_solution`, and
`payload_extra`). Runtime conversion helpers reconstruct the established
Pydantic shape at API boundaries without exposing numeric database identities.

Both tables use persisted bigint identities. Public problem codes remain the
API-facing identifiers; selectors and attempt writers resolve them to the
numeric `app.problems.id` only at the persistence boundary. Provisioning,
authored-set ingestion, generated variants, and active curriculum seeds all
write the folded, course-scoped target rows directly.

## DB-11 question opportunities

Unified questioning persists one `QuestionOpportunity` per
`(attempt_id, reference_node_id)` in `app.question_opportunities`. Each row is
scoped by `course_id` and the tutoring `learning_activity_id`, and combines the
student-facing question timing with the durable per-node learner state,
verbatim evidence, decline memory, and ask counters. The controller loads this
single scoped row set once per turn and constructs the existing response shape
without a compatibility model or a second tally query.

The `state` column is the tally state (`missing`, `tentative`, `understood`, or
`conflicting`). Question-audit closure changes only `answered_turn`; it never
overwrites learner state. `times_asked` remains cumulative, so the first probe
and one confirmation reach two asks for that node, after which the existing
confirm-once policy moves on. Evidence validation and deduplication remain
unchanged.

## DB-13 learner model

The Layer-1/Layer-3 learner-model ORM is retargeted onto the DB-13 app-schema
DDL. `LearnerEntity` maps to `app.learner_entities`, `EntityPrereq` to
`internal.entity_prerequisites`, `LearnerState` to `app.learner_state`, and
`MasteryEvent` to `app.mastery_events`. `LearnerEntity.course_id` and
`EntityPrereq.course_id` are denormalized `NOT NULL` columns (initplan-safe
RLS, matching the rest of the app schema); every mint/seed/upsert call site
(`apollo/provisioning/tag_mint_persist.py`, `scripts/seed_apollo_learner_model.py`,
and their test doubles) threads a `course_id` through construction.
`aliases` (`LearnerEntity`) and `evidence_node_ids` (`MasteryEvent`) are
`TEXT[]` columns now, not JSONB. `misconception_code` is gone from
`LearnerState` and `MasteryEvent` — misconceptions are tracked via
`MasteryEvent.event_kind` instead. `learner_entities__kind__check` also
dropped `'misconception'` from its allowed kinds: the Postgres Layer-1 skill
inventory no longer stores misconception entities AT ALL. Every writer that
used to mint `kind='misconception'` rows (`tag_mint.py`'s auto-provisioning
path, `scripts/seed_apollo_learner_model.py`'s curriculum seeder) now excludes
misconception `EntitySpec`s from the entity upsert entirely — they surface
only as observability (`MintPlan.misconception_keys`, `seed()`'s
`misconceptions_linked` stat, which is now always 0). The opposes-link readers
(`tag_mint_persist.link_opposes` / `drop_unlinkable_minted_misconceptions`,
the seed script's own `_link_opposes`) are kept as permanent no-ops (no row
ever matches `kind == 'misconception'`) rather than removed, so a future
schema reversal would not need them rebuilt.

A6 (`.planning/cleanup/db-plan-amendments-2026-07-20.md`) removed the
`apollo_kg_negotiations` Postgres audit table and the `KGNegotiation` model —
surgically: the three negotiate KG mutations (`mark_node_disputed` /
`paraphrase_node` / `skip_node`, reached via the challenge/paraphrase/skip
routes) and their Neo4j status writes keep working unconditionally; only the
Postgres audit-row write is gone, so `KGStore.get_node_trace` now always
returns an empty `moves` list. `MasteryEvent.negotiation_move` is a distinct,
still-live nullable column (unrelated to the deleted audit table) — the
dormant WU-5A2 in-memory row-spec dataclass (`MasteryEventRowSpec` in
`apollo/learner_model/state_model.py`) mirrors it (writers pass `None`,
matching `apollo/projections/mastery.py`'s live writer) rather than dropping
it, so the spec stays a true 1:1 onto the ORM/DDL column set.

## Done-time grading

The permanent grader of record is the transcript adjudicator plus topic score.
`handle_done` freezes the attempt, loads the complete student/Apollo transcript,
computes per-reference-item credit with validated student evidence spans, maps
that coverage into the existing rubric, computes the topic score, generates the
grounded diagnostic narrative, awards XP, and returns grading provenance.

The topic-score serve flag controls whether the topic score replaces the rubric
overall and adds `topics[]`; computing it remains best-effort and a failure does
not erase the underlying transcript grade. If transcript adjudication fails and
Neo4j is available, the pre-existing semantic coverage fallback remains. If both
sources are unavailable, the route raises `CoverageGradingError` instead of
fabricating an empty-graph grade.

`APOLLO_GRADING_ARTIFACT_ENABLED` optionally writes one canonical artifact for
the transcript/topic result. Artifact persistence and artifact-derived mastery
projection are soft-failing telemetry: neither can change or void the served
grade. The response continues to expose the historical `graph_lane: null` field
for API compatibility.

## A7 ruling

On 2026-07-20, A7 abandoned the experimental student/reference graph grading
roadmap. The comparison engine, Done-time calibration branch, paired artifacts,
findings persistence, audited abstention logic, contraction experiment, retry
worker, findings-driven quarantine sweep, and their deployment switches were
removed. Student KG construction during chat, transcript grading, topic scores,
and reference-graph provisioning/storage are explicitly retained.

The schema still contains historical comparison tables until MIG-AMEND performs
the separately reviewed migration changes. No runtime in `apollo/` writes new
comparison findings or comparison-run rows.

## Reference content and provisioning

Teacher-authored sets and synchronous problem generation remain active. They
ingest and lint problems, reference solutions, concepts, entity links, and
declared solution paths, then project reference entities to Neo4j where needed.
The abandoned upload-triggered auto-provision worker remains removed. Existing
`quarantined_at` values still exclude unsafe problems; A7 only removed the
findings-driven process that automatically changed that value.

DB-12 persists both teacher-facing run types through one
`app.provisioning_runs` model. `kind = 'authored_set'` rows use the course/set
identity and optional problem/solution document pair; `kind = 'generation'`
rows use a concept and may link to an ingest run. Runtime construction and every
lookup are kind-scoped, while the existing authored-set and problem-generation
HTTP response shapes remain unchanged. Authored-set document links are real
`app.documents` foreign keys.

Ingest telemetry is service-only in `internal.content_ingest_runs`,
`internal.content_ingest_errors`, `internal.ingest_page_evidence`, and
`internal.dedup_decisions`. Stable dedup counters and the embedding merge ratio
are typed columns; only the per-concept breakdown remains JSON. The synchronous
path owns these records directly—there is no provisioning queue, worker, or
rejected-problem write seam.

## Security and data boundaries

- Session routes require the authenticated session owner.
- Browse and session creation require course membership; teacher projections and
  authored content routes require the teacher role.
- Reference solutions, private nodes, and rubric vocabulary never enter the
  student-facing browse payload.
- Ordinary logs remain aggregate-only. The explicit debug flag is the sole
  exception and must stay off in production because it can contain bounded draft
  text derived from private reference material.
- Chat-time KG writes remain scoped by attempt id; Postgres records remain scoped
  by user and course.

## Main modules

| Area | Responsibility |
|---|---|
| `handlers/chat.py` | Persist a teaching turn, parse KG updates, run unified questioning. |
| `handlers/done.py` | Transcript/topic grading, XP, retention, artifact capture, provenance. |
| `knowledge_graph/` | Neo4j KG storage, typed graph reads/writes, reference projection. |
| `overseer/` | Coverage, rubric, transcript adjudication, topic scoring, problem selection. |
| `smart_questions/` | Unified tally-aware confused-learner response generation. |
| `provisioning/` | Teacher-authored and generated reference content pipelines. |
| `persistence/` | Apollo ORM models and scoped repositories. |
| `projections/` | Student scorecard, mastery, and teacher classroom read models. |
