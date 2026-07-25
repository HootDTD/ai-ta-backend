---
doc: ai-ta-backend/rag-pipeline/router-deferred
description: The DEFERRED / not-wired legacy two-stage embedding router (dormant cohesive group).
owns:
  - ai/router/embedding_router.py
  - ai/router/orchestrator.py
  - ai/router/routes.py
  - ai/router/seeds.json
related:
  - ai-ta-backend/rag-pipeline/router-mode
last_verified: 2026-07-25
stub: false
---

# router-deferred — the dormant embedding router

Four dormant, cohesive files grouped into one doc (deliberate dormant-group
exception). **Only tests import them; the LIVE router is
`router-mode`/`router-wiring`/`router-llm`.**

## Interface

- `embedding_router.py` — `class EmbeddingRouter` + `Stage1Decision` + `Embedder`
  Protocol + `_normalize`: cosine of the query vs seed utterances (Stage 1);
  seeds are fluids-specific and thresholds are untuned.
- `orchestrator.py` — `async route(...) -> RouteDecision`, the two-stage combiner
  (`_stage1_retrieval_mode` + stage2). NOT the live `mode.decide_retrieval_mode`.
- `routes.py` — `RouteName`/`RetrievalMode` enums, `Route` dataclass, `REGISTRY`
  (6 routes), `load_seed_utterances()` reading `seeds.json` (`_SEEDS_PATH`).
- `seeds.json` — the seed-utterance registry: 5 seeded routes (`clarify` has no
  seeds — it is the fallback).

## Invariants & gotchas

- **Unwired pending telemetry-justified thresholds** — kept for a future
  embedding fast-path in front of `router-mode`. `seeds.json` is a non-`.py`
  source file in the optional lint universe.

## Related

`router-mode` (the live replacement).
