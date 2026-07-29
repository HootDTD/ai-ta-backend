---
doc: database/legacy-migrations
description: The FROZEN numbered migration archive (001–048), including the explicit Interaction-1 exception.
owns:
  - database/migrations/__init__.py
  - database/migrations/001_create_schema.py
  - database/migrations/002_seed_from_supabase.py
  - database/migrations/003_reindex_existing.py
  - database/migrations/README.md
  - database/migrations/legacy-manifest.sha256
  - database/migrations/
related:
  - database/supabase-migrations
  - platform/ops-db-tooling
last_verified: 2026-07-26
stub: false
---

# database/legacy-migrations — frozen numbered history

The numbered `database/migrations/001–048` chain is a **read-only, checksum-
enforced historical record after the approved 048 addition.** This doc owns the directory
as a UNIT (the migrations-doc exemption): the 3 Python entrypoints + `__init__` +
`README.md` + `legacy-manifest.sha256` explicitly, plus the sentinel token
`database/migrations/` covering the frozen `004..048 *.sql` chain (do NOT
document per-`.sql` — point readers to git history for individual DDL).

## Interface

- `001_create_schema.py`, `002_seed_from_supabase.py`, `003_reindex_existing.py`
  — the only migration-apply entrypoints this repo ever had. **DB-03 RETIRED all
  three**: they now `raise SystemExit` immediately, pointing at
  `node scripts/db/reset-local.mjs` ([platform/ops-db-tooling](../platform/ops-db-tooling.md)).
  Their bodies remain frozen as provenance — do not import or invoke.
- `legacy-manifest.sha256` — the normalized (LF) SHA-256 of every frozen file,
  enforced by CI + pre-commit.

## Data flow

Thematic inventory (one paragraph, ranges only): base schema (`001`) → teacher
features (`004–008`) → apollo KG slices (`009–023`) → learner model (`026–028`)
→ autoprovisioning (`030`) → soundness/authored-sets/clarifications (`031–033`)
→ grading artifacts + observability (`034–047`) → the explicit Interaction-1
session-grounding JSONB addition (`048`, targeting `app.learning_activities`).
The duplicate legacy `023`
(`023_apollo_auth_scoping.sql` + `023_chunks_halfvec_hnsw.sql`) is preserved as
history. This repo never had a `database.run_migrations` aggregate runner.

## Invariants & gotchas

- **Freeze contract:** CI rejects add/edit/renumber/delete/content-change under
  this directory. The coverage lint asserts the sentinel dir contains **only** the
  checksum-manifested set — a new or renamed `.sql` **fails the lint as uncovered**
  until `legacy-manifest.sha256` is updated, preserving the uncovered-file alarm
  exactly where an unreviewed migration is most dangerous.
- `048_apollo_session_grounding_bundle.sql` is the explicitly requested
  next-sequential exception. It adds nullable `grounding_bundle JSONB` to the
  current `app.learning_activities` table and is frozen with the rest after merge.
- **Remote history reconciliation and remote application are human-only.** The
  harness runs LOCAL Docker only.

## Related

- [supabase-migrations](supabase-migrations.md) — the ACTIVE forward chain that
  supersedes this one. [platform/ops-db-tooling](../platform/ops-db-tooling.md)
  (`reset-local.mjs`, the sanctioned local applier).
