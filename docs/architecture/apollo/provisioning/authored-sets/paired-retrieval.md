---
doc: apollo/provisioning/authored-sets/paired-retrieval
description: Doc-scoped grounding against ONLY the paired solution document — structure-pair, deterministic label match, then semantic top-k
owns:
  - apollo/provisioning/authored_sets/paired_retrieval.py
  - apollo/provisioning/authored_sets/label_match.py
related:
  - apollo/provisioning/authored-sets/_index
  - apollo/provisioning/authored-sets/orchestrator
  - apollo/provisioning/authored-sets/structure-pass
  - apollo/provisioning/scrape
  - apollo/provisioning/solution
  - rag-pipeline/hybrid-search
  - database/models
last_verified: 2026-07-25
stub: false
---

## Interface

- **paired_retrieval:** `make_paired_solution_retrieve_fn(db, *,
  solution_document_id, label_index, page_conf, solution_chunks=(),
  structure_pairs=None, structure_only=False, top_k=DEFAULT_PAIRED_TOP_K)` → the
  async `retrieve(question)` closure `find_or_generate` calls (it also exposes
  `last_min_conf` / `last_match_method`). Plus `load_solution_chunks`,
  `chunk_ocr_confidence`, `DEFAULT_PAIRED_TOP_K`.
- **label_match:** `SolutionChunk`, `normalize_label`, `extract_problem_label`,
  `build_solution_label_index`, `match_solution_label`.

## Data flow

`retrieve` grounds in three tiers over the paired solution doc: an unambiguous
structure-pass pair → a deterministic regex label match → doc-scoped semantic top-k
(`_halfvec_cosine_distance` over that one document's chunks). Structure/label matches
are confirmed printed solutions, so their spans are `carries_solution=True`
(`find_or_generate` takes its extract branch); the semantic fallback is an
unconfirmed guess marked `carries_solution=False` (rides along as generation context
only). `solution_document_id is None` → no spans (caller falls through to generate).

## Invariants & gotchas

- Filtering is by `internal.document_chunks.document_id` — this path NEVER uses the
  student-RAG document-visibility gate.
- **Ambiguity returns `None`, never a guess:** `match_solution_label` yields hits
  only when exactly one distinct chunk carries the label;
  `_structure_pair_index` keeps only labels with a single pair.
- `structure_only` is combined-document safety: regex + semantic whole-chunk paths
  are skipped so only answer-block structure spans enter grounding (whole persisted
  chunks would re-introduce question/answer mixed text).
- **Fixed sub-label regex bug:** a bare letter must immediately follow the digit,
  else `"Solution 1\nM=..."` was mis-keyed as `1m` and problem `1` never matched.

## Related

`scrape.chunk_content_hash` (span provenance), `solution.GroundingSpan` (span type),
`structure_pass.StructurePair`, `retrieval.hybrid_search._halfvec_cosine_distance`
(rag-pipeline/hybrid-search), `database.models.DocumentChunk`/`Document`.
