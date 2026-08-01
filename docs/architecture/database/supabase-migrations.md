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
  - supabase/migrations/20260728120000_apollo_grounding_bundle.sql
  - supabase/migrations/20260730120000_auth_users_identity_grant.sql
  - supabase/migrations/20260731120000_db08d_rls_inline_membership_policies.sql
  - supabase/migrations/20260731130000_pr4_hybrid_search_stored_halfvec.sql
  - supabase/config.toml
  - supabase/seed.sql
  - supabase/.gitignore
related:
  - apollo/persistence/models
  - database/models
  - database/session
  - platform/ops-db-sql
  - platform/ops-db-tooling
  - rag-pipeline/hybrid-search
last_verified: 2026-07-31
stub: false
---

# database/supabase-migrations — active chain

The timestamped `supabase/migrations/` chain is the **CURRENT forward schema**
(pinned Supabase CLI 2.109.0 per `config.toml`, applied in ascending 14-digit
order). `database/models.py` ORM MUST track it.

## Interface — the ten migrations, in order

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
7. **`…_apollo_grounding_bundle`** (INTERACTION1) — nullable
   `app.learning_activities.grounding_bundle` JSONB, the per-session
   grounding-bundle cache. Twin of legacy `database/migrations/048`; both
   chains must carry it.
8. **`…_auth_users_identity_grant`** (Class-Performance-v2) — GRANTs
   `app_runtime` USAGE on schema `auth` + column-level SELECT on
   `auth.users (id, email, raw_user_meta_data)` so the teacher
   class-performance projection resolves student email / display name
   (`encrypted_password` etc. stay unreadable). Guarded to no-op with a NOTICE
   where the auth schema/table or the role is absent (local Docker). Twin of
   legacy `database/migrations/049`; both chains must carry it.
9. **`…_db08d_rls_inline_membership_policies`** (pilot-perf PR-3) — rewrites 38
   of the 45 `app` policies IN PLACE so the membership test is an
   **uncorrelated** subquery over `app.course_memberships` instead of a
   `(SELECT internal.has_course_role(<col>, …))` call. POLICY objects only —
   no GRANT, no DDL, no helper change; count stays 45 (same names, commands,
   `TO authenticated`). See "RLS policy shape" below.
10. **`…_pr4_hybrid_search_stored_halfvec`** (pilot-perf PR-4) — adds
    `internal.document_chunks.embedding_halfvec`, a STORED generated column
    (`(embedding)::halfvec(3072)`, `database/models.py`'s
    `DocumentChunk.embedding_halfvec`), plus its own HNSW index
    (`document_chunks__embedding_halfvec_stored_hnsw__idx`) — computed once at
    write time so [rag-pipeline/hybrid-search](../rag-pipeline/hybrid-search.md)'s
    semantic arm never casts `embedding` per row at query time. DROPS the DB-04
    expression index (`document_chunks__embedding_halfvec_hnsw__idx`), confirmed
    unused in real traffic; the only other referrers
    (`internal.hybrid_search()`/`fetch_items()`, migration 4 above) are dead
    legacy RPCs, deliberately not repointed — see the migration's own header.

## Data flow

`node scripts/db/reset-local.mjs`
([platform/ops-db-tooling](../platform/ops-db-tooling.md)) applies this chain +
seed to **LOCAL Docker only** — never linked/remote. `config.toml` sets
`project_id = "e2e-harness"`, major version 17, seed `./seed.sql`; analytics is
disabled (a Windows/Docker-Desktop `vector` log-forwarder limitation).

## RLS policy shape (post-DB-08d)

Every membership check reads `<course col> IN (SELECT cm.course_id FROM
app.course_memberships AS cm WHERE cm.user_id = (SELECT auth.uid()) AND
cm.role = <'teacher' | ANY (ARRAY['student','teacher'])>)`. Full derivation in
the DB-08d file header; the rules that must survive edits:

- `(SELECT auth.uid())` is the ONLY caller expression (the `request.jwt.claims`
  → `sub` value [database/session](session.md) binds), and the subquery must
  stay **uncorrelated** — correlate it and the per-row SubPlan is back.
- Equivalence rests on every `course_id` being `NOT NULL` (so `IN` is
  two-valued) and on `app.course_memberships`' own SELECT policy admitting, by
  construction, every row the subquery selects.
- `course_memberships__self_or_teacher_select` still calls the helper and MUST:
  inlining a policy ON `app.course_memberships` recurses on its own table.
- `app.learning_activities` deliberately keeps TWO permissive SELECT policies
  (the Supabase `multiple_permissive_policies` WARN) — merging means demoting a
  `FOR ALL` policy into three per-action ones for no remaining perf gain.
- Coverage: `test_db08b_rls_enforcement.py` (the unchanged 45-policy matrix,
  re-run against the new policies) +
  `test_db08d_rls_inline_membership_policies.py`.

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
  [database/session](session.md),
  [rag-pipeline/hybrid-search](../rag-pipeline/hybrid-search.md) (consumer of
  migration 10's `embedding_halfvec` column/index).
