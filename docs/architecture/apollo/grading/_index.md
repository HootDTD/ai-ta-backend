---
doc: apollo/grading/_index
description: Router for the shared grading value objects and canonical artifact-builder helpers.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# Apollo grading — shared value objects & artifact builder

A small, dependency-light package holding the **shapes** the grading path
shares, not the grading logic itself (that lives in
[overseer](../overseer/_index.md) + the
[done](../conversation/handlers/done.md) orchestrator). The package facade
`__init__.py` re-exports both leaves' public symbols.

## Leaves

| Doc | One-liner | Owns |
|---|---|---|
| [artifact-build](artifact-build.md) | `build_llm_artifact` — the pure canonical transcript/topic artifact builder (+ facade) | `apollo/grading/artifact_build.py`, `apollo/grading/__init__.py` |
| [event-model](event-model.md) | `LearnerEvent` / `LearnerEventKind` — the frozen learner-model event value object | `apollo/grading/event_model.py` |

## Cross-cutting invariants

- **Pure shapes only.** Neither leaf performs IO; both are consumed by the
  handlers/overseer/learner-model modules that own the behavior.
- **`build_llm_artifact` is the single artifact shape.** The dict it returns is
  what `handlers/artifact_writer.py` persists to `internal.grading_runs`, what
  [projections/scorecard](../projections/scorecard.md) templates over, and what
  `campaign.cast.student.SqlArtifactReader` reconstructs off a row.
