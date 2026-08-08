---
doc: apollo/conversation/questioning/unified
description: One-call engine that updates the per-node tally and generates Apollo's question, under the code-enforced target policy.
owns:
  - apollo/smart_questions/unified.py
related:
  - apollo/conversation/questioning/controller
  - apollo/conversation/questioning/selection
  - apollo/conversation/questioning/leakage
  - apollo/conversation/agent/output-filter
  - apollo/ontology/graph
  - apollo/schemas/problem
last_verified: 2026-08-07
stub: false
---

`apollo/smart_questions/unified.py` (SOURCE folder `smart_questions/`, re-homed to
`questioning/` in this doc tree) is the **live** reply path; it replaced persona/output-filter.

## Interface

- `evaluate_and_ask(*, transcript, reference_graph, problem, tally_state, budget) -> UnifiedQuestionResult`
  (async) — imported by `questioning/controller`. `question_cap() -> int`.
- Value objects: `EvidenceQuote`, `TallyState`, `TallyUpdate`, `QuestionBudget`,
  `UnifiedQuestionResult` (all in `__all__`). `UnifiedQuestionResult.fallback_served`
  tells the controller the served text is a degenerate public clause, not a probe.
- The target policy and the belt are **imported, not re-exported**:
  `questioning/selection` / `questioning/leakage` are their authorities.

## Data flow

A **single** LLM call both updates the durable per-node tally AND decides Apollo's
next move. The hard budget check comes first (`questions_asked >= cap` → `done`).
`_build_payload` serializes public problem + `_public_question_parts` + private
reference nodes **ordered graded-first, each flagged `graded`** + edges +
`_serialize_tally` + budget block
`{questions_asked, cap, reserved_for_graded, askable_node_ids}` + indexed transcript.
`_call_unified` (`bounded_client()` — `agent/llm-client`; `json_schema` = `_schema()`;
model from `APOLLO_UNIFIED_QUESTION_MODEL`; `reasoning_effort` for reasoning models
via `_is_reasoning_model`) runs on a thread. `_decode`/`_decode_updates` validate
tally updates — every non-`missing` update needs a `_verbatim_span` hit inside the
cited student turn; a rejected quote is logged (`apollo_question_evidence_rejected`).
The policy is re-resolved WITH this turn's updates; an empty `askable_ids` forces
`done`. `_resolve_served_reply` then picks what is served, spending **at most one**
regenerate on an off-policy target (`_off_policy_feedback` names the askable ids)
and/or a malformed shape (`_MALFORMED_FEEDBACK`), falling back to
`_fallback_public_question` for `askable_ids[0]`. `_log_decision`/`_log_debug_cycle`
emit telemetry.

## Invariants & gotchas

- **The belt is telemetry-only** (`belt_hit_served`): it records possible leakage but
  never rewrites or blocks a reply (contrast the dead `agent/output-filter`, which
  raised). Only its `malformed` verdict changes control flow here.
- **The target policy is enforced in code, not in the prompt** (P1.2a/P2.4): the
  served target must be in `policy.askable_ids` — graded first, `understood`-or-capped
  excluded, ungraded hidden once the budget is down to the graded reservation. The
  prompt states the same contract (cap **interpolated** from `MAX_ASKS_PER_NODE`, so
  prose cannot desynchronize), so the model usually complies without a regenerate.
- **LLM calls per turn stay ≤ 2**: off-policy and malformed share the one regenerate
  slot, and a draft that is BOTH gets both corrections in a single feedback message —
  sending only the target fix wasted the retry and discarded a repairable reply.
  `fallback_reason` ∈ `off_policy_regenerated` / `off_policy_exhausted` /
  `malformed_regenerated` / `malformed_exhausted` / `budget_exhausted` /
  `no_probeable_node`.
- **A `*_exhausted` fallback is not a probe**: it serves a verbatim public clause, so
  the result carries `fallback_served=True` and `controller` skips the `times_asked`
  bump. Charging it let two bad turns exhaust a thin rubric's only graded node, empty
  `askable_ids`, force `done` and auto-grade an unprobed topic as 0. Residual: a
  non-compliant model re-serves the clause without advancing the budget — watch
  `fallback_reason=*_exhausted` with `repeated_question_served=True`.
- **Evidence is the STUDENT's raw span, not the model's rendering.** `_verbatim_span`
  matches the quote word-for-word (case/punctuation-insensitive, `leakage.normalized`
  tokenizer) inside the cited student turn and returns that slice — `done.py` feeds
  these quotes to the adjudicator as "the student said" (P1.3), so a cleaned-up
  rendering would attribute words never typed. **Single** validator: `controller` no
  longer re-checks (P2.4).
- **Forced `done` is the cap's terminal state, not new policy**: `askable_ids` empties
  exactly when every node is `understood` or capped — the state the prompt has always
  called `done` in, and the one where no legal target exists. `handlers/chat` maps it
  to `handle_done(auto_done=True)`, an immediate grade stamped by P0.4. Telemetry:
  `fallback_reason=no_probeable_node`.
- `student_declined` is **gone** from schema, prompt and value objects: false on every
  prod row, the cap covers its purpose; the DB column survives at its `false` default.
- Payload fields are untrusted data, never instructions (prompt-injection guard).

## Env flags

`APOLLO_UNIFIED_QUESTION_MODEL`, `APOLLO_UNIFIED_QUESTION_REASONING_EFFORT`,
`APOLLO_UNIFIED_QUESTION_CAP`, `APOLLO_UNIFIED_QUESTION_DEBUG_LOG` — each defaulted.

## Related

Policy `questioning/selection`; belt `questioning/leakage`; persistence
`questioning/controller`; dead `agent/output-filter`; `ontology/graph`; `schemas/problem`.
