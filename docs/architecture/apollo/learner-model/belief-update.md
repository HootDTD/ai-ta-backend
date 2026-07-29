---
doc: apollo/learner-model/belief-update
description: The pure per-event apply_event orchestration and between-session decay layered on the belief math core.
owns:
  - apollo/learner_model/update.py
  - apollo/learner_model/decay.py
related:
  - apollo/learner-model/_index
  - apollo/learner-model/belief-filter
  - apollo/grading/event-model
  - apollo/persistence/models
  - apollo/projections/mastery
last_verified: 2026-07-25
stub: false
---

# apollo/learner-model/belief-update

The pure orchestration + between-session decay layered on the `belief-filter`
math core. No IO.

## Interface

- **`update.apply_event(event, *, prior_belief, prior_last_evidence_at,
  parser_confidence, grader_confidence, done_ts) → BeliefUpdate`** — threads
  `likelihood_for_event → damp → bayes_update → mastery_of/confidence_of/
  misconception_code_of`. The ONLY place `q` is formed is
  `q = parser_confidence · grader_confidence` (`event.confidence` is NOT
  re-multiplied — the COVERED score is already resolution-scaled, folding it in
  would double-count). A `None` prior falls back to `COLD_START_PRIOR`;
  `dt_days_since_last` is the whole-day gap to `done_ts` and is RECORDED but no
  decay is applied here.
- **`update.event_to_row_specs(event, update, *, user_id, search_space_id,
  entity_id, attempt_id) → (MasteryEventRowSpec, LearnerStateRowSpec)`** — the
  WU-5A2 hand-off seam. Identity ids pass through defaulting `None`;
  `negotiation_move` is always `None` (mirrors `apollo/projections/mastery`);
  neither spec carries `misconception_code` (DB-13).
- **`decay.DECAY_K = 0.05`** (LOCKED, ~14-day half-life); **`decay_weight(dt_days,
  k) = 1 − e^(−k·max(0, dt_days))`** (the `max(0, ·)` clamp kills negative-dt
  sharpening from clock skew; `dt=0` → identity); **`decay_toward_prior(belief,
  prior, dt_days, k)`** — the §3 Step-0 convex blend `(1−w)·belief + w·prior`
  (sum-1 preserved; repairs zeros since every prior component ≥ 0.20). The caller
  passes `COLD_START_PRIOR` in — `decay.py` imports nothing from `belief.py`.

## Data flow

`apply_event` consumes a frozen `LearnerEvent` (`apollo/grading/event-model`) +
the belief math from `belief-filter`, and emits a `BeliefUpdate`;
`event_to_row_specs` maps that onto the two `*RowSpec` objects that mirror
`app.mastery_events` / `app.learner_state`.

## Invariants & gotchas

- **PURE** — no DB/LLM/Neo4j; builds NEW value objects, never mutates inputs;
  constants LOCKED.
- **DORMANT** — `apply_event`/`event_to_row_specs` have no live external caller
  (imported only inside the package + tests); the live mastery writer is
  `apollo/projections/mastery`.
- DRIFT: `decay.py`'s docstring references a nonexistent `learner_update.py` (a
  closure that no longer exists) — ignore it; decay is a standalone pure fn.

## Related

`apollo/learner-model/belief-filter` (the math core + value objects),
`apollo/grading/event-model` (`LearnerEvent`), `apollo/persistence/models`
(`MasteryEvent`/`LearnerState`).
