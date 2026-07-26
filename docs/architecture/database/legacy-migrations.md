---
doc: database/legacy-migrations
description: The FROZEN numbered legacy migration archive (001–047) — checksum-locked provenance, not the live schema.
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
last_verified: 2026-07-25
stub: false
---

# database/legacy-migrations — frozen provenance

The numbered `database/migrations/001–047` chain is a **read-only, checksum-
enforced historical record — NOT the live schema.** This doc owns the directory
as a UNIT (the migrations-doc exemption): the 3 Python entrypoints + `__init__` +
`README.md` + `legacy-manifest.sha256` explicitly, plus the sentinel token
`database/migrations/` covering the frozen `004..047 *.sql` chain (do NOT
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
→ grading artifacts + observability (`034–047`). The duplicate legacy `023`
(`023_apollo_auth_scoping.sql` + `023_chunks_halfvec_hnsw.sql`) is preserved as
history. This repo never had a `database.run_migrations` aggregate runner.

## Invariants & gotchas

- **Freeze contract:** CI rejects add/edit/renumber/delete/content-change under
  this directory. The coverage lint asserts the sentinel dir contains **only** the
  checksum-manifested set — a new or renamed `.sql` **fails the lint as uncovered**
  until `legacy-manifest.sha256` is updated, preserving the uncovered-file alarm
  exactly where an unreviewed migration is most dangerous.
- **Remote history reconciliation and remote application are human-only.** The
  harness runs LOCAL Docker only.

## Related

- [supabase-migrations](supabase-migrations.md) — the ACTIVE forward chain that
  supersedes this one. [platform/ops-db-tooling](../platform/ops-db-tooling.md)
  (`reset-local.mjs`, the sanctioned local applier).
