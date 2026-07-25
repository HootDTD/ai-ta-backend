---
doc: apollo/provisioning/authored-problem
description: Synchronous provisioning for a single teacher-authored problem (the live teacher path).
owns:
  - apollo/provisioning/authored_problem.py
related:
  - apollo/provisioning/solution
  - apollo/provisioning/pairing-gate
  - apollo/provisioning/tag-mint
  - apollo/provisioning/promote
last_verified: 2026-07-25
stub: false
---

# provisioning/authored-problem

Synchronous provisioning for a SINGLE teacher-authored problem — the live teacher path,
intentionally independent of the removed background auto-provision queue/worker.

## Interface

- `provision_authored_problem(db, neo, authored, *, search_space_id, ingest_concept_id, construct_chat_fn, judge_fn, tag_chat_fn, embed_fn) -> AuthoredProvisionResult`
- `AuthoredProvisionResult` — `outcome` / `stage` / `diagnostic` / `failed_gate`.

Both are re-exported by the package facade (`provisioning/_index`).

## Data flow

Per candidate it chains the in-scope primitives: `construct_authored_reference` /
`build_authored_approved_pair` (from `solution`) → `validate_pair` (`pairing_gate`) →
`tag_and_mint` → `promote`, first resolving the existing Tier-1 row id and the
concept-scoped existing dup hashes. Grounding is the authored solution itself, so it passes a
no-op `_no_retrieve` to the pairing gate.

## Invariants & gotchas

- **Failure modes surface as a bounded rejection, never a retired audit write.** A
  `SolutionDraftError`, a pairing `Rejection`, a `TagMintError`, a `CostBudgetExceeded`, or a
  lint failure is captured into `AuthoredProvisionResult` (`outcome='rejected'` with the
  stage), so the authored-set ledger records it without aborting the batch.
- A missing Tier-1 row for the authored `problem_code` is a hard `RuntimeError` (an ingest
  invariant violation, not a per-candidate reject).

## Related

- `provisioning/solution` — authored construction + `build_authored_approved_pair`.
- `provisioning/pairing-gate` — the correctness gate.
- `provisioning/tag-mint` — tag + mint.
- `provisioning/promote` — the Tier-2 promotion.
