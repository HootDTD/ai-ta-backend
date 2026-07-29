---
doc: apollo/provisioning/retrieval-adapter
description: Course-scoped retrieval-grounding adapter that feeds find-or-generate and the pairing gate.
owns:
  - apollo/provisioning/retrieval_adapter.py
related:
  - apollo/provisioning/solution
  - apollo/provisioning/scrape
  - rag-pipeline/hybrid-search
last_verified: 2026-07-25
stub: false
---

# provisioning/retrieval-adapter

The course-scoped retrieval-grounding adapter for auto-provisioning: it binds the existing
hybrid search to a course and returns the `retrieve(question)` closure `find_or_generate` /
`validate_pair` depend on.

## Interface

- `make_course_retrieve_fn(db, *, search_space_id, top_k=DEFAULT_GROUNDING_TOP_K) -> retrieve` — build the closure.
- `DEFAULT_GROUNDING_TOP_K` — the small grounding-span bound (6).

## Data flow

`make_course_retrieve_fn` returns an async `retrieve(question)` that runs
`AITAHybridSearchRetriever(db, search_space_id).hybrid_search(query_text, top_k=…)` (pgvector
+ FTS over `internal.document_chunks`) and maps each chunk dict into an immutable
`GroundingSpan` (reusing `scrape.chunk_content_hash`). v1 always RAG-generates, so every span
is `carries_solution=False`.

## Invariants & gotchas

- **Course scoping is enforced INSIDE `AITAHybridSearchRetriever`** (`active_document_conditions`)
  — grounding never crosses courses.
- **Empty retrieval is NOT an error** — the faithfulness judge rejects honestly; a row with no
  usable text is a per-span no-op (skipped, not a `KeyError`). A real DB/embedding failure
  propagates to the orchestrator's terminal handler, never masked as empty grounding.
- Printed-solution detection (`carries_solution=True`) is a documented follow-up; reranking of
  the RRF-ranked spans is also deferred.

## Related

- `provisioning/solution` — the `GroundingSpan` consumer (`find_or_generate` / `build_approved_pair`).
- `provisioning/scrape` — reuses `chunk_content_hash`.
- `rag-pipeline/hybrid-search` — `AITAHybridSearchRetriever` (owned there, referenced here).
