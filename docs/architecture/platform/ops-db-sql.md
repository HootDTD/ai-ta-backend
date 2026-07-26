---
doc: platform/ops-db-sql
description: The five one-off SQL operational scripts from the #194 Supabase schema redesign — forward copy, rollback, legacy teardown, duplicate-index drop, and unused-index review (manual DBA, never auto-applied)
owns:
  - scripts/db/drop_duplicate_indexes.sql
  - scripts/db/reconcile_copy.sql
  - scripts/db/remove_legacy_public_schema.sql
  - scripts/db/rollback_reverse_copy.sql
  - scripts/db/unused_index_review.sql
related:
  - database/supabase-migrations
  - platform/ops-db-tooling
last_verified: 2026-07-25
stub: false
---

# platform/ops-db-sql — one-off #194 redesign SQL

The record of the #194 Supabase 33→28-table `app`/`internal` schema redesign
cutover. **Mostly historical/manual — NOT part of the numbered migration chain.**

## Interface

- **`reconcile_copy.sql`** — DB-05 forward-copy reconciliation: copies/reconciles
  legacy `public` rows into the new `app`/`internal` schemas; every mismatch
  raises so a rehearsal cannot continue on partial data.
- **`rollback_reverse_copy.sql`** — DB-05 reverse delta: upserts target rows
  created/updated at/after a watermark back into legacy `public` (never deletes
  target rows).
- **`remove_legacy_public_schema.sql`** — DB-16 legacy `public` teardown: a
  one-shot, irreversible, human-applied step taken only after prod soak + sign-off.
- **`drop_duplicate_indexes.sql`** — DB-17 pre-cutover hygiene: drops 13 exact
  duplicate legacy `public` indexes captured from prod.
- **`unused_index_review.sql`** — DB-17 read-only 30-day review query over
  `pg_stat_user_indexes`; **nothing in it executes a DROP**.

## Invariants & gotchas

- **Manual DBA one-offs — never auto-applied by agents; remote apply is a
  human/CI step** (per CLAUDE.md). None of these live under `supabase/migrations/`
  precisely so `supabase db reset` / `reset-local.mjs` never runs them, and the
  CI drift check (which only inspects `database/migrations/` + `supabase/
  migrations/`) treats them as inert.
- `remove_legacy_public_schema.sql` and `unused_index_review.sql` are deliberately
  sequenced for a **later, separately-gated window** — do not fold them into the
  initial cutover.

## Related

`database/supabase-migrations` (the authoritative active schema these scripts
migrated toward), `ops-db-tooling` (the drift/reset tooling that deliberately
skips this directory).
