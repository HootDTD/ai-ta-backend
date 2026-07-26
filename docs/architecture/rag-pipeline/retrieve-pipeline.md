---
doc: rag-pipeline/retrieve-pipeline
description: retrieve_for_question — the single pgvector retrieval entry point (hybrid → rerank → bias → pack).
owns:
  - retrieval/pipeline.py
  - retrieval/__init__.py
related:
  - rag-pipeline/hybrid-search
  - rag-pipeline/reranker
  - rag-pipeline/store-bias
  - rag-pipeline/context-packer
  - platform/config-contracts
last_verified: 2026-07-25
stub: false
---

# retrieve-pipeline — the single retrieval entry

`retrieve_for_question` is THE public retrieval symbol; `retrieval/__init__.py`
re-exports it as the only name callers should import. server.py's `_ask_pgvector`
reaches it through `ai/router/wiring` (see `router-wiring`).

## Interface

- `async retrieve_for_question(query, keywords, search_space_id, db_session, weight_overrides=None, top_k=20, token_budget=6000, citation_label=None) -> (list[BundleSnippet], dict)`
  — orchestrates the four stages and returns packed snippets + a diagnostics
  dict (`hit_count_raw`, `hit_count_sem`, `chunks_in_context`, `combined_query`).

## Data flow

1. Build `combined_query = query + " " + top-6 keywords`.
2. `AITAHybridSearchRetriever(db_session, search_space_id).hybrid_search(combined_query, top_k=top_k*3)`
   (`hybrid-search`) — fetches 3× headroom for the reranker.
3. `AITARerankerService.get_instance().rerank(query, raw_chunks)` (`reranker`) —
   scores against the ORIGINAL question, not the expanded query.
4. `apply_store_biases(reranked, weight_overrides)` (`store-bias`) — additive,
   then slice to `top_k`.
5. `pack_context(top_chunks, token_budget, citation_label)` (`context-packer`).

## Invariants & gotchas

- **Keywords are appended HINTS, never standalone targets** — the original
  question always anchors semantic search, so a bad keyword extraction cannot
  kill retrieval.
- The reranker sees the raw question; hybrid_search sees the expanded query.
- Store bias is applied AFTER rerank; packing happens on the top-`k` slice.
- `BundleSnippet`/`ResearchBundle` contract types are owned by
  `platform/config-contracts` — referenced here, not owned.

## Related

`hybrid-search`, `reranker`, `store-bias`, `context-packer`; consumed via
`router-wiring`.
