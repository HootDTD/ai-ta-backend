---
doc: apollo/learner-model/_index
description: Router for the Apollo learner-model sub-area — the pure 3-state Bayesian belief filter and the live session-personalization wedge.
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# apollo / learner-model

The Apollo learner-model package: the PURE 3-state Bayesian belief filter
(WU-5A/5B — no DB/LLM/Neo4j) plus the WU-6A session-personalization wedge.

## Leaf docs

| Doc | One-liner | Owns |
|---|---|---|
| [belief-filter](belief-filter.md) | §3/§6.5 belief math core + frozen value objects + package facade | `belief.py`, `state_model.py`, `__init__.py` |
| [belief-update](belief-update.md) | Per-event `apply_event` orchestration + between-session decay | `update.py`, `decay.py` |
| [personalization](personalization.md) | Learner-profile DB read + pure problem-selection algorithm | `personalization_read.py`, `personalization_select.py` |

## Cross-cutting invariants

- **DORMANT belief filter.** The belief/state_model/update/decay half (and the
  package `__init__`) has **no live external caller** — `apply_event`,
  `event_to_row_specs`, and the `*RowSpec` objects are imported only inside the
  package + tests. The live mastery-event writer is `apollo/projections/mastery`,
  which does NOT use them.
- **The one live-wired half is personalization.** `personalization_read` +
  `personalization_select` are consumed by `apollo/overseer/problem-selector` —
  the ONLY external consumer of this package.
- **Constants are LOCKED** across the pure modules (cold-start prior, γ, floor,
  decay k, teachable band) — do not re-derive.

## Related

`apollo/persistence/models` (`LearnerState`/`MasteryEvent`/`LearnerEntity`
targets), `apollo/grading/event-model` (the `LearnerEvent` input),
`apollo/overseer/problem-selector` (the live consumer), `apollo/projections/mastery`
(the live writer).
