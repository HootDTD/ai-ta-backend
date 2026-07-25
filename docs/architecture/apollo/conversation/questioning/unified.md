---
doc: apollo/conversation/questioning/unified
description: One-call engine that updates the per-node tally and generates Apollo's question, with log-only belt telemetry.
owns:
  - apollo/smart_questions/unified.py
related:
  - apollo/conversation/questioning/controller
  - apollo/conversation/agent/output-filter
  - apollo/ontology/graph
  - apollo/schemas/problem
last_verified: 2026-07-25
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
  `UnifiedQuestionResult` (all in `__all__`).

## Data flow

A **single** LLM call both updates the durable per-node tally AND decides Apollo's
next move. The hard budget check comes first (`questions_asked >= cap` → `done`).
It serializes the payload (public problem + `_public_question_parts` + private
reference nodes/edges + `_serialize_tally` + budget + indexed transcript), calls
`_call_unified` (`OpenAI` `json_schema` = `_schema()`; model from
`APOLLO_UNIFIED_QUESTION_MODEL`; `reasoning_effort` set for reasoning models via
`_is_reasoning_model`) on a thread. `_decode`/`_decode_updates` validate tally
updates — every non-`missing` update needs a `_validated_evidence` verbatim
student-turn quote. The **belt** (`_belt_verdict`/`_private_content_violations`/
`_leaks_private_content`) scans the reply for private digits/vocabulary/phrases over
`_private_strings(reference_graph)`. `_student_reply` joins acknowledgement +
question; a malformed shape (not exactly one `?`) triggers **one** regenerate, then
`_fallback_public_question`. `_log_decision`/`_log_debug_cycle` emit telemetry.

## Invariants & gotchas

- **The belt is telemetry-only** (`belt_hit_served` metric) — it records possible
  private-data leakage but never rewrites a valid reply and never hard-filters
  (contrast the dead `agent/output-filter`, which raised).
- **Confirm-once budgeting:** the system prompt probes any node at most twice;
  `done` when coverage is sufficient, the student signals done, or no probeable
  node remains.
- Evidence must be **verbatim in the transcript** (`_validated_evidence` normalizes
  then substring-matches student turns); manufactured/paraphrased evidence is dropped.
- Payload fields are treated as untrusted data, never instructions (prompt-injection
  guard).

## Env flags

`APOLLO_UNIFIED_QUESTION_MODEL`, `APOLLO_UNIFIED_QUESTION_REASONING_EFFORT`,
`APOLLO_UNIFIED_QUESTION_CAP`, `APOLLO_UNIFIED_QUESTION_DEBUG_LOG` — each with a
hardcoded default fallback.

## Related

Persistence orchestration `questioning/controller`; dead predecessor
`agent/output-filter`; `KGGraph` authority `ontology/graph`; problem/reference-graph
`schemas/problem`.
