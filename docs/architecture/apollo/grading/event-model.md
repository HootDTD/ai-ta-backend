---
doc: apollo/grading/event-model
description: The frozen learner-model event value object mapping 1:1 onto app.mastery_events.
owns:
  - apollo/grading/event_model.py
related:
  - apollo/projections/mastery
  - apollo/learner-model/belief-update
  - apollo/persistence/models
last_verified: 2026-07-25
stub: false
---

# Grading event model — LearnerEvent value object

The WU-4B2 §6.4 frozen learner-model event, kept as a tiny dependency-light
module separate from the decision-table logic so the data shape stands alone.

## Interface

- `LearnerEvent` (frozen dataclass) — one in-memory learner-model event.
- `LearnerEventKind` (`StrEnum`: `covered` / `missing` / `partial` /
  `misconception` / `corrected`).
- `EVENT_CONVERSION_VERSION`, `AMBIGUOUS_ORDER_SCORE` module constants.

Imported by `apollo.learner_model.{belief,update}` and re-exported through the
[grading facade](_index.md).

## Data flow

A `LearnerEvent` maps 1:1 onto `app.mastery_events` columns (`canonical_key` /
`event_kind` / `score` / `confidence` / `misconception_code` /
`evidence_node_ids` / `reference_step_id`). This unit **produces in-memory events
only** — the Bayesian belief columns (`parser_confidence` / `grader_confidence` /
`prior_belief` / `posterior_belief` / `mastery_after`) are filled at WU-5A
persistence, not here.

## Invariants & gotchas

- **Immutable.** The §6.5 conversion table only ever returns new events.
- **`LearnerEventKind` value-set == `models.MASTERY_EVENT_KINDS`**, asserted by
  test so the enum and the documentation tuple can never drift.
- `diagnostic_flags` (`edge-gap` / `mixed-understanding`) are carried as data
  only — not a DB column.
