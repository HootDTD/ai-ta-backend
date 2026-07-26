---
doc: apollo/provisioning/ingest
description: Subject-fluid Stage-1 replacement — load a professor-authored problem set into Tier-1 inventory.
owns:
  - apollo/provisioning/ingest.py
related:
  - apollo/provisioning/authored-problem
  - apollo/provisioning/authored-sets/_index
  - apollo/persistence/models
  - apollo/schemas/problem
last_verified: 2026-07-25
stub: false
---

# provisioning/ingest

The subject-fluid Stage-1 REPLACEMENT for the textbook scrape: load a professor-authored
problem set (one structured record per problem) into Tier-1 inventory. Each record becomes an
`AuthoredProblem` classified by completeness, then written as a `tier=1` row carrying
`solution_source='authored'`.

## Interface

- `ingest_authored_problems(db, records, *, subject_id, concept_id, search_space_id, default_concept_slug=…, commit=True) -> IngestResult` — the Stage-1 entry.
- `load_authored_problems(records, *, default_concept_slug) -> (list[AuthoredProblem], n_dropped)` — parse only.
- `write_authored_tier1_problems(db, problems, *, concept_id, search_space_id) -> int` — persist, returns rows inserted.
- `classify_completeness(solution, worked_procedure) -> Completeness`, `authored_problem_code(statement) -> str`.
- `AuthoredProblem`, `Completeness` (`worked`/`answer_only`/`none`), `IngestResult`.

`AuthoredProblem`, `IngestResult`, `ingest_authored_problems`, `load_authored_problems`, and
`write_authored_tier1_problems` are re-exported by the package facade (`provisioning/_index`).

## Data flow

`load_authored_problems` coerces each record into an `AuthoredProblem` (a pure heuristic
classifies worked / answer_only / none — no LLM here), `write_authored_tier1_problems` writes
each as a `tier=1` EXPLICIT row via `Problem.from_inventory_payload` under a SELECT-then-skip
`(concept_id, problem_code)` guard, and `ingest_authored_problems` then COMMITS independently
(`commit=True`). `authored_problem_code = authored.<statement_hash>` mirrors the scrape key.

## Invariants & gotchas

- **The independent commit is the load-bearing fix.** The legacy orchestrator committed once
  at run end, so an interrupted run lost every ingested problem; here the durable inventory
  persists the instant ingest finishes, regardless of what a later stage does. Pass
  `commit=False` only to compose inside a caller-owned transaction (e.g. a pre-commit test).
- **Content-hash idempotency** — a re-ingest of the same statement inserts ZERO rows.
- **Fail-soft per record** — a record with no statement (or one failing validation) is dropped
  and counted, never a half-row or run abort.
- `tier=1` EXPLICIT (the safety trap — the ORM default is 2/teachable); `promote` keeps the
  `authored` source it stamps.

## Related

- `provisioning/authored-problem` — the per-problem synchronous provisioning of ingested rows.
- `provisioning/authored-sets/_index` — the manual-set path that consumes ingest.
- `apollo/persistence/models` — `Problem` ORM.
- `apollo/schemas/problem` — `Difficulty` and the `Problem` payload shape.
