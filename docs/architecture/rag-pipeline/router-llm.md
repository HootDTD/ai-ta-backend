---
doc: rag-pipeline/router-llm
description: LLMRouter — the strict-JSON stage-2 route classifier (gpt-4o-mini).
owns:
  - ai/router/llm_router.py
related:
  - rag-pipeline/router-mode
  - rag-pipeline/router-wiring
last_verified: 2026-07-25
stub: false
---

# router-llm — the stage-2 route classifier

## Interface

- `class LLMRouter(client, model=None)` with
  `async classify(*, query, recent_turns, cached_titles) -> Stage2Decision`
  (dataclass: `route`, `retrieval_mode`, `confidence`, `reason`). Constructed and
  cached by `router-wiring._get_llm_router`.
- `_HOOT_ROUTE_SCHEMA` — the strict `json_schema` response format (route enum of 6
  specialists + `retrieval_mode` enum + confidence + reason).
- `_SYSTEM_PROMPT` — routing instructions over recent turns + cached snippet titles.

## Invariants & gotchas

- Low-confidence / error outcomes are handled UPSTREAM in `router-mode` (fail-open
  to FRESH) — this class just returns the model's structured verdict.
- `temperature=0`, strict JSON schema.

## Env flags

`ROUTER_MODEL` (default gpt-4o-mini).

## Related

`router-mode` (caller), `router-wiring`.
