---
doc: ai-ta-backend/apollo/provisioning/authored-sets/structure-pass
description: One structured-output pass that segments a document into question/answer/other units and deterministically pairs labels
owns:
  - apollo/provisioning/authored_sets/structure_pass.py
related:
  - ai-ta-backend/apollo/provisioning/authored-sets/_index
  - ai-ta-backend/apollo/provisioning/authored-sets/orchestrator
  - ai-ta-backend/apollo/provisioning/authored-sets/paired-retrieval
  - ai-ta-backend/apollo/provisioning/metered-chat
last_verified: 2026-07-25
stub: false
---

## Interface

- `run_structure_pass(*, problem_chunks, solution_chunks=(), metered_chat,
  scrape_spend)` → `StructurePassResult` — segment problem + optional solution
  documents within a pass-local budget (called by the orchestrator).
- `StructurePassResult` (`units`, `pairs`, `tokens_spent`, `budget_exhausted`,
  `summary()`), `StructurePair`, `StructureUnit`, `BlockSpan`, `StructurePassSummary`.

## Data flow

Because retrieval chunks are tiny and may split a printed label from its block, each
document is `_assemble`d into one stable, id-ordered character stream and handed to a
single metered-cheap structured call. The model returns half-open document-global
offsets; `_map_unit` maps them back to real chunk ids and chunk-local `BlockSpan`s
(chunk ids are re-derived from the offset map, never trusted from model output).
`_align` pairs a normalized question label to an answer label.

## Invariants & gotchas

- **Pairing is deterministic:** one normalized question label must align with
  EXACTLY one normalized answer label, else the label is left unpaired rather than
  guessed. An `Answer:` block inherits its numbered question's label.
- **Budget:** separate docs run after scrape and stop once pass-local spend exceeds
  `max(scrape_spend, 30_000)`; a combined document must run before scrape (so answer
  blocks stay out of student-facing problem text) using the 30k floor. The pass also
  refuses to spend once cumulative usage crosses half of `PER_DOCUMENT_TOKEN_CEILING`
  (headroom guard). The guard is deliberately pre-flight, so a final call may overshoot.
- Calls use only the injected metered cheap tier and never log document/response
  bodies. A `ValidationError` on the response yields no units (safe degrade).
- `StructurePassSummary` is a bounded projection with no document text.

## Related

`label_match.normalize_label` (shared normalizer),
`cost_constants.PER_DOCUMENT_TOKEN_CEILING` (metered-chat). Consumed by the
orchestrator (masking + pairing) and `paired_retrieval` (structure-pair grounding).
