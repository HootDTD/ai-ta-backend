---
doc: apollo/projections/performance-problems
description: performance_problems — the class-performance payload's per-problem problems[] block (best-wins letter distribution, full text, per-problem student list, and the per-reference-node right/wrong/unprobed breakdown that reuses the served topic score's own credit helper).
owns:
  - apollo/projections/performance_problems.py
related:
  - apollo/projections/performance
  - apollo/projections/performance-insights
  - apollo/overseer/topic-score
  - apollo/conversation/handlers/done
last_verified: 2026-08-11
stub: false
---

# Projections performance-problems — per-problem drill-down block

Owns the whole `problems[]` block of the teacher class-performance payload
(design spec 2026-07-31 v2.1 addendum). Everything but two thin DB loaders is a
**pure function on plain rows / prebuilt nodes**, so the projection tests
exercise it with hand-computed fixtures and no database. Composed by
[performance](performance.md)'s assembler.

## Interface

- **Pure builders:** `letter_distribution(best_rows)` (best-wins letter counts
  over every `LETTER_BANDS` band, zeros included — also reused by
  [performance](performance.md) for the class-level `grade_distribution`);
  `students_for(rows, identities, aggregates=None)` (per-problem best-wins
  `{user_id, email, score, letter, attempts, median_gap_seconds}`, score desc,
  id tie-break); `aggregate_nodes(graded_nodes, attempts)` (per graded node,
  understood/partial/missed/`unprobed`/`graded` counts over
  `AttemptNodes(coverage, unprobed)` records); `build_problems(best_rows,
  meta_by_problem, identities, graded_nodes_by_problem, aggregates=None)`
  (assembles the ordered rows: `problem_text`, distribution, `students`,
  `nodes`).
- **Value object:** `AttemptNodes(coverage, unprobed=frozenset())` — one
  included attempt's stored coverage plus the node ids that attempt's OWN grade
  excluded; `_unprobed_ids` parses the latter defensively from the row.
- **DB loaders:** `load_problem_meta(db, *, problem_ids)` →
  `{problem_id: {problem_code, problem_text, concept_id, concept_name}}`;
  `load_graded_reference_nodes(db, *, problem_ids)` →
  `{problem_id: [graded Node, ...]}`.

## Data flow

`load_problem_meta` joins `app.problems` → `app.concepts` for code/full text/
concept. `load_graded_reference_nodes` reassembles each problem's reference graph
the way `done.py` / [topic-score](../overseer/topic-score.md) do
(`Problem.to_kg_graph`) and keeps only the graded node types the scorer grades.
`build_problems` groups the assembler's best-wins rows by problem; each row's
`nodes` are tallied over that problem's best attempts' stored
`diagnostic_report -> 'coverage'` PLUS that attempt's
`diagnostic_report -> 'unprobed_node_ids'` (both threaded in by
[performance](performance.md)'s `_best_graded_rows`, both written by
[done](../conversation/handlers/done.md)).

## Invariants & gotchas

- **Node credit is the served grade's, not a copy.** `aggregate_nodes` derives
  each node's covered/partial/missing status by REUSING
  `topic_score._credit_for_node` over `coverage.per_step` +
  `coverage.procedure_scores`, plus the same `_GRADED_NODE_TYPES` set and
  `_display_name_for` the scorer uses (imported directly, mirroring
  `transcript_coverage.py`) — so the drill-down can never disagree with the
  grade the student was shown. Status maps covered→understood, partial→partial,
  missing→missed.
- **…but that helper is not the whole verdict** (2026-08-08 review fix). It can
  only return covered/partial/missing, and since P1.2b a graded node may instead
  have been EXCLUDED from that attempt's grade. Re-deriving from `coverage`
  alone therefore reported a class-wide `missed` on a topic no grade counted and
  nobody was asked, and the stacked bar stopped reconciling with the letter
  distribution beside it. Each attempt's own excluded key set arrives via
  `AttemptNodes.unprobed`, gets its OWN `unprobed` count, and is left OUT of
  `graded` — now understood+partial+missed, identical to the attempt count for
  every pre-P1.2b row.
- **Attempts with no usable coverage are excluded.** A coverage lacking a
  non-empty `per_step`/`procedure_scores` (e.g. a pre-topic-score snapshot) is
  dropped so a node is never counted `missed` for want of data. A problem with
  graded best rows but zero usable coverages still lists its reference nodes
  with all-zero counts. A malformed/absent `unprobed_node_ids` degrades to the
  empty set (the coverage-only tally), never an exception on the panel.
- **Reference-graph load is failure-isolated per problem** — a malformed
  reference solution degrades to an empty node list for that problem, never
  voiding the payload. The `to_kg_graph` `attempt_id` stamp is irrelevant to
  node identity, so a placeholder is passed.
- **Roster-bounded.** A course is tens of students / a handful of problems, so
  full `problem_text` and per-problem student lists are not a cross-course
  export.
- **Per-pair retry decoration is display-only (P3.3).** `attempts` (graded
  count) and `median_gap_seconds` come from the SAME
  `performance_insights.ProblemAgg` map the insights block uses, matched on
  (user_id, problem_id) — pair-grained, never that student's cross-problem
  totals. Absent aggregate → `(1, None)`, the truthful floor for a row that
  exists only because a graded attempt does. Neither field participates in
  ordering or in the served grade.
