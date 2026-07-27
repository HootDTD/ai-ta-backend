---
doc: database/supabase-migrations
description: The ACTIVE forward migration chain plus Supabase config/seed — the current-schema source of truth.
owns:
  - supabase/migrations/20260717032246_legacy_public_snapshot.sql
  - supabase/migrations/20260717035041_create_app_schema_v1.sql
  - supabase/migrations/20260717043000_copy_app_schema_v1.sql
  - supabase/migrations/20260717050000_retrieval_functions_v1.sql
  - supabase/migrations/20260722120000_db08b_rls_enforcement_grants.sql
  - supabase/migrations/20260723060000_db08c_rls_write_policy_gaps.sql
  - supabase/config.toml
  - supabase/seed.sql
  - supabase/.gitignore
related:
  - apollo/persistence/models
  - database/models
  - database/session
  - platform/ops-db-sql
  - platform/ops-db-tooling
last_verified: 2026-07-26
stub: false
---

# database/supabase-migrations — active chain

The timestamped `supabase/migrations/` chain is the **CURRENT forward schema**
(pinned Supabase CLI 2.109.0 per `config.toml`, applied in ascending 14-digit
order). `database/models.py` ORM MUST track it.

## Interface — the six migrations, in order

1. **`…_legacy_public_snapshot`** — the loud, non-authoritative DB-03 draft
   reconstruction of legacy `public`. MUST be replaced by a reviewed human
   `supabase db pull` from prod before any history reconciliation.
2. **`…_create_app_schema_v1`** (DB-04) — non-destructive DDL-only target build
   beside legacy `public`: 18 tenant-bearing `app` tables (forced RLS + real
   authenticated policies) + 10 service-only `internal` tables + initplan-safe
   `internal.has_course_role()` + reviewed index allowlist + typed grading-v2
   seam. Creates the `app_runtime` role (NOLOGIN, NOBYPASSRLS); moves `vector`
   into the `extensions` schema.
3. **`…_copy_app_schema_v1`** (DB-05) — non-destructive, idempotent legacy→target
   copy with planned merges + JSON→typed promotions, 7 approved no-copy tables,
   tutoring ids shifted +1,000,000 (chat ids unchanged), and unattributable rows
   quarantined for operator review.
4. **`…_retrieval_functions_v1`** (DB-06) — hardened `internal.fetch_items` /
   `fts_count` / `hybrid_search`, pinned to the `extensions` schema with an empty
   `search_path`, `EXECUTE` granted to `service_role` only.
5. **`…_db08b_rls_enforcement_grants`** — flips the backend onto the enforced
   `app_runtime` role + the two role-membership grants; pairs with
   [database/session](session.md)'s `after_begin` listener.
6. **`…_db08c_rls_write_policy_gaps`** — the missing INSERT/UPDATE/DELETE policies
   DB-04's select-only set omitted (student_progress, course_memberships,
   mastery_events, learner_state, tutoring_messages, concepts, problems,
   documents, learner_entities). POLICY objects only, no GRANT changes.

## Data flow

`node scripts/db/reset-local.mjs`
([platform/ops-db-tooling](../platform/ops-db-tooling.md)) applies this chain +
seed to **LOCAL Docker only** — never linked/remote. `config.toml` sets
`project_id = "e2e-harness"`, major version 17, seed `./seed.sql`; analytics is
disabled (a Windows/Docker-Desktop `vector` log-forwarder limitation).

## Invariants & gotchas

- **`seed.sql` is intentionally EMPTY** (DB-02).
- The legacy-public teardown is a SEPARATE human-gated post-soak script
  (`scripts/db/remove_legacy_public_schema.sql`,
  [platform/ops-db-sql](../platform/ops-db-sql.md)) — deliberately **NOT** under
  `supabase/migrations/` so `reset` never runs it.
- A new column on a tutoring/learner table normally needs a migration in THIS
  active chain. Interaction-1 is the explicit sequential-number exception at
  frozen `database/migrations/048`; see the add-a-column recipe in
  `database/_index`.

## Related

- [apollo/persistence/models](../apollo/persistence/models.md) (add-a-column
  recipe), [database/models](models.md) (core ORM must track this),
  [database/session](session.md).
