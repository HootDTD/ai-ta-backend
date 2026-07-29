-- DB-17: 30-day unused-index review query (plan section 6.3).
--
-- This is a DOCUMENTED REVIEW QUERY, not an automated drop. Nothing in this
-- file executes a DROP. It is a human-run, read-only report the operator
-- runs against PROD's `pg_stat_user_indexes` a good while after the target
-- schema (`app`/`internal`) has been carrying real traffic.
--
-- WHY NOT SOONER / WHY NOT AUTOMATED (plan section 6.3, read this before
-- acting on any row this query returns):
--   "Do not use two days of post-reseed idx_scan=0 as proof of deadness."
--   Most of the ~90 "unused" indexes the pre-cutover advisor scan found
--   disappear on their own once the legacy `public` tables are removed
--   (`scripts/db/remove_legacy_public_schema.sql`) -- the target DDL simply
--   never recreates anything outside its reviewed allowlist
--   (`supabase/migrations/20260717035041_create_app_schema_v1.sql`,
--   `tests/database/test_db17_index_allowlist.py` pins that allowlist
--   exactly). This query exists only to catch a SURVIVING candidate in the
--   NEW `app`/`internal` schema -- an allowlisted index that turns out to be
--   genuinely unused once real traffic runs -- not to relitigate the
--   allowlist design itself.
--
-- HOW TO USE (human, remote, run in this exact order):
--   1. RESET STATS after test/prod cutover activates the target schema:
--        SELECT pg_stat_reset();  -- or, narrower, per-index via
--        SELECT pg_stat_reset_single_table_counters(indexrelid) for each row
--        you intend to track, if a full-instance reset is too blunt.
--      Resetting before this point makes every row below meaningless: it
--      would still be counting pre-cutover legacy traffic or migration-time
--      noise, not real target-schema usage.
--   2. WAIT at least 30 REPRESENTATIVE days. "Representative" means the
--      workflow calendar actually exercised upload, grading, report, and
--      teacher paths at least once each -- not 30 idle calendar days. If the
--      pilot's usage cadence is bursty (e.g. assignment-driven), extend the
--      window until every one of those four workflow classes has run.
--   3. RUN the query below. It reports only `app`/`internal` indexes with
--      zero or near-zero scans since the last reset, alongside size,
--      definition, and constraint ownership.
--   4. FOR EVERY CANDIDATE ROW, before touching anything:
--        - tie it to query logs / `EXPLAIN (ANALYZE, BUFFERS)` for the
--          queries that plausibly should have used it -- a real zero-usage
--          finding is corroborated by a live plan that does NOT choose it
--          (e.g. via seq scan or a different index), not by `idx_scan` alone;
--        - capture its size (`index_size_pretty` below), full definition
--          (`index_definition` below), and constraint ownership BEFORE
--          dropping anything, so rollback is a literal `CREATE INDEX`
--          statement built from the captured `index_definition`;
--        - never drop a `is_constraint_owned = true` row here -- that index
--          backs a PRIMARY KEY or UNIQUE constraint; dropping it means
--          dropping the constraint, a schema change out of this review's
--          scope entirely.
--   5. ONLY THEN drop concurrently, one at a time, outside a transaction
--      block (same `DROP INDEX CONCURRENTLY IF EXISTS` shape as
--      `scripts/db/drop_duplicate_indexes.sql`), re-running this query
--      after each drop to confirm no regression before moving to the next
--      candidate.
--
-- This query is intentionally conservative: it surfaces candidates for a
-- human decision, in schemas + workflow context, it never decides for you.

-- NOTE on portability: this deliberately does NOT select
-- `pg_stat_user_indexes.last_idx_scan` / `last_seq_scan` -- those columns
-- only exist from Postgres 18 onward. This repo's local rehearsal image is
-- `pgvector/pgvector:pg16` and prod's exact Supabase-managed Postgres major
-- version is not pinned in this file, so stick to columns present since long
-- before PG16 (`idx_scan`, `idx_tup_read`, `idx_tup_fetch`) for portability.
SELECT
    stats.schemaname                                   AS schema_name,
    stats.relname                                       AS table_name,
    stats.indexrelname                                  AS index_name,
    stats.idx_scan                                       AS scans_since_reset,
    stats.idx_tup_read                                   AS tuples_read_since_reset,
    stats.idx_tup_fetch                                  AS tuples_fetched_since_reset,
    pg_size_pretty(pg_relation_size(stats.indexrelid))   AS index_size_pretty,
    indexes.indexdef                                     AS index_definition,
    EXISTS (
        SELECT 1
        FROM pg_constraint con
        WHERE con.conindid = stats.indexrelid
          AND con.contype IN ('p', 'u')
    )                                                    AS is_constraint_owned
FROM pg_stat_user_indexes AS stats
JOIN pg_indexes AS indexes
  ON indexes.schemaname = stats.schemaname
 AND indexes.tablename = stats.relname
 AND indexes.indexname = stats.indexrelname
WHERE stats.schemaname IN ('app', 'internal')
  AND stats.idx_scan <= 5  -- "near-zero", not a hard zero -- see step 4 above
ORDER BY stats.schemaname, stats.relname, stats.indexrelname;
