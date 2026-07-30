---
doc: apollo/projections/_index
description: Router for the read-side projections over the canonical grading artifact (Campaign-plan Phase B).
owns: []
related: []
last_verified: 2026-07-30
stub: false
---

# Apollo projections — read-side reshapes of the grading artifact

Every projection here is a **pure reshape of an already-built / already-persisted
canonical grading artifact** — no fresh grading, resolution, LLM, or computing
DB reads. `handlers/done.py` calls the scorecard + mastery projections after the
artifact row is durable.

## Leaves

| Doc | One-liner | Owns |
|---|---|---|
| [scorecard](scorecard.md) | `render_scorecard` — pure student-scorecard template over the artifact dict | `apollo/projections/scorecard.py` |
| [mastery](mastery.md) | `update_mastery_from_artifact` — flat-EWMA mastery ledger write (+ namespace init) | `apollo/projections/mastery.py`, `apollo/projections/__init__.py` |
| [classroom](classroom.md) | `mastery_heatmap` / `struggle_signals` — teacher-facing SQL aggregations | `apollo/projections/classroom.py` |
| [performance](performance.md) | `class_performance` — teacher class-performance payload over served grade snapshots | `apollo/projections/performance.py` |

## Cross-cutting invariants

- **Nothing computed fresh.** The artifact's ledgers/scores are the answer; a
  projection only formats or aggregates them.
- **Legacy `composite`, not the served grade.** `scorecard` bands and `mastery`
  EWMA both read `scores.composite` / `GradingRun.composite_score` — the legacy
  axis-rubric blend, retained but distinct from the served
  [topic score](../overseer/topic-score.md) grade of record.
- **Failure-isolated.** Each projection owns its own commit domain in `done.py`;
  a projection failure is logged and swallowed, never voiding the served grade.
