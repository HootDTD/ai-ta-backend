---
doc: apollo/projections/classroom
description: Pure teacher-facing classroom aggregations — mastery heatmap and windowed struggle signals.
owns:
  - apollo/projections/classroom.py
related:
  - apollo/projections/mastery
  - apollo/grading/artifact-build
  - apollo/conversation/handlers/done
  - apollo/conversation/routing/router
last_verified: 2026-08-12
stub: false
---

# Projections classroom — teacher aggregations

Two pure read-side aggregations backing the teacher classroom endpoints
registered in `apollo/api.py` (teacher-role gated). No new grading, inference,
LLM, or Neo4j.

## Interface

- `mastery_heatmap(db, *, search_space_id) -> list[dict]` — roster × concept
  mastery grid.
- `struggle_signals(db, *, search_space_id, window_days=14) -> dict` — windowed
  abstention/fallback counts, lowest-coverage nodes
  (`{key, mean_coverage, n, n_unprobed}`), top misconceptions.

## Data flow

`mastery_heatmap` averages `app.learner_state` rows per `(user_id, concept_id)`
joined through `app.learner_entities`. `struggle_signals` runs raw SQL directly
over `internal.grading_runs` for the course window: `FILTER`ed counts plus
`jsonb_array_elements` lateral expansion over `node_ledger` and
`grader_payload -> 'misconceptions'`.

## Invariants & gotchas

- **Pure aggregation over already-durable rows** — reads `grading_runs` directly,
  never re-grades.
- **Bounded worst-offender lists** (limit 10 each), not full exports.
- **Coverage attribution excludes student-id-keyed `unresolved` rows**; a
  never-taught concept (`unresolved` with `evidence_span IS NULL`) still counts as
  a 0.0-coverage contribution.
- **`unprobed` rows stay in the lowest-coverage signal** (2026-08-08 review fix).
  P1.2b re-filed "a graded node the questioning loop never raised" from that
  `unresolved` + NULL-span branch onto its own ledger status
  ([artifact-build](../grading/artifact-build.md)); dropping it here would have
  silently deleted the exact worst-offender signal the branch exists for. The
  status is matched explicitly (constant IMPORTED, never re-spelled) and still
  contributes 0.0, with an additive per-key `n_unprobed` count so the teacher can
  read "the class got this wrong" apart from "Apollo never asked".
- **`top_misconceptions` counts only TEACHER-VISIBLE entries** (P3.2 §2.5/S10,
  `_TEACHER_VISIBLE_MISCONCEPTION_SQL`). The array is persisted from wrongness
  level 1 for INTERNAL readers (cross-attempt question memory, the XP dedup —
  [done](../conversation/handlers/done.md)), so without a predicate this teacher
  aggregate would light up two rungs below the ≥3 S10 names. Two exclusions:
  entries marked `shadow` (written below level 3), and `resolved` ones (a
  contradiction the student FIXED — persisted only for the XP dedup; a success,
  not a struggle signal). `IS DISTINCT FROM 'true'` is load-bearing, NOT
  `!= 'true'`: `->>` on an absent key is NULL and `NULL != 'true'` is NULL, which
  would silently drop every pre-P3.2 row. The marker key is re-spelled here
  rather than imported (keeping this pure projection out of the `done` → Neo4j
  chain, the same convention [scorecard](scorecard.md) uses) and pinned equal by
  `tests/test_misconception_surfaces_light_up.py`; the SQL itself is gated on real
  Postgres by `tests/database/test_wrongness_teacher_surfaces_postgres.py`.
- **Dual-grader SQL, LLM-only reality.** The `abstention_count` /`fallback_count`
  filters model a graph/shadow grader (`grader_used='graph'`, `role='pair'`) and
  the docstrings reference `build_graph_artifact` / ledger helpers that no longer
  exist — with the shadow chain off, only the `llm_fallback` canonical rows
  populate, so these counts are effectively 0 on the live path.
