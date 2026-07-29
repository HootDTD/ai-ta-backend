---
doc: apollo/provisioning/authored-sets/orchestrator
description: Drives one authored problem-set through scrape → ground → derive/pair → mint+promote, returning a bounded per-candidate report
owns:
  - apollo/provisioning/authored_sets/orchestrator.py
related:
  - apollo/provisioning/authored-sets/_index
  - apollo/provisioning/authored-sets/structure-pass
  - apollo/provisioning/authored-sets/paired-retrieval
  - apollo/provisioning/authored-sets/graph-derivation
  - apollo/provisioning/authored-sets/verification
  - apollo/provisioning/scrape
  - apollo/provisioning/solution
  - apollo/provisioning/pairing-gate
  - apollo/provisioning/tag-mint
  - apollo/provisioning/promote
  - apollo/provisioning/dedup
  - apollo/provisioning/concept-match
  - apollo/conversation/curriculum/db
last_verified: 2026-07-25
stub: false
---

## Interface

- `run_authored_set_provisioning(db, neo, *, search_space_id, problem_document_id,
  solution_document_id, metered_chat, combined_document=False, ...)` → `ProvisioningReport`
  — the single entry (called by `api._run_set_background`).
- `ProvisioningReport` (list of `ProblemResult` + `counts` + optional
  `structure_pass` summary), `ProblemResult`, `MintRejected`,
  `reversed_provisioning_enabled`.
- `_authored_concept_dup_hashes` and `_tag_mint_chat_fn` — imported by `api.py`
  (approve path reuses the same gate-8 hash set + tag prompt).

## Data flow

Resolve the provisional concept → `list_registered_concepts` decides
`reversed_mode` (reversed AND the course has registered concepts). Load solution
chunks + label index + per-page OCR confidence. Optionally run the structure pass
(combined-document probe runs BEFORE scrape). `scrape_document` → `write_tier1_problems`
(inventory persists immediately). Per candidate (`_process_authored_candidate`):

- **Reversed:** `match_concept` first (NO_MATCH → held, never force-matched);
  `derive_reference_graph` from the paired-solution spans anchored to the matched
  concept's vocabulary.
- **Legacy fallback / no paired span:** `find_or_generate` (generated drafts held
  for review).
- Extracted/`llm_paired` drafts go through `verify_against_generated` (low-OCR
  cross-check) + `validate_pair` (fail-closed gate) before promotion.
- `tag_and_mint` + `promote` run inside ONE `begin_nested()` savepoint.

## Invariants & gotchas

- **Mint + promote share one savepoint.** A lint rejection raises `MintRejected` to
  unwind the savepoint so the mint's flushed concept/LearnerEntity/prereq/dedup rows
  never survive as orphans (the verified 17→33 entity-doubling bug). `TagMintError`
  rejects just that candidate; a `CanonProjectionError` propagates as a run failure.
- **Combined-Q&A ordering (student-safety boundary).** With structure pairing on,
  the structure pass runs before scrape and the scraper receives only question-unit
  slices (`_compose_question_mask` subtracts answer ranges + an answer-line
  backstop), because tier-1 problem text is persisted immediately after scrape and
  cannot be repaired later. Uses the structure module's 30k budget floor.
- All shadow/structure work is wrapped so a failure restores the ordinary flow and
  never aborts provisioning. Per-candidate rejections are captured as `ProblemResult`,
  not raised.
- `ProvisioningReport` serializer drops `structure_pass`/`combined_document` when
  inactive to stay byte-compatible with the pre-structure model_dump.

## Env flags

`APOLLO_REVERSED_PROVISIONING` (default on), `APOLLO_STRUCTURE_PAIRING`
(off/shadow/on), `APOLLO_STRUCTURED_SCRAPE`. Values live outside this doc (D15).

## Related

Cross-cutting provisioning-savepoint invariant + stage ordering live in
`provisioning/_index`. Wires nearly every in-scope stage plus `curriculum_db`,
`paired_retrieval`, `verification`, and `label_match`.
