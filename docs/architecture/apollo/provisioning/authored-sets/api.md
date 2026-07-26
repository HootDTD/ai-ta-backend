---
doc: apollo/provisioning/authored-sets/api
description: Teacher-gated HTTP surface for authored problem/solution sets — upload, index-hidden, background-provision, review, approve, delete
owns:
  - apollo/provisioning/authored_sets/api.py
related:
  - apollo/provisioning/authored-sets/_index
  - apollo/provisioning/authored-sets/orchestrator
  - apollo/provisioning/authored-sets/indexing
  - apollo/provisioning/authored-sets/observability
  - apollo/provisioning/authored-problem
  - apollo/provisioning/ingest
  - apollo/provisioning/tag-mint
  - apollo/provisioning/promote
  - apollo/provisioning/solution
  - apollo/provisioning/metered-chat
  - apollo/conversation/routing/auth-deps
  - apollo/conversation/routing/errors
  - apollo/persistence/models
  - database/models
last_verified: 2026-07-25
stub: false
---

Known monolith-hub doc (PLAN R2, ~1616 lines): one file = one leaf, so this doc
runs past the 80-line target (still ≤150). A code-split (lifecycle vs
mutation routes) is the recommended follow-up.

## Interface

`router` — `APIRouter` mounted at `apollo/api.py` (the entire authored-set HTTP
surface, alongside `concepts_api` and `problem_generation`). Endpoints:

| Method + path | Handler | Purpose |
|---|---|---|
| `POST /authored-sets` | `create_authored_set` | Upload problem PDF (+ optional solution PDF), index both hidden, provision in a BackgroundTask |
| `POST /authored-sets/manual` | `create_manual_authored_set` | Typed problems (no PDF) → `ingest_authored_problems` → per-problem `provision_authored_problem` |
| `GET /authored-sets` | `list_authored_sets` | Course-scoped set list |
| `GET /authored-sets/{set_id}` | `get_authored_set` | Status + result summary + enriched reviews + ingest/OCR evidence |
| `PATCH /authored-sets/problems/{concept_problem_id}` | `edit_authored_problem` | Edit held problem text / reference-solution content |
| `DELETE /authored-sets/{set_id}` | `delete_authored_set` | Teardown (problems, hidden docs, orphaned concepts + `:Canon`) |
| `POST /authored-sets/{set_id}/problems/{problem_id}/approve` | `approve_held_problem` | Teacher promotes a held reference |

`approve_held_row` + `ApproveBody` — the shared mint+promote-in-a-savepoint
helper, also imported by `problem_generation/api.py`.

## Data flow

`create_authored_set` persists a `ProvisioningRun(kind='authored_set')` with real
`app.documents` FKs, then hands a BackgroundTask (`_run_set_background`) a fresh
session: index problem (+solution) via `index_authored_doc` → open an ingest run +
page evidence → `run_authored_set_provisioning` → `finalize_ingest_run` → store the
bounded `ProvisioningReport`. Approve rebuilds an `ApprovedPair` from the held
draft and runs `tag_and_mint` + `promote` inside one `begin_nested()` savepoint.

## Invariants & gotchas

- Every route is teacher-gated (`require_course_teacher`) + course-scoped; identity
  (`require_user`) resolves before the first `db` query (DB-08b).
- `approve_held_problem` enforces cross-tenant safety: `_problem_belongs_to_set`
  requires the id to appear in the set's own `result_summary` AND share its
  `course_id`. Historical IDOR here (dual-stream review 2026-07-01) — treat any
  change as verify-auth-scope.
- `get_neo4j_client()` may be `None` (degraded); every KG-touching path routes
  through `_require_neo` → `KGUnavailableError` (503), never a silent no-op.
- Same-doc guard: a solution PDF whose `content_hash` matches the problem PDF is
  treated as no solution (generate + hold) unless `APOLLO_STRUCTURE_PAIRING=on`,
  which flips to combined-document mode (`solution_document_id = problem_document_id`).
- `DELETE` is 409 while the set is in-flight (`pending`/`indexing`/`provisioning`)
  because per-candidate `:Canon` nodes are written outside the PG txn; terminal-only.
  KG teardown is strictly scoped to fully-orphaned concepts (`_protected_concepts`
  spares any PG footprint; `_concepts_with_canon_history` spares grading history);
  `:Canon` DETACH DELETE runs AFTER the PG commit (no shared txn).
- Problem/solution text is bounded (`_LIST_OCR_TEXT_CAP`, `?full_ocr`/`?full_text`
  opt-outs) and redacted in review payloads; edit collisions are dup-hash-guarded.

## Env flags

`APOLLO_STRUCTURE_PAIRING` (off/shadow/on) gates the same-doc combined-mode branch.

## Related

Wires `provision_authored_problem`, `run_authored_set_provisioning`,
`ingest_authored_problems`, `promote`, `tag_and_mint`, `build_approved_pair`,
`enumerate_strategy_paths`, the observability writers, and `MeteredChat`. Reference
ORM + `database.models.Document`; `indexing.embed_text` for doc-embedding.
