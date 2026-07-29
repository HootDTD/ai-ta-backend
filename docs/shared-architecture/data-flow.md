---
doc: shared/data-flow
description: Thin cross-repo router for the two end-to-end runtime data flows — the Hoot QA path and the Apollo teaching path
owns: []
related: []
last_verified: 2026-07-25
stub: false
---

# System data flow — router

Two runtime flows carry a request end to end. This router only points at the
durable leaves that own each stage; open those for interface/data-flow detail.
(Replaces the retired `DATA-FLOW.md` monolith, now parked at
`_archive/design/data-flow-legacy.md`.)

## Hoot QA path — student asks, backend answers with citations

A `/ask` question → hybrid retrieval → packed context → cited tutor answer:

1. [`rag-pipeline/_index`](../architecture/rag-pipeline/_index.md) — the canonical
   `/ask` pipeline sequence and stage router (start here).
2. [`platform/http-server`](../architecture/platform/http-server.md) — `server.py`
   composition root + the `/ask` HTTP surface (sync `def`; `search_space_id` key).
3. [`rag-pipeline/retrieve-pipeline`](../architecture/rag-pipeline/retrieve-pipeline.md)
   — `retrieve_for_question` single entry point.
4. [`rag-pipeline/hybrid-search`](../architecture/rag-pipeline/hybrid-search.md) —
   pgvector + FTS RRF rank-fusion over the visible-document set.
5. [`rag-pipeline/context-packer`](../architecture/rag-pipeline/context-packer.md) —
   token-budget packing + citation markers.
6. [`rag-pipeline/main-ai`](../architecture/rag-pipeline/main-ai.md) — the LLM brain
   that produces the answer.
7. [`rag-pipeline/citations-formatter`](../architecture/rag-pipeline/citations-formatter.md)
   — renders citation-disciplined output.

## Apollo teaching path — student teaches the confused learner, then grades

A teaching turn (parse utterance → KG → persona reply → questioning) and the
Done-time grade of record are documented end to end by a single authority:

- [`apollo/conversation/_index`](../architecture/apollo/conversation/_index.md) —
  **the Apollo teaching-turn end-to-end authority.** It routes the live path
  (routing → handlers → agent → parser → questioning → curriculum) and names the
  grade-of-record orchestrator; follow its tables into the grading, ontology,
  knowledge-graph, resolution, solver, and overseer leaves.

## Related

- [`README`](./README.md) — umbrella navigation across the whole doc tree.
