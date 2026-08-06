---
doc: apollo/conversation/handlers/grading-artifact-writer
description: apollo/handlers/artifact_writer.py — write_artifacts, the one canonical GradingRun row per Done click (DB-14)
owns:
  - apollo/handlers/artifact_writer.py
related:
  - apollo/conversation/handlers/done
  - apollo/overseer/topic-score
  - apollo/grading/artifact-build
  - apollo/persistence/models
  - apollo/projections/mastery
  - apollo/projections/scorecard
last_verified: 2026-08-06
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
  A re-clicked Done re-grades the same attempt and its INSERT hits this
  constraint — that is an expected soft-fail, not an error path.
- **Soft-fail**: any exception logs and returns `None` — the served grade
  (already committed in `done.py`) is never affected. `done.py`'s
  `_project_mastery` reads the committed row back only *after* this returns.
- **The INSERT runs inside a SAVEPOINT (`begin_nested`)**: an insert failure
  rolls back only the savepoint, expunging the failed row while leaving the
  outer transaction healthy and `attempt`/`sess` unexpired — `done.py` keeps
  reading those instances after this returns, and a full `rollback()` here
  would expire them into lazy-load failures (the 2026-08-05 prod 500: the
  except path itself read `attempt.id` off an expired instance and raised
  `PendingRollbackError` before the cleanup ran). Full rollback is reserved
  for commit-stage failures. `attempt.id` is captured before the `try` for
  the same reason. Regression gate:
  `tests/database/test_done_artifact_route_postgres.py::test_second_done_click_soft_fails_artifact_conflict_instead_of_500`.
- **DRIFT (Appendix A #26): `composite_score` and `node_coverage_score` are
  RETIRED legacy columns.** `_artifact_row` writes `scores.get("composite")` /
  `scores.get("node_coverage")`, but the live builder never sets a `"composite"`
  key, so both persist as `None`. The live grade of record is `topic_score`
  (`overseer/topic-score`) — no leaf may present `scores.composite` as the grade.

## Related

Written by `handlers/done`; built by `grading/artifact-build`; row schema in
`persistence/models`; read downstream by `projections/mastery` and
`projections/scorecard`. The retirement invariant is centralized in `overseer/_index`.
