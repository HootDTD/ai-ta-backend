---
doc: apollo/provisioning/solution
description: Stage-2 find-or-generate a reference solution, plus the subject-fluid authored-construction path.
owns:
  - apollo/provisioning/solution.py
related:
  - apollo/provisioning/provisioning-schema
  - apollo/provisioning/pairing-gate
  - apollo/provisioning/tag-mint
  - apollo/schemas/problem
last_verified: 2026-07-25
stub: false
---

# provisioning/solution

Stage-2: for a scraped `CandidateQuestion`, retrieve-first-then-RAG-generate a reference
solution and return a `ReferenceSolutionDraft`; plus the subject-fluid path that STRUCTURES
a professor's authored solution into a teachable reference graph.

## Interface

- `find_or_generate(db, question, *, retrieve_fn, chat_fn, augment_recall=False) -> ReferenceSolutionDraft`
  — the Stage-2 entry (extracted vs generated branch).
- `construct_authored_reference(authored, *, chat_fn) -> ReferenceSolutionDraft` — build a draft from an `AuthoredProblem`.
- `build_approved_pair(question, draft, *, search_space_id) -> ApprovedPair` — assemble the real `tag_mint.ApprovedPair`.
- `build_authored_approved_pair(authored, draft, *, search_space_id) -> ApprovedPair` — the authored-id variant.
- `solution_hash(draft) -> str` — canonical-JSON sha256 of `reference_solution` (idempotency component).
- `GroundingSpan` — frozen retrieved-passage value object (`carries_solution` marks a printed solution).
- `ReferenceSolutionDraft` — the Stage-2 output (`solution_source`, `reference_solution`, `grounding`, augmented fields).
- `SolutionDraftError` — the fail-closed exception.

All of these except `construct_authored_reference` framing are re-exported by the package
facade (`GroundingSpan`, `ReferenceSolutionDraft`, `SolutionDraftError`, `find_or_generate`,
`solution_hash`, `build_approved_pair`, `construct_authored_reference`, `build_authored_approved_pair`).

## Data flow

Two branches: **extracted** — a retrieved span flagged `carries_solution=True` is parsed
into steps (`solution_source='extracted'`); **generated** — RAG-generate from the question +
retrieved spans, KEEPING those same spans as `grounding` so the Stage-3 Phase-B faithfulness
judge has real context (`solution_source='generated'`). Recall/definition prompts get the
explain-why augmentation. The shared per-step contract comes from `generation_contract.
ontology_block()`; the schema from `build_solution_schema`. `build_approved_pair` imports the
real `ApprovedPair` (never redefines it) and hands it to `tag_and_mint`.

## Invariants & gotchas

- **FAIL-CLOSED.** An empty/malformed generate with no usable extracted solution raises
  `SolutionDraftError` — NEVER an empty-step draft (`Problem` requires `reference_solution`
  min_length 1). The generated `reference_solution` is `Problem`-validated before a draft returns.
- **§1.8 / OPS-6 caveat.** A coherent-but-WRONG solution can pass Stage-3 + every promotion
  gate and still be shown in shadow (no Layer-3 belief movement; `APOLLO_GRAPH_SIM_LAYER3_ENABLED`
  OFF). This unit REDUCES but does not eliminate that; the pre-exposure safety is the
  `APOLLO_AUTOPROVISION_ENABLED` flag-OFF posture (a historical master flag — see
  `provisioning/_index`) + the calibration gate, with quarantine the retroactive catch.
- **Authored construction** builds over the universal 6-entry-type mint-map vocab with no
  forced symbolic target (a prose argument carries no equations), grounds the faithfulness
  judge on the PROFESSOR's solution (not RAG'd chunks), and fails closed on a foreign
  entry_type or a non-`Problem`-valid graph.
- No DB write; `retrieve_fn`/`chat_fn` injected; inputs are course material only (no PII).

## Env flags

- `APOLLO_AUTOPROVISION_ENABLED` — historical master OFF flag (no live read-site here; see `provisioning/_index`).

## Related

- `provisioning/provisioning-schema` — `build_solution_schema` + `ontology_block()`.
- `provisioning/pairing-gate` — Stage-3 consumes the draft + grounding.
- `provisioning/tag-mint` — owns `ApprovedPair`, assembled here.
- `apollo/schemas/problem` — the `Problem` schema the draft is validated against.
