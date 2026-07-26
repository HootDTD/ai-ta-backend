---
doc: platform/ops-db-tooling
description: The four Node (.mjs) DB dev/CI tooling scripts — migration-drift guard, local reset, legacy-snapshot draft builder, and schema-dump comparator
owns:
  - scripts/db/build-legacy-snapshot-draft.mjs
  - scripts/db/check-migration-drift.mjs
  - scripts/db/compare-schema-dump.mjs
  - scripts/db/reset-local.mjs
related:
  - database/legacy-migrations
  - database/supabase-migrations
  - platform/ci-workflows
last_verified: 2026-07-25
stub: false
---

# platform/ops-db-tooling — Node DB tooling

Durable Node tooling (unlike the one-off SQL sibling `ops-db-sql`). Exposed as
`package.json` scripts `db:drift` / `db:reset`.

## Interface

- **`check-migration-drift.mjs`** — CI/local guard (the `database` CI job runs
  it): verifies the frozen numbered-migration checksums against
  `database/migrations/legacy-manifest.sha256` and permits only timestamped
  `\d{14}_*.sql` files in the active `supabase/migrations/` chain. **Load-bearing.**
- **`reset-local.mjs`** — cross-platform harness requiring Supabase CLI 2.109.0;
  starts the local Docker stack and runs `supabase db reset --local`. **No remote
  mode** — agents never reset remote DBs.
- **`build-legacy-snapshot-draft.mjs`** — deterministically reconstructs the
  non-authoritative `legacy_public_snapshot` draft from migration-001 DDL +
  the frozen `004..047 *.sql` (excludes data-only Python jobs).
- **`compare-schema-dump.mjs`** — normalizes two schema-only SQL dumps (strips
  comments/session settings, canonicalizes whitespace, sorts statements) and
  reports statement-level diffs.

## Data flow

`check-migration-drift` reads both migration dirs (frozen + active) and the
manifest; `reset-local` shells out to the pinned Supabase CLI against local
Docker. These back the migration-drift/reset workflow in
`shared-architecture/supabase`.

## Invariants & gotchas

- The **migrations themselves** are owned by the dedicated `database/*-migrations`
  docs, not here — this doc owns only the tooling.
- `reset-local.mjs` prefers a repo-local `node_modules/.../supabase` binary,
  falling back to `SUPABASE_BIN` / a `supabase` on PATH.

## Env flags

`SUPABASE_BIN` (optional CLI override); `SUPABASE_TELEMETRY_DISABLED=1` is set by
`reset-local`.

## Related

`database/legacy-migrations` + `database/supabase-migrations` (the two chains the
drift check guards), `ci-workflows` (the `database` job that invokes drift +
reset).
