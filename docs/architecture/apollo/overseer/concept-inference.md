---
doc: apollo/overseer/concept-inference
description: Isolated LLM call mapping a Hoot transcript to one course concept_id for problem selection.
owns:
  - apollo/overseer/concept_inference.py
related:
  - apollo/overseer/problem-selector
  - apollo/conversation/curriculum/db
  - apollo/conversation/session-init
last_verified: 2026-07-31
stub: false
---

# Overseer concept inference — transcript → concept_id

`infer_concept_id` is an isolated LLM call that maps a Hoot conversation to
exactly one `concept_id` from the course's candidate concepts, or raises. Apollo
never sees this output — only the Overseer uses it to pick a problem.

## Interface

- `infer_concept_id(*, transcript, candidates: list[ConceptRow], model=None) ->
  int` — imported by `hoot_bridge/session_init.py`.

## Data flow

The handler threads in the course-scoped candidate list (`{concept_id,
display_name}` from `app.concepts`, WU-3D §8A — no longer a hard-coded constant).
One `MAIN_MODEL` `json_object` call (temperature 0) returns a single
`concept_id`; the value is validated against the allowed id set before return.
The provider request has a 30-second timeout.

## Invariants & gotchas

- **Fail-closed to `NoMatchingConceptError`** on null / unknown id / invalid JSON,
  including the empty-candidates "course has no curriculum" path (the LLM can
  only return null there).
- Provider timeouts use that same `NoMatchingConceptError` fallback instead of
  leaving the Hoot session-start request unbounded.
- **Bool is rejected explicitly** — Python's `True == 1` would otherwise pass an
  `in {1, ...}` membership check.
- Pure LLM call: no DB access here; candidate resolution happens upstream.

## Related

`ConceptRow` comes from `apollo/conversation/curriculum/db`. `MAIN_MODEL` is
pinned in `platform/config-model-pins`.
