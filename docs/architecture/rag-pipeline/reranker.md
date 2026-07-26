---
doc: rag-pipeline/reranker
description: Optional cross-encoder rerank; no-op/fail-open unless RERANKERS_ENABLED.
owns:
  - retrieval/reranker.py
related:
  - rag-pipeline/retrieve-pipeline
  - rag-pipeline/hybrid-search
  - platform/config-settings
last_verified: 2026-07-25
stub: false
---

# reranker — optional cross-encoder rerank

## Interface

- `class AITARerankerService` with `classmethod get_instance()` (lazy global
  model load) and `rerank(query, chunks) -> list[dict]`.
- Module `_get_reranker()` lazily loads the model via the `rerankers` library.

## Data flow

Called by `retrieve-pipeline` between `hybrid_search` and store bias. Wraps each
chunk as a `rerankers.Document` (carrying `original_index` + `rrf_score`), ranks
against `query`, and rewrites each surviving chunk's `score` with the
cross-encoder score.

## Invariants & gotchas

- **Fail-open**: returns the input chunks unchanged when the reranker is disabled,
  unconfigured, empty, or on ANY error — the pipeline degrades to RRF order.
- **Uses the ORIGINAL question**, not the keyword-expanded query (contrast
  `hybrid-search`).

## Env flags

`RERANKERS_ENABLED` (gate), `RERANKER_MODEL` — read via
`config.settings.rerankers_enabled()` / `get_reranker_model()`.

## Related

`retrieve-pipeline`, `hybrid-search`, `platform/config-settings`.
