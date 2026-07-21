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
