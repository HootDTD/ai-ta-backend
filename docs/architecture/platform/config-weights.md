---
doc: ai-ta-backend/platform/config-weights
description: config/weights.py — the store-kind retrieval bias weights (defaults, env parsing, clamp/normalize) consumed by the post-fusion store-bias step
owns:
  - config/weights.py
related:
  - ai-ta-backend/rag-pipeline/store-bias
  - ai-ta-backend/rag-pipeline/hybrid-search
  - ai-ta-backend/platform/http-server
  - ai-ta-backend/platform/workspaces
last_verified: 2026-07-25
stub: false
---

# platform/config-weights — store-kind bias weights

**These are the bias weights consumed by `store-bias` (a post-fusion, additive
per-store-kind nudge) — NOT the RRF rank-fusion** (that lives in
`rag-pipeline/hybrid-search` and has no per-arm weight). This is the §4.0.15
disambiguation config leg.

## Interface

- `WEIGHT_KINDS = (textbook, slides, notes, homework, exams, other)`.
- `WEIGHT_ENV_PREFIX = "RETRIEVAL_STORE_WEIGHT_"`; per-kind default map
  `WEIGHT_DEFAULTS` (textbook highest ~0.12 … other ~0.03).
- Bounds `WEIGHT_MIN = 0.0`, `WEIGHT_MAX = 1.0`.
- `get_env_weight(kind)` / `get_env_weights(kinds=None)` — read
  `RETRIEVAL_STORE_WEIGHT_<KIND>` with the default fallback.
- `clamp_weight(value, …)`, `normalize_weight(value, …)`,
  `normalize_weights(raw, *, base=None)` — ignore unknown keys, fall back to
  `base` (or env) per kind, clamp to `[0, 1]`.

## Data flow

`get_env_weights()` is the baseline; `server.py::_build_retrieval_weight_overrides`
layers workspace + per-material + teacher-saved overrides on top, and the merged
map reaches `retrieval/store_bias.apply_store_biases`.

## Invariants & gotchas

- Unknown keys are silently dropped by `normalize_weights` (only `WEIGHT_KINDS`
  survive) — a teacher payload with a typo'd kind is ignored, not rejected.
- The teacher `POST /teacher/retrieval-weights` route persists an override map
  that ultimately displaces these defaults per course.

## Env flags

`RETRIEVAL_STORE_WEIGHT_TEXTBOOK` / `_SLIDES` / `_NOTES` / `_HOMEWORK` /
`_EXAMS` / `_OTHER`.

## Related

`store-bias` (consumer), `hybrid-search` (the distinct RRF fusion),
`http-server` (`_build_retrieval_weight_overrides`), `workspaces` (per-course
`weight_overrides`).
