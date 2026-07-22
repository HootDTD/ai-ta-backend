-- DB-17: PRE-CUTOVER prod-hygiene -- drop the 13 exact duplicate legacy
-- `public` schema indexes captured read-only from PROD via MCP (plan section
-- 6.1; source list `.planning/cleanup/inputs/db-preflight.md`).
--
-- WHY THIS IS NOT UNDER supabase/migrations/: `supabase db reset` /
-- `node scripts/db/reset-local.mjs` applies every file under
-- supabase/migrations/ unconditionally, and this cleanup is independent of
-- that forward chain -- it targets the LEGACY `public` schema, which the
-- forward chain never touches (create_app_schema_v1 builds `app`/`internal`
-- beside it; nothing under supabase/migrations/ mutates a legacy table).
-- Living under scripts/db/ mirrors the two other human-run, non-auto-applied
-- scripts already in this directory (reconcile_copy.sql,
-- remove_legacy_public_schema.sql) -- neither check-migration-drift.mjs nor
-- reset-local.mjs reads this directory, so this file is inert to both.
--
-- WHAT THIS DROPS: each pair below is a hand-written/constraint-owned
-- canonical index (`idx_*`, or the implicit index backing a UNIQUE
-- constraint) duplicated by a plain `ix_*` index. Per the DB-01 preflight
-- capture, every `ix_*` member is SQLAlchemy `index=True` residue -- created
-- directly against prod by the old ORM/Python-migration-runner path outside
-- the numbered SQL migration chain (the reconstructed
-- `legacy_public_snapshot.sql` never creates them, since it is built only
-- from migrations 001-047; see that file's own top-of-file caveat and
-- `tests/database/test_drop_duplicate_indexes.py`, which seeds the `ix_*`
-- duplicates by hand before rehearsing this script for exactly that reason).
-- The target schema (`create_app_schema_v1.sql`) never recreates the `ix_*`
-- family, so this drop only removes indexes that have zero future home.
--
-- APPLY (human, remote, any time before cutover -- no ordering dependency on
-- DB-16's teardown or on activation):
--   psql "$SUPABASE_DB_URL" -f scripts/db/drop_duplicate_indexes.sql
-- HARMLESS IF SKIPPED: the 13 dropped indexes live only on legacy `public`
-- tables, which `scripts/db/remove_legacy_public_schema.sql` drops in their
-- entirety at cutover-cleanup time (human-gated, 14-day-soak-delayed per plan
-- section 8.3). Running this script first is optional prod hygiene (smaller
-- write-path lock/maintenance overhead, cleaner `pg_stat_user_indexes`/
-- advisor output pre-cutover), never a hard prerequisite for anything else in
-- this repo.
--
-- CONCURRENTLY, no wrapping transaction: `DROP INDEX CONCURRENTLY` cannot run
-- inside a transaction block (BEGIN/COMMIT), so unlike
-- `remove_legacy_public_schema.sql` this file is deliberately NOT wrapped in
-- one. `psql -f` runs each top-level statement in its own autocommit
-- transaction by default, which is exactly what CONCURRENTLY requires; do not
-- add BEGIN/COMMIT around these statements. Each is independently
-- `IF EXISTS`-guarded (plan section 6.1: "IF EXISTS guards acceptable"), so a
-- partial prior run, or a target table already gone, makes the remaining/
-- re-run statements no-ops rather than errors -- this is what
-- `tests/database/test_drop_duplicate_indexes.py` proves by running the
-- whole file twice.
--
-- REHEARSED LOCALLY by tests/database/test_drop_duplicate_indexes.py
-- (Testcontainers Postgres): applies the legacy snapshot, adds the 13 `ix_*`
-- duplicates by hand (since the snapshot itself never creates them -- see
-- above), runs this script once (all 13 drop, every surviving twin remains
-- valid), then runs it again (fully idempotent no-op, zero errors).

SET lock_timeout = '5s';
SET statement_timeout = '5min';

-- aita_chunks: ix_* duplicates the hand-written idx_* on the same column.
DROP INDEX CONCURRENTLY IF EXISTS public.ix_aita_chunks_document_id;
-- surviving twin: public.idx_aita_chunks_document (document_id)

DROP INDEX CONCURRENTLY IF EXISTS public.ix_aita_chunks_page_number;
-- surviving twin: public.idx_aita_chunks_page (page_number)

-- aita_documents: two ix_*/idx_* pairs plus two ix_* duplicates of the
-- implicit unique-constraint index.
DROP INDEX CONCURRENTLY IF EXISTS public.ix_aita_documents_material_kind;
-- surviving twin: public.idx_aita_documents_material_kind (material_kind)

DROP INDEX CONCURRENTLY IF EXISTS public.ix_aita_documents_search_space_id;
-- surviving twin: public.idx_aita_documents_search_space (search_space_id)

DROP INDEX CONCURRENTLY IF EXISTS public.ix_aita_documents_content_hash;
-- surviving twin: constraint public.aita_documents_content_hash_key (content_hash UNIQUE)

DROP INDEX CONCURRENTLY IF EXISTS public.ix_aita_documents_unique_identifier_hash;
-- surviving twin: constraint public.aita_documents_unique_identifier_hash_key (unique_identifier_hash UNIQUE)

-- aita_search_spaces: one ix_*/idx_* pair plus one ix_* duplicate of the
-- implicit unique-constraint index.
DROP INDEX CONCURRENTLY IF EXISTS public.ix_aita_search_spaces_name;
-- surviving twin: public.idx_aita_search_spaces_name (name)

DROP INDEX CONCURRENTLY IF EXISTS public.ix_aita_search_spaces_slug;
-- surviving twin: constraint public.aita_search_spaces_slug_key (slug UNIQUE)

-- chat_sessions: ix_* duplicates the implicit unique-constraint index.
DROP INDEX CONCURRENTLY IF EXISTS public.ix_chat_sessions_chat_id;
-- surviving twin: constraint public.chat_sessions_chat_id_key (chat_id UNIQUE)

-- course_invite_links: one ix_*/idx_* pair plus one ix_* duplicate of the
-- implicit unique-constraint index.
DROP INDEX CONCURRENTLY IF EXISTS public.ix_course_invite_links_search_space_id;
-- surviving twin: public.idx_invite_links_space (search_space_id)

DROP INDEX CONCURRENTLY IF EXISTS public.ix_course_invite_links_code;
-- surviving twin: constraint public.course_invite_links_code_key (code UNIQUE)

-- teacher_courses: ix_* duplicates the implicit unique-constraint index.
DROP INDEX CONCURRENTLY IF EXISTS public.ix_teacher_courses_search_space_id;
-- surviving twin: constraint public.teacher_courses_search_space_id_key (search_space_id UNIQUE)

-- teacher_upload_jobs: ix_* duplicates the hand-written idx_* on the same column.
DROP INDEX CONCURRENTLY IF EXISTS public.ix_teacher_upload_jobs_upload_id;
-- surviving twin: public.idx_teacher_upload_jobs_upload_id (upload_id)
