---
doc: apollo/conversation/hoot-bridge-reference-answer
description: INTERACTION4 "ask Hoot" hint lane — stateless bridge from an Apollo chat turn into Hoot's scoped QA pipeline for one reference-question aside.
owns:
  - apollo/hoot_bridge/reference_answer.py
related:
  - apollo/conversation/handlers/intent
  - apollo/conversation/handlers/chat
  - apollo/conversation/handlers/done
  - apollo/conversation/session-init
last_verified: 2026-07-26
stub: false
---

# hoot-bridge-reference-answer — the "ask Hoot" hint lane

`apollo/hoot_bridge/reference_answer.py` answers a single mid-teaching
side-question ("wait, what IS a network effect?") through Hoot's own
scoped, citation-backed QA lane, without leaving the Apollo teaching turn.
Gated end-to-end by `INTERACTION4` (default OFF; brief:
`styx/plans/hoot-apollo-04-ask-hoot-hint-lane.md`).

## Interface

- `is_enabled() -> bool` — reads `INTERACTION4` (truthy env var). Consumed by
  `handlers/intent` (gates the classifier label) and `handlers/chat` (gates
  direct-execution wiring). Cheap: no heavy imports at module load, so
  checking the flag never pulls in retrieval/ai/DB machinery.
- `answer_reference_question(*, db, course_id, question, problem) ->
  ReferenceAsideResult` — the stateless compose. Raises on genuine failure
  (network/DB/LLM); never swallows an error into a fake "not found" result,
  so the caller's failure-vs-out-of-scope branches stay distinct.
- `ReferenceAsideResult(in_scope, text, citations)` — persona-free; the
  caller (`handlers/chat`) wraps this in Apollo's resume line.
- `MESSAGE_KIND_REFERENCE_ASIDE` / `ASIDE_MESSAGE_INTENT_TAG` /
  `MAX_ASIDES_PER_SESSION` — the response-envelope tag, the
  `TutoringMessage.intent` tag `handlers/done._full_transcript` excludes on,
  and the per-session aside cap (3), respectively.

## Data flow

`answer_reference_question` composes, per call, with no bundle cache or
chat-turn persistence of its own (Apollo's `TutoringMessage` rows are the
only persistence, written by the caller):

1. `ai.main_ai.check_question_relevance` — the same relevance guard Hoot's
   `/ask` uses. `relevance == "none"` short-circuits to an out-of-scope
   `ReferenceAsideResult` before any retrieval call — scope enforcement is
   never bypassed.
2. `ai.main_ai.extract_and_filter_keywords` (best-effort; empty list on
   failure) feeds `retrieval.pipeline.retrieve_for_question` — the same
   hybrid-search + rerank + store-bias + pack_context lane `/ask` uses.
3. Leakage filter (`_excluded_document_ids` + `_filter_leaked_snippets`):
   drops any snippet whose `document_id` is a course-wide paired solution
   document (`ProvisioningRun.solution_document_id`) or the current
   problem's own paired solution doc (resolved via the problem's
   `provenance["document_id"]` → `ProvisioningRun.problem_document_id`).
   `material_kind` cannot do this — authored-set problem and solution docs
   both index as `"other"` — so `ProvisioningRun` is the only durable
   problem/solution pairing signal.
4. `ai.main_ai.parse_question` → `solve_with_bundle` (the tutor prompt lane,
   `ai/prompts/tutor.py`, pinned `MAIN_MODEL`) → `format_answer`.
5. `_structured_citations` (a private copy of `server.py`'s
   `_structured_citations_from_bundle`, via `citations.formatter`) — kept
   local rather than imported from `server.py`, which this module must not
   route through (brief: no `/ask`, no `ai/router` wiring).

## Invariants & gotchas

- Every exception propagates uncaught — `handlers/chat` owns the "never a
  5xx, apology + fall through as a teaching turn" contract on failure; this
  module only returns gracefully for a **successful** out-of-scope or
  not-found outcome (still an aside, per the brief: "Out-of-scope ⇒ the
  aside says so, teaching resumes").
- `is_enabled()` must stay free of module-level heavy imports — `handlers/
  intent` imports it to decide whether to alter the classifier prompt every
  turn; the retrieval/ai/DB imports live inside `answer_reference_question`.
- Never routes through `server.py` (`/ask`, `_structured_citations_from_bundle`,
  `_prepare_router_context*`) or `ai/router` wiring — those are welded to
  Hoot chat sessions/bundle cache Apollo does not have.

## Related

Gated by and gates `handlers/intent`; executed from and persisted by
`handlers/chat`; excluded from `handlers/done`'s adjudicator transcript;
sibling entry point in `session-init`.
