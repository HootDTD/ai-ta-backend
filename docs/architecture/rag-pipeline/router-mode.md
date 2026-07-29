---
doc: rag-pipeline/router-mode
description: decide_retrieval_mode — the always-on retrieval-mode classifier (NONE/AUGMENT/FRESH).
owns:
  - ai/router/mode.py
related:
  - rag-pipeline/router-wiring
  - rag-pipeline/router-llm
  - chats/bundle-cache
last_verified: 2026-07-25
stub: false
---

# router-mode — retrieval-mode decision

Always-on (the 2026-07 flag reset removed the master switch). v1 is LLM-only —
it deliberately skips the embedding stage (`router-deferred`).

## Interface

- `async decide_retrieval_mode(*, question, has_cache, recent_turns,
  cached_titles, llm_router) -> ModeDecision` (dataclass: `mode`, `route`,
  `confidence`, `reason`, `llm_invoked`, `latency_ms`).
- Helpers `_fresh(reason)`, `_min_confidence()`; `VALID_MODES = {NONE, AUGMENT, FRESH}`.

## Data flow

No session cache → FRESH with ZERO LLM calls. Cache present → delegate to
`LLMRouter.classify` (`router-llm`) over recent turns + cached snippet titles →
NONE / AUGMENT / FRESH.

## Invariants & gotchas

- **Fail-open asymmetry**: misrouting toward FRESH costs only efficiency;
  misrouting toward NONE can cost correctness. So router errors, an empty
  question, and any NONE/AUGMENT below `ROUTER_MIN_CONFIDENCE` all downgrade to
  FRESH.
- Classifies the RAW question (never the memory-prefixed `q_effective`, which
  would dominate the evidence).

## Env flags

`ROUTER_MIN_CONFIDENCE`, `ROUTER_MODEL`.

## Related

`router-wiring` (caller), `router-llm`, `chats/bundle-cache`.
