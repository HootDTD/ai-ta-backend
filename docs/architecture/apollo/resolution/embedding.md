---
doc: apollo/resolution/embedding
description: Embedding primitives for the resolution layer — cosine similarity and a per-candidate-set embedding cache.
owns:
  - apollo/resolution/embedding.py
related:
  - apollo/resolution/candidates
  - apollo/resolution/resolver
last_verified: 2026-07-25
stub: false
---

# Resolution — embedding

> Part of the §5 resolver, currently **unwired** (see [_index](_index.md)).

Meaning-matching primitives (cosine over text-embedding-3-large). Intentionally
neutral so resolution and clarification can both import without a cycle.

## Interface

- `default_embedder(texts)` — the batched project-wide embedding path
  (lazy-imports `indexing.document_embedder.embed_texts` so test collection never
  touches the OpenAI SDK).
- `cosine(a, b)` — cosine similarity (0.0 on a zero vector).
- `candidate_surface_texts(candidate)` — display name + aliases + exact aliases,
  order-preserving deduped.
- `candidate_set_hash(candidates)` — deterministic sha256 cache key over the
  candidate identity fields.
- `CandidateEmbeddingCache` — memoizes candidate surface embeddings per
  candidate-set hash (`vectors_for(candidates, *, embedder)`).

## Data flow

`CandidateEmbeddingCache.vectors_for` flattens each candidate's surface texts,
embeds the whole batch once, and re-groups vectors back per `canonical_key` — so
a caller pays one batched embed per turn, keyed by `candidate_set_hash`.

## Invariants & gotchas

- The cache key tracks the same invalidation surface as the reference (the
  candidate set derives from reference + misconceptions), so a reference change
  yields a new hash and a fresh embed.
- Neutral by design — no import back into `resolver`/clarification, avoiding a
  cycle.

## Related

- [resolution/candidates](candidates.md) — the `Candidate` surfaces embedded.
- [resolution/resolver](resolver.md) — the resolution layer these primitives
  serve.
