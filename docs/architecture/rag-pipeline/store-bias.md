---
doc: rag-pipeline/store-bias
description: Post-fusion additive per-material-kind score bias, applied after reranking.
owns:
  - retrieval/store_bias.py
related:
  - rag-pipeline/retrieve-pipeline
  - rag-pipeline/hybrid-search
  - platform/config-weights
  - platform/http-server
  - knowledge/teacher-weekly
last_verified: 2026-07-25
stub: false
---

# store-bias — per-store-kind score boost

**Post-fusion, per-material-kind additive bias.** This is NOT the RRF fusion
(that is `hybrid-search`); the bias-weight values live in
`platform/config-weights`; per-workspace teacher overrides enter via
`server.py::_build_retrieval_weight_overrides` (`platform/http-server`). See the
retrieval-weights disambiguation in `_index`.

## Interface

- `apply_store_biases(chunks, weight_overrides=None) -> list[dict]` — adds a
  `final_score` key (rerank/RRF score + per-kind bias) and re-sorts descending.
  The pipeline then slices to `top_k`.

## Data flow

Per chunk: `kind = chunk["material_kind"] or "other"`; `bias = get_env_weight(kind)`
(`platform/config-weights`), overridden by `weight_overrides[kind]` when present
(from `TeacherCourse.weights`); `final_score = score + bias`.

## Invariants & gotchas

- **Additive, applied AFTER reranking** — never before fusion.
- Defaults come from env weights; teacher overrides win per kind.

## Env flags

`RETRIEVAL_STORE_WEIGHT_{TEXTBOOK,SLIDES,NOTES,HOMEWORK,EXAMS,OTHER}` (via
`platform/config-weights`).

## Related

`retrieve-pipeline`, `hybrid-search`, `platform/config-weights`,
`platform/http-server`, `knowledge/teacher-weekly` (weight controls).
