---
doc: apollo/provisioning/authored-sets/verification
description: Low-OCR-confidence cross-check — independently generate and LLM-compare so a bad OCR'd reference is flagged for teacher review
owns:
  - apollo/provisioning/authored_sets/verification.py
related:
  - apollo/provisioning/authored-sets/_index
  - apollo/provisioning/authored-sets/orchestrator
  - apollo/provisioning/solution
last_verified: 2026-07-25
stub: false
---

## Interface

- `verify_against_generated(db, *, candidate, draft, min_conf, problem_low_conf,
  match_method, metered_chat, conf_threshold)` → `VerificationVerdict` — called by
  the orchestrator for extracted/`llm_paired` drafts.
- `VerificationVerdict` (`review_required`, `reason`, `generated_alt`,
  `ocr_confidence`, `match_method`).
- `_empty_retrieve` — a no-grounding retrieve fn, reused by
  `problem_generation.generator`.

## Data flow

When an extracted reference's grounding OCR'd below threshold (or the problem doc is
itself low-confidence), independently `find_or_generate` a solution and compare: if
the final answers match, trust the extraction; otherwise an LLM judge decides
MATERIAL equivalence (different-but-valid procedures reaching the same answer count
as equivalent). A material divergence sets `review_required=True` and stashes the
generated alternative for the teacher. High-confidence extractions skip the whole
check for cost control.

## Invariants & gotchas

- **Fail-closed on absent confidence (M4):** a page missing from `page_debug` yields
  `min_conf=None`, which carries NO confidence signal and is deliberately treated as
  LOW confidence (so generate-and-compare still runs) — not as high confidence.
- The equivalence judge is metered-cheap; an unparseable judge response defaults to
  NOT equivalent (flag for review), the safe direction.

## Related

`solution.find_or_generate`/`ReferenceSolutionDraft` — the independent generation +
draft shape it compares against.
