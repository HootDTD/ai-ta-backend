---
doc: apollo/learner-model/belief-filter
description: The pure 3-state Bayesian belief-filter math core plus its frozen pre-DB value objects and the package re-export facade.
owns:
  - apollo/learner_model/belief.py
  - apollo/learner_model/state_model.py
  - apollo/learner_model/__init__.py
related:
  - apollo/learner-model/_index
  - apollo/learner-model/belief-update
  - apollo/grading/event-model
  - apollo/persistence/models
  - apollo/projections/mastery
last_verified: 2026-07-25
stub: false
---

# apollo/learner-model/belief-filter

The PURE 3-state Bayesian belief-filter math over the belief vector
`(p_misc, p_shaky, p_mastered)` (spec §3 / §6.5), plus the frozen value objects a
persister would consume. No IO. Imports only `apollo.grading.event_model`.

## Interface

**`belief.py` — LOCKED constants** (do not re-derive): `COLD_START_PRIOR =
(0.20, 0.60, 0.20)`, `GAMMA = 1.5`, `LIKELIHOOD_FLOOR = 0.02` (BKT boundary
floor, clamped per-component to kill the absorbing-zero at coverage extremes),
`MISCONCEPTION_FLAG_THRESHOLD = 0.5`, the fixed `MISSING`/`MISCONCEPTION`/
`CORRECTED` likelihood rows, and `NO_OP_LIKELIHOOD = (1,1,1)`.

**`belief.py` — 6 math fns:** `likelihood_for_event` (dispatch on
`LearnerEventKind`; COVERED uses the resolution-scaled `score` verbatim, PARTIAL
maps to covered@`AMBIGUOUS_ORDER_SCORE`), `damp` (§3.1 linear damper
`q·L + (1-q)·NO_OP`), `bayes_update` (`normalize(prior ⊙ L)`, zero-sum guard
returns the prior), `mastery_of` (`0.5·p_shaky + p_mastered`), `confidence_of`
(`1 − normalized entropy`), `misconception_code_of` (two-step flag: the event's
code only when `p_misc` is argmax AND ≥ threshold AND a code is present).

**`state_model.py` — frozen pre-DB value objects** (the dormant WU-5A2 persister
would consume these): `BeliefUpdate` (one `apply_event` result — prior/posterior
belief, mastery/confidence readouts, two-step `misconception_code`, parser/grader
confidences, recorded-but-unapplied `dt_days_since_last`); `MasteryEventRowSpec`
(1:1 onto `app.mastery_events` non-id columns; identity ids default `None`;
carries `negotiation_move` nullable-live; **no `misconception_code`**, DB-13);
`LearnerStateRowSpec` (1:1 onto `app.learner_state` belief columns; omits
`last_evidence_at` as a persist-time concern; **no `misconception_code`**).

**`__init__.py` (facade):** re-exports the belief constants + fns, the
`state_model` value objects, and `update.apply_event`/`event_to_row_specs`
(guarded by the package-seam test).

## Data flow

`apply_event` (owned by `belief-update`) threads these fns:
`likelihood_for_event → damp → bayes_update → mastery_of/confidence_of/
misconception_code_of`, producing a `BeliefUpdate`. The `*RowSpec` objects mirror
the `app.learner_state` / `app.mastery_events` DDL columns 1:1.

## Invariants & gotchas

- **PURE** — no DB/LLM/Neo4j/network; immutable frozen dataclasses.
- **Constants LOCKED** — the floor is clamp-EACH-component (not an s-clamp), and
  the cold-start `mastery_of == 0.50` (the §3 prose "0.40" was an arithmetic
  error, corrected).
- **DORMANT** — no live external consumer imports this half; the live mastery
  writer is `apollo/projections/mastery`.
- DRIFT: `state_model.py` docstrings cite absolute line anchors into `models.py`
  (e.g. `models.py:837-899`) — reference the models by NAME, never by line.

## Related

`apollo/learner-model/belief-update` (`apply_event` uses these),
`apollo/grading/event-model` (`LearnerEvent`/`LearnerEventKind`/
`AMBIGUOUS_ORDER_SCORE`), `apollo/persistence/models` (`LearnerState`/
`MasteryEvent` DDL these row specs mirror).
