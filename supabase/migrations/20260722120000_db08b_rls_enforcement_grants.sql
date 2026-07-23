/*
 * DB-08b: turn on RLS enforcement for backend request traffic.
 *
 * DB-04/DB-05 already built the policy surface (32 policies, FORCE RLS on all
 * 18 app tables, `app_runtime` created NOLOGIN NOBYPASSRLS). This migration
 * does not add or change a single policy -- it only grants what the
 * `app_runtime` role needs to actually BIND to those policies and to reach
 * the specific internal-schema objects the ENFORCED request path touches.
 * No feature flag: once this migration and the paired database/session.py
 * runtime land, every DB session minted by `get_db_session` (the FastAPI
 * dependency used by native-async routes) that resolved a request user runs
 * as app_runtime under RLS. Every other session-creation path --
 * `get_async_session` (BackgroundTasks callbacks, calls made by
 * `ai/router/wiring.py` from the sync-endpoint bridge, scripts, campaign),
 * `run_async` (the legacy sync routes in server.py), and the standalone
 * upload worker -- is unaffected and stays on the owning connection role
 * (BYPASSRLS), exactly as today. See the module docstring in
 * database/session.py for the enforcement boundary and why it is gated on
 * session-creation path, not on whether a
 * request identity happens to still be readable in the calling context.
 *
 * Why the two role-membership grants matter:
 *   - `GRANT authenticated TO app_runtime`: every one of the 32 policies is
 *     scoped `TO authenticated`. A Postgres RLS policy TO-role clause
 *     only binds to that literal role OR a role that is a MEMBER of it --
 *     the existing direct table grants app_runtime holds (from DB-04) are
 *     necessary but not sufficient; without this membership, FORCE RLS would
 *     show app_runtime zero rows on every app table despite holding SELECT/etc.
 *   - `GRANT app_runtime TO <owner>`: the backend DB connection itself
 *     (SUPABASE_DB_URL, the Supabase `postgres` bootstrap role in every real
 *     environment) must be able to `SET LOCAL ROLE app_runtime` inside a
 *     request transaction. Granted to both the literal `postgres` role
 *     (skipped if that role does not exist, e.g. the Testcontainers-bootstrapped
 *     local test harness) and to the role actually applying this migration in
 *     whatever environment it runs (captured via CURRENT_USER into a `name`
 *     variable and granted through `EXECUTE format('GRANT app_runtime TO
 *     %I', ...)` -- NOT a literal `GRANT app_runtime TO CURRENT_USER`, which
 *     reproducibly SIGSEGVs the backend on the local Supabase Postgres
 *     17.6.1.140 image; see the inline comment at that block for the
 *     isolated repro), so the same file works unmodified against hosted
 *     Supabase and local Docker. BOTH GRANTS ARE
 *     UNCONDITIONAL -- deliberately NOT guarded on `pg_has_role(..., 'MEMBER')`.
 *     On Postgres 16+, `CREATE ROLE app_runtime` (DB-04) automatically makes
 *     its creator a member of app_runtime with ADMIN OPTION but WITHOUT
 *     INHERIT/SET rights (`pg_auth_members.set_option = false`) -- so
 *     `pg_has_role(role, 'app_runtime', 'MEMBER')` reads TRUE from that alone,
 *     even though the role still cannot `SET ROLE app_runtime` (that needs
 *     `set_option`, which `pg_has_role(role, 'app_runtime', 'USAGE')` checks).
 *     A guard written against 'MEMBER' looks idempotent but silently skips the
 *     grant that actually confers SET rights -- confirmed by reproducing
 *     "permission denied to set role app_runtime" against a real Postgres 17.6
 *     instance where `postgres` (the DB-04 migration-applying role) held exactly
 *     that admin-only, non-settable membership from having created the role.
 *     A bare `GRANT app_runtime TO <role>` (no WITH clause) always upgrades an
 *     existing admin-only membership to a settable one without touching
 *     ADMIN OPTION, and re-granting an already-settable membership is a
 *     harmless no-op -- so removing the guard is strictly safe and fixes the
 *     bug in one move.
 *
 * Internal-schema grants are scoped per-table to exactly the CRUD verbs
 * reachable from a session minted by `get_db_session` -- NOT every verb
 * the "request_access" column of the recon map lists, because several of those
 * request-reachable callsites turn out, on inspection of the actual session
 * plumbing, to run inside a `BackgroundTasks` callback (its own
 * `get_async_session()` session, never enforced) rather than the enforced
 * request-scoped session. Each grant below is justified by an actual
 * `Depends(get_db_session)` callsite, not by "a request route eventually
 * reaches this code". `internal.upload_jobs`, `internal.chat_session_snippets`,
 * and `internal.chat_routing_decisions` get nothing: the first is worker-only,
 * the latter two are reached exclusively via the sync run_async bridge in
 * server.py (ai/router/wiring.py calls get_async_session() directly), never via
 * get_db_session. `internal.content_ingest_errors` also gets nothing: every
 * write site is inside a BackgroundTasks callback and nothing ever reads it
 * back through an enforced session. See docs/architecture/domain-data.md and
 * docs/shared-architecture/security.md for the callsite-by-callsite
 * justification of each grant below.
 *
 * Sequences: all six granted internal tables with a surrogate id use
 * `GENERATED BY DEFAULT AS IDENTITY`, not a bare `bigserial`/`nextval()`
 * default -- Postgres does not require a separate sequence grant for
 * identity-column inserts (unlike serial columns, whose default expression
 * literally calls `nextval('some_seq')` and needs USAGE on that sequence by
 * name). INSERT privilege on the table alone is sufficient, so no
 * `GRANT ... ON SEQUENCE` statements appear below; this was verified against
 * the real DDL in supabase/migrations/20260717035041_create_app_schema_v1.sql
 * and exercised by tests/database/test_db08b_rls_enforcement.py.
 */

SET lock_timeout = '5s';
SET statement_timeout = '5min';

BEGIN;

DO $preconditions$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        RAISE EXCEPTION 'DB-08b precondition failed: role app_runtime is missing';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        RAISE EXCEPTION 'DB-08b precondition failed: role authenticated is missing';
    END IF;
    IF to_regclass('internal.document_chunks') IS NULL THEN
        RAISE EXCEPTION 'DB-08b precondition failed: internal.document_chunks is missing';
    END IF;
    IF (SELECT rolbypassrls FROM pg_roles WHERE rolname = 'app_runtime') THEN
        RAISE EXCEPTION 'DB-08b precondition failed: app_runtime must not BYPASSRLS';
    END IF;
END
$preconditions$;

-- -------------------------------------------------------------------------
-- Role membership: bind app_runtime to the policies, and let the owner
-- connection assume it for the duration of a request transaction.
-- -------------------------------------------------------------------------

GRANT authenticated TO app_runtime;

-- Unconditional -- see the file header for why a pg_has_role(..., 'MEMBER')
-- guard here would be a real bug (an admin-only membership from CREATE ROLE
-- reads as 'MEMBER' but cannot SET ROLE). Skips the literal `postgres` role
-- gracefully when it does not exist (local Testcontainers harness).
--
-- The applying-role grant below goes through EXECUTE format(%I, ...) instead
-- of a literal `GRANT app_runtime TO CURRENT_USER` -- NOT a style preference.
-- A bare `GRANT <role> TO CURRENT_USER` (CURRENT_USER as the literal token in
-- a GRANT-role RoleSpec position, no dynamic SQL involved) reproducibly
-- crashes the backend with SIGSEGV on the local Supabase Postgres 17.6.1.140
-- image used by this repo's Testcontainers/CLI harness (confirmed directly
-- against a bare `GRANT pg_read_all_data TO CURRENT_USER;` with no DO block
-- and no app_runtime involved at all -- this is not specific to this
-- migration's role or to PL/pgSQL). Capturing CURRENT_USER into a `name`
-- variable first (ordinary expression evaluation, not the GRANT-role parser)
-- and building the GRANT text via `format(%I, ...)` for EXECUTE sidesteps the
-- crash entirely -- verified against the same local image. `pg_has_role(
-- CURRENT_USER, ...)` and using CURRENT_USER as a RAISE EXCEPTION format
-- argument (both further below, in the postcondition block) are unaffected --
-- confirmed separately -- the crash is specific to CURRENT_USER appearing
-- directly as a GRANT-role RoleSpec.
DO $owner_membership$
DECLARE
    v_applying_role name := CURRENT_USER;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
        GRANT app_runtime TO postgres;
    END IF;

    EXECUTE format('GRANT app_runtime TO %I', v_applying_role);
END
$owner_membership$;

-- -------------------------------------------------------------------------
-- Retrieval RPCs: request path is retrieval/hybrid_search.py today (direct
-- ORM, reached only via the sync-endpoint bridge -- not enforced), but these
-- three functions are the sanctioned RPC surface for any PostgREST-style or
-- future native-async caller and are covered by the DB-08b test suite as a
-- request-path regression regardless of current callers.
-- -------------------------------------------------------------------------

GRANT EXECUTE ON FUNCTION internal.fetch_items(integer[], integer[]) TO app_runtime;
GRANT EXECUTE ON FUNCTION internal.fts_count(text, integer[]) TO app_runtime;
GRANT EXECUTE ON FUNCTION internal.hybrid_search(
    text, extensions.vector, integer[], integer, integer
) TO app_runtime;

-- -------------------------------------------------------------------------
-- Internal tables reached by a session minted through get_db_session,
-- scoped to exactly the verbs exercised on that path (see the file header
-- for why several recon-map "request_access" entries are narrower here than
-- the raw callsite list suggests -- most writes to these tables happen
-- inside a BackgroundTasks callback, in its OWN get_async_session() session, which
-- is never enforced).
-- -------------------------------------------------------------------------

-- SECURITY INVOKER default on the three RPCs above: they read ONLY this
-- table. No enforced ORM callsite reads or writes document_chunks directly
-- today (orchestrator.py / paired_retrieval.py / checkpoint_indexer.py all
-- run inside BackgroundTasks callbacks; retrieval/hybrid_search.py runs only
-- on the sync-endpoint bridge) -- this grant exists solely so the RPC
-- surface itself is usable under app_runtime.
GRANT SELECT ON internal.document_chunks TO app_runtime;

-- apollo/handlers/artifact_writer.py::write_artifacts (insert, one row per
-- Done click when APOLLO_GRADING_ARTIFACT_ENABLED); apollo/handlers/done.py
-- and apollo/projections/scorecard.py (select the row back). Chained from
-- POST /apollo/sessions/{id}/done, Depends(get_db_session). No UPDATE on
-- this path -- the row is write-once.
GRANT SELECT, INSERT ON internal.grading_runs TO app_runtime;

-- apollo/learner_model/personalization_read.py (select, Apollo session
-- request handlers) + the teardown-safety read in
-- apollo/provisioning/authored_sets/api.py::_protected_concepts (both
-- Depends(get_db_session)); apollo/provisioning/tag_mint_persist.py::
-- insert_prereqs (insert), reached ONLY via the two enforced
-- approve-held-{problem,generated}-problem routes (tag_and_mint ->
-- insert_prereqs, running inside db.begin_nested() on the request-scoped
-- session) -- the orchestrator/authored_problem.py tag_and_mint callsites
-- are BackgroundTasks-only and do not need this grant. No UPDATE/DELETE on
-- the enforced path.
GRANT SELECT, INSERT ON internal.entity_prerequisites TO app_runtime;

-- GET /authored-sets/{set_id} (authored_sets/api.py::_load_ingest_evidence)
-- and GET /problem-generation/runs/{run_id}, both Depends(get_db_session),
-- read the ingest run row back for the teacher-facing observability surface.
-- All WRITES (start_ingest_run/finalize_ingest_run) happen exclusively
-- inside BackgroundTasks callbacks (their own get_async_session()) -- no
-- INSERT/UPDATE grant on the enforced path.
GRANT SELECT ON internal.content_ingest_runs TO app_runtime;

-- apollo/provisioning/dedup.py::resolve_candidate writes ONE audit row per
-- candidate, reached (like entity_prerequisites above) via tag_and_mint
-- called from the two enforced approve-held-*-problem routes. The teardown
-- route (DELETE /authored-sets/{set_id}) explicitly deletes orphaned
-- decisions on the same enforced session before deleting their parent
-- concept (FK is ON DELETE SET NULL, so an explicit delete is required
-- first). SELECT is included even though no application code queries this
-- table back: dedup_decisions.id is GENERATED BY DEFAULT AS IDENTITY, and
-- the asyncpg dialect of SQLAlchemy defaults to implicit_returning=True, i.e.
-- the ORM INSERT for DedupDecision(...) is actually
-- ``INSERT INTO ... RETURNING id`` -- and RETURNING a column requires SELECT
-- privilege on it, not just INSERT (verified against a real INSERT ...
-- RETURNING under app_runtime in tests/database/test_db08b_rls_enforcement.py;
-- omitting this grant makes every enforced dedup_decisions insert fail with
-- "permission denied for table dedup_decisions", not a clean INSERT denial).
GRANT SELECT, INSERT, DELETE ON internal.dedup_decisions TO app_runtime;

-- GET /authored-sets/{set_id} (authored_sets/api.py::_load_ingest_evidence)
-- reads per-page OCR evidence back on the enforced session. The write side
-- (observability.py::persist_page_evidence) runs exclusively inside
-- BackgroundTasks callbacks -- no INSERT grant on the enforced path.
GRANT SELECT ON internal.ingest_page_evidence TO app_runtime;

DO $postconditions$
DECLARE
    v_postgres_can_set boolean;
BEGIN
    IF (SELECT rolbypassrls FROM pg_roles WHERE rolname = 'app_runtime') THEN
        RAISE EXCEPTION 'DB-08b postcondition failed: app_runtime must not BYPASSRLS';
    END IF;

    IF NOT pg_has_role('app_runtime', 'authenticated', 'MEMBER') THEN
        RAISE EXCEPTION 'DB-08b postcondition failed: app_runtime is not a member of authenticated';
    END IF;

    -- 'USAGE' (not 'MEMBER') is the check that actually matters here: it
    -- verifies the membership carries SET rights (pg_auth_members.set_option),
    -- not merely that a membership row exists. An admin-only membership from
    -- CREATE ROLE (Postgres 16+) reads 'MEMBER' = true but 'USAGE' = false,
    -- and SET LOCAL ROLE app_runtime would fail despite 'MEMBER' passing --
    -- see the file header.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
        SELECT pg_has_role('postgres', 'app_runtime', 'USAGE') INTO v_postgres_can_set;
        IF NOT v_postgres_can_set THEN
            RAISE EXCEPTION
                'DB-08b postcondition failed: postgres cannot SET ROLE app_runtime (missing SET option)';
        END IF;
    END IF;

    IF NOT pg_has_role(CURRENT_USER, 'app_runtime', 'USAGE') THEN
        RAISE EXCEPTION
            'DB-08b postcondition failed: migration-applying role % cannot SET ROLE app_runtime (missing SET option)',
            CURRENT_USER;
    END IF;
END
$postconditions$;

COMMIT;
