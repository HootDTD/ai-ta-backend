---
doc: apollo/provisioning/_index
description: Router for the apollo-provisioning sub-domain — teacher reference-content pipelines and the shared auto-provision stages.
owns:
  - apollo/provisioning/__init__.py
related:
  - apollo/provisioning/authored-sets/_index
  - apollo/provisioning/problem-generation/_index
last_verified: 2026-07-25
stub: false
---

# apollo/provisioning

How teacher reference content becomes teachable. Two live teacher entry paths — authored sets
(+ synchronous single-problem provisioning) and problem-variant generation — feed the shared
§8B pipeline: **scrape/ingest → solution → pairing → tag/mint → promote (lint + `:Canon`)**,
with `metered-chat` wrapping every LLM call.

| Leaf | Role |
|---|---|
| [scrape](scrape.md) | Stage-1 textbook scrape → Tier-1 rows |
| [scrape-sections](scrape-sections.md) | pure section reconstruction + triage feeding scrape |
| [ingest](ingest.md) | Stage-1 authored problem-set load (commits independently) |
| [solution](solution.md) | Stage-2 find-or-generate / authored construction |
| [provisioning-schema](provisioning-schema.md) | prompt↔parser json_schema + shared `ontology_block` |
| [pairing-gate](pairing-gate.md) | Stage-3 two-phase fail-closed correctness gate |
| [tag-mint](tag-mint.md) | Stage-4 concept tag + entity mint + persistence helpers |
| [promote](promote.md) | Stage-6 Tier-1→2 flip + lint + `:Canon` |
| [promotion-lint](promotion-lint.md) | the pure nine-gate safety core |
| [path-enumeration](path-enumeration.md) | alternative-strategy path enumeration (multi-path) |
| [dedup](dedup.md) | entity dedup ladder + gate-8 problem hash |
| [metered-chat](metered-chat.md) | the metered LLM client + cost/config table |
| [retrieval-adapter](retrieval-adapter.md) | course-scoped grounding for the generator/judge |
| [authored-problem](authored-problem.md) | synchronous single-problem provisioning |
| [concept-match](concept-match.md) | closed-list concept matching (reversed provisioning) |
| [concepts-api](concepts-api.md) | teacher concept-authoring HTTP router |
| [problem-leak-guard](problem-leak-guard.md) | GEN-1 answer-leak guard (used by generation) |
| [authored-sets/_index](authored-sets/_index.md) | sub-router: teacher-gated authored problem/solution sets |
| [problem-generation/_index](problem-generation/_index.md) | sub-router: default-OFF teacher variant generation |

## Cross-cutting invariants

- Scraped/ingested rows are `tier=1` EXPLICIT (NOT teachable); only `promote` flips Tier-2.
- Every LLM call routes through `MeteredChat` under the per-document token ceiling.
- Stages 2/3/4/6 are FAIL-CLOSED — prefer a false-reject to a false-approve.
- All concept persistence keys on the BIGINT `app.concepts.id`, never the slug.
- Stages flush but never commit (the orchestrator owns the txn) — except `ingest`, which commits independently by design.

## PROVISIONING-SAVEPOINT recipe (D21)

The mint+promote pair runs inside ONE savepoint, so a lint rejection rolls back every KG row
the mint flushed (no orphaned entities). To change that transaction shape, touch
[authored-sets/orchestrator](authored-sets/orchestrator.md) + [tag-mint](tag-mint.md) +
[promote](promote.md) + [dedup](dedup.md) together.

## Public surface & routers (facade `__init__.py`, D4)

`apollo/provisioning/__init__.py` flat-re-exports the pipeline (scrape/ingest, solution,
pairing, tag/mint, dedup, promote, promotion-lint, metered-chat, leak-guard,
`provision_authored_problem`, cost helpers). Out-of-scope consumers: `scripts/
dag4_granularity_eval.py`, `scripts/wave1_live_smoke.py`, tests. Three FastAPI routers mount in
`apollo/api.py`: `authored_sets/api`, `concepts_api`, `problem_generation/api`.
`APOLLO_AUTOPROVISION_ENABLED` is a HISTORICAL master flag with NO live read-site in scope
(only cited in caveat docstrings); the live teacher paths are teacher-auth-gated, not flag-gated.
