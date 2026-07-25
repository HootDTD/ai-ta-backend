---
doc: ai-ta-backend/platform/config-model-pins
description: config/models.py — the hardcoded served solver-model constants (MAIN_MODEL, MAIN_REASONING_EFFORT) introduced by the 2026-07 flag reset
owns:
  - config/models.py
related:
  - ai-ta-backend/rag-pipeline/main-ai
last_verified: 2026-07-25
stub: false
---

# platform/config-model-pins — pinned solver model

The single source of truth for the served solver model, imported at ~15 call
sites.

## Interface

- `MAIN_MODEL` — the pinned solver model constant.
- `MAIN_REASONING_EFFORT` — the pinned reasoning-effort constant.

## Data flow

Both were Railway env vars read at ~15 call sites with drifting defaults; the
flag reset **hardcodes** them so a model change is now a code change + deploy, by
design. Per-surface overrides still layer on top from the environment
(`APOLLO_MODEL`, `APOLLO_CHEAP_MODEL`, `VISION_ANSWER_MODEL`,
`APOLLO_UNIFIED_QUESTION_MODEL`, `REPORTS_MODEL`).

## Invariants & gotchas

- Do not reintroduce `MAIN_MODEL`/`MAIN_REASONING_EFFORT` as env vars — the
  hardcode is the fix for the pre-reset drift. (Per D15 this doc names the
  constants but not their current values, which are volatile.)

## Related

`rag-pipeline/main-ai` is the primary consumer of the served solver model.
