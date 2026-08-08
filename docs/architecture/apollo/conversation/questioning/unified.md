---
doc: apollo/conversation/questioning/unified
description: One-call engine that updates the per-node tally and generates Apollo's question, with log-only belt telemetry.
owns:
  - apollo/smart_questions/unified.py
related:
  - apollo/conversation/questioning/controller
  - apollo/conversation/questioning/selection
  - apollo/conversation/agent/output-filter
  - apollo/ontology/graph
  - apollo/schemas/problem
last_verified: 2026-08-07
stub: false
---

`apollo/smart_questions/unified.py` (SOURCE folder `smart_questions/`, re-homed to
`questioning/` in this doc tree) is the **live** reply + leakage path that replaced
the persona/output-filter chain.

## Interface

- `evaluate_and_ask(*, transcript, reference_graph, problem, tally_state, budget) -> UnifiedQuestionResult`
  (async) — imported by `questioning/controller`.
- `question_cap() -> int`.
- Value objects: `EvidenceQuote`, `TallyState`, `TallyUpdate`, `QuestionBudget`,
  `UnifiedQuestionResult` (all in `__all__`), plus `SelectionPolicy` /
  `build_selection_policy` / `MAX_ASKS_PER_NODE` re-exported from
  `questioning/selection`.

## Data flow

A **single** LLM call both updates the durable per-node tally AND decides Apollo's
next move. The hard budget check comes first (`questions_asked >= cap` → `done`).
`_build_payload` serializes the payload (public problem + `_public_question_parts`
+ private reference nodes **ordered graded-first, each flagged `graded`** + edges +
`_serialize_tally` + budget block
`{questions_asked, cap, reserved_for_graded, askable_node_ids}` + indexed
transcript), then `_call_unified` (`bounded_client()` — `agent/llm-client`,
2026-08-04, was a bare `OpenAI()` — `json_schema` = `_schema()`; model from
`APOLLO_UNIFIED_QUESTION_MODEL`; `reasoning_effort` set for reasoning models via
`_is_reasoning_model`) runs on a thread. `_decode`/`_decode_updates` validate tally
updates — every non-`missing` update needs a `_validated_evidence` verbatim
student-turn quote, and a rejected quote is logged
(`apollo_question_evidence_rejected`). The policy is then re-resolved WITH this
turn's updates (`build_selection_policy`); an empty `askable_ids` forces `done`.
`_resolve_served_reply` picks what is served, spending **at most one** regenerate on
either an off-policy target (`_off_policy_feedback` names the askable ids) or a
malformed shape (`_MALFORMED_FEEDBACK`), then falling back to
`_fallback_public_question` for `askable_ids[0]`. The **belt**
(`_belt_verdict`/`_private_content_violations`/`_leaks_private_content`) scans the
reply for private digits/vocabulary/phrases over `_private_strings(reference_graph)`.
`_log_decision`/`_log_debug_cycle` emit telemetry.

## Invariants & gotchas

- **The belt is telemetry-only** (`belt_hit_served` metric) — it records possible
  private-data leakage but never rewrites a valid reply and never hard-filters
  (contrast the dead `agent/output-filter`, which raised).
- **The target policy is enforced in code, not in the prompt** (P1.2a/P2.4): the
  served target must be in `policy.askable_ids` — graded nodes first, nodes already
  `understood` or probed `MAX_ASKS_PER_NODE` times excluded, ungraded nodes hidden
  once the remaining budget is down to the graded reservation. The prompt states the
  same contract so the model usually complies without a regenerate. Authority:
  `questioning/selection`.
- **LLM calls per turn stay ≤ 2**: off-policy and malformed share the single
  regenerate slot. `fallback_reason` distinguishes `off_policy_regenerated` /
  `off_policy_exhausted` / `malformed_regenerated` / `malformed_exhausted` /
  `budget_exhausted` / `no_probeable_node`.
- Evidence must be **verbatim in the transcript** (`_validated_evidence` normalizes
  then substring-matches student turns); manufactured/paraphrased evidence is dropped.
  This normalized check is the **single** evidence validator — `controller` no longer
  re-checks (P2.4).
- `student_declined` is **gone** from the schema, prompt and value objects: false on
  every prod row, and the code-enforced ask cap covers what it was meant to do. The
  DB column survives at its `false` default for backward compatibility.
- Payload fields are treated as untrusted data, never instructions (prompt-injection
  guard).

## Env flags

`APOLLO_UNIFIED_QUESTION_MODEL`, `APOLLO_UNIFIED_QUESTION_REASONING_EFFORT`,
`APOLLO_UNIFIED_QUESTION_CAP`, `APOLLO_UNIFIED_QUESTION_DEBUG_LOG` — each with a
hardcoded default fallback.

## Related

Target policy `questioning/selection`; persistence orchestration
`questioning/controller`; dead predecessor `agent/output-filter`; `KGGraph` authority
`ontology/graph`; problem/reference-graph `schemas/problem`.
