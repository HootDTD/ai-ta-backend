---
doc: rag-pipeline/streaming
description: JsonStringFieldStreamer — incremental extraction of one top-level JSON string field for SSE.
owns:
  - ai/streaming.py
related:
  - rag-pipeline/main-ai
last_verified: 2026-07-25
stub: false
---

# streaming — incremental JSON string-field streamer

## Interface

- `class JsonStringFieldStreamer(field="steps")` with `feed(chunk) -> str`
  (decoded delta) and a `complete` flag. Imported lazily by
  `main_ai.solve_with_bundle_stream` to stream the solver's `steps` prose out of
  a JSON-mode Responses stream before the full object arrives.

## Invariants & gotchas

- Handles only a **single top-level string field** (no nested search).
- Decodes standard JSON escapes and **never emits a partial escape sequence that
  straddles a chunk boundary** (buffers the tail); split `\uXXXX` surrogate pairs
  are held until both halves arrive, and lone/doubled surrogates self-heal to the
  replacement character.
- Consumed only within the `/ask/stream` SSE path.

## Related

`main-ai` (sole consumer).
