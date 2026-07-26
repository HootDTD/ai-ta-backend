---
doc: apollo/provisioning/problem-generation/api
description: Teacher-gated batch API for generated problem variants — start a run, poll status, review, and approve into tier-2
owns:
  - apollo/provisioning/problem_generation/api.py
related:
  - apollo/provisioning/problem-generation/_index
  - apollo/provisioning/problem-generation/generator
  - apollo/provisioning/authored-sets/api
  - apollo/provisioning/authored-sets/observability
  - apollo/provisioning/metered-chat
  - apollo/provisioning/scrape
  - apollo/provisioning/tag-mint
  - apollo/conversation/routing/auth-deps
  - apollo/persistence/models
last_verified: 2026-07-25
stub: false
---

## Interface

`router` — `APIRouter` mounted in `apollo/api.py`. Endpoints:

| Method + path | Handler | Purpose |
|---|---|---|
| `GET /problem-generation/concepts/{concept_id}/seeds` | `list_generation_seeds` | The concept's teachable (tier-2) problems as seeds; NOT flag-gated (read-only) |
| `POST /problem-generation/concepts/{concept_id}/variants` | `create_generation_run` | Persist a `ProvisioningRun(kind='generation')` + BackgroundTask |
| `GET /problem-generation/runs` | `list_generation_runs` | Course-scoped run list |
| `GET /problem-generation/runs/{run_id}` | `get_generation_run` | Status + ingest-run token/cost + reviewed problems |
| `POST /problem-generation/problems/{problem_id}/approve` | `approve_generated_problem` | Reuses `authored_sets.api.approve_held_row` |

## Data flow

`create_generation_run` writes the run row + commits, then `_run_generation_background`
opens an ingest run, calls `generate_problem_variants` under a `MeteredChat` (ceiling
= `generation_token_ceiling()`), wraps start/finalize/record-error observability, and
stores a `result_summary`. Approve rebuilds the held reference and delegates to the
shared authored-sets savepoint helper (`approve_held_row`).

## Invariants & gotchas

- Every write route is teacher-gated (`require_course_teacher`) + course-scoped;
  identity (`require_user`) resolves before the first `db` query (DB-08b — the seed
  handler docstring is the canonical warning).
- `create_generation_run` 403s unless `problem_generation_enabled()`; the seed GET is
  deliberately NOT gated so the picker works while generation is toggled off.
- Problem text is bounded (2000-char cap) in review payloads; a run's ingest row
  carries the token/cost aggregates.
- `_course_concept_or_404` rejects the provisional-inventory slug.

## Env flags

`APOLLO_PROBLEM_GENERATION` (create-run gate), `APOLLO_PROBLEM_GENERATION_TOKEN_CEILING`.

## Related

`generator.generate_problem_variants`, `authored_sets.api.approve_held_row` +
`ApproveBody`, the observability writers, `MeteredChat`,
`scrape.PROVISIONAL_CONCEPT_SLUG`, `tag_mint.ResolvedConcept`, persistence
`Concept`/`Problem`/`ProvisioningRun`/`IngestRun`, `database.session`.
