---
doc: ai-ta-backend/apollo/conversation/handlers/grading-artifact-writer
description: apollo/handlers/artifact_writer.py — write_artifacts, the one canonical GradingRun row per Done click (DB-14)
owns:
  - apollo/handlers/artifact_writer.py
related:
  - ai-ta-backend/apollo/conversation/handlers/done
  - ai-ta-backend/apollo/overseer/topic-score
  - ai-ta-backend/apollo/grading/artifact-build
  - ai-ta-backend/apollo/persistence/models
  - ai-ta-backend/apollo/projections/mastery
  - ai-ta-backend/apollo/projections/scorecard
last_verified: 2026-07-25
stub: false
---

# handlers/grading-artifact-writer — the canonical artifact row

`apollo/handlers/artifact_writer.py` persists the single canonical
transcript/topic `GradingRun` row per Done click (DB-14 shape).

## Interface

- `write_artifacts(db, *, attempt, sess, coverage, rubric, latency_ms, topic_score=None) -> dict | None`
  — builds the payload via `build_llm_artifact` (`grading/artifact-build`), maps
  it onto a `GradingRun` row, flushes + commits, and **returns the canonical
  payload** (or `None` on failure). Called only by `handlers/done`.
- `_artifact_row(*, attempt, sess, payload) -> GradingRun` — the internal
  payload-dict → typed-column mapping.

## Data flow

`done.py` → `write_artifacts` → `_artifact_row`. `versions`/`scores`/`abstention`
are stored whole in their `*_details` JSONB columns AND have query-friendly
scalars lifted into typed columns. `misconceptions` + `clarification_trace` have
no dedicated columns in the target DDL, so they nest under the catch-all
`grader_payload` JSONB. The reciprocal reader is
`campaign/cast-student`'s `SqlArtifactReader._row_to_payload`.

## Invariants & gotchas

- **Append-only `UNIQUE(attempt_id, role, grader_version)`**; `role` is always
  `"canonical"`; `problem_id` is a real `BigInteger` FK to `app.problems.id`.
- **Soft-fail**: any exception logs, rolls back, and returns `None` — the served
  grade (already committed in `done.py`) is never affected. `done.py`'s
  `_project_mastery` reads the committed row back only *after* this returns.
- **DRIFT (Appendix A #26): `composite_score` and `node_coverage_score` are
  RETIRED legacy columns.** `_artifact_row` writes `scores.get("composite")` /
  `scores.get("node_coverage")`, but the live builder never sets a `"composite"`
  key, so both persist as `None`. The live grade of record is `topic_score`
  (`overseer/topic-score`) — no leaf may present `scores.composite` as the grade.

## Related

Written by `handlers/done`; built by `grading/artifact-build`; row schema in
`persistence/models`; read downstream by `projections/mastery` and
`projections/scorecard`. The retirement invariant is centralized in `overseer/_index`.
