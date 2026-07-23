/*
 * DB-08c: close the RLS write-policy gaps DB-08b's enforcement flip exposed.
 *
 * DB-08b (20260722120000) turned request-path sessions from BYPASSRLS onto
 * the FORCE-RLS `app_runtime` role, and its own docstring is explicit that it
 * "does not add or change a single policy" -- it only grants role membership
 * and internal-schema table/RPC access. That was true of the DB-04 policy
 * set (32 policies) at the time: it was authored against a BYPASSRLS
 * backend, so several `app` tables only ever got a SELECT policy -- the
 * missing INSERT/UPDATE/DELETE policies were invisible until a real,
 * enforced end-to-end run actually attempted those writes as `app_runtime`.
 *
 * A post-DB-08b e2e run found exactly that: POST /apollo/sessions/{id}/done
 * 500s on every student's FIRST graded attempt, because
 * apollo/persistence/progress_repo.py::load_progress's default-row INSERT
 * into app.student_progress has no covering policy (asyncpg
 * InsufficientPrivilegeError, uncaught) -- and a second defect where
 * AUTO_ENROLL_STUDENT_MEMBERSHIP's INSERT into app.course_memberships is
 * silently denied for brand-new students. This migration is the fix: it
 * adds the missing WRITE policies (INSERT/UPDATE/DELETE only -- every SELECT
 * policy already in place from DB-04 is left untouched) for every enforced
 * write callsite found in a full recon of every route wired
 * `Depends(get_db_session)` (see docs/shared-architecture/security.md for
 * the callsite-by-callsite justification kept in sync with this file).
 *
 * Scope discipline: this migration adds POLICY objects only. No GRANT
 * changes -- DB-04's blanket `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL
 * TABLES IN SCHEMA app TO authenticated, app_runtime` already covers every
 * verb these policies gate; RLS policies are the only thing standing between
 * that table-level grant and an actual allowed row.
 */

SET lock_timeout = '5s';
SET statement_timeout = '5min';

BEGIN;

DO $preconditions$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        RAISE EXCEPTION 'DB-08c precondition failed: role app_runtime is missing';
    END IF;
    IF to_regprocedure('internal.has_course_role(bigint, text[])') IS NULL THEN
        RAISE EXCEPTION 'DB-08c precondition failed: internal.has_course_role is missing';
    END IF;
    IF (SELECT count(*) FROM pg_policies WHERE schemaname = 'app') <> 32 THEN
        RAISE EXCEPTION
            'DB-08c precondition failed: expected the DB-04 baseline of 32 app-schema policies, found %',
            (SELECT count(*) FROM pg_policies WHERE schemaname = 'app');
    END IF;
END
$preconditions$;

-- -------------------------------------------------------------------------
-- app.student_progress: apollo/persistence/progress_repo.py::load_progress
-- (INSERT, default row on a student's first graded attempt) and ::apply_xp
-- (UPDATE xp_total/level/last_level_up_at) -- both mainline, reached
-- unconditionally from handle_done. Only a SELECT policy existed before.
-- -------------------------------------------------------------------------
CREATE POLICY student_progress__owner_insert ON app.student_progress FOR INSERT TO authenticated
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND (SELECT internal.has_course_role(course_id, ARRAY['student', 'teacher']))
    );
CREATE POLICY student_progress__owner_update ON app.student_progress FOR UPDATE TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND (SELECT internal.has_course_role(course_id, ARRAY['student', 'teacher']))
    );

-- -------------------------------------------------------------------------
-- app.course_memberships: auth.py::auto_enroll_student_membership (INSERT,
-- role hardcoded 'student'), reached from apollo/auth_deps.py::
-- require_course_member -- itself called from every course-scoped route
-- (session_from_hoot, create_session, /progress, /concepts, /problems) and
-- transitively from require_session_owner (every /sessions/{id}/* route).
-- Only a SELECT policy existed before.
-- -------------------------------------------------------------------------
CREATE POLICY course_memberships__self_insert ON app.course_memberships FOR INSERT TO authenticated
    WITH CHECK (user_id = (SELECT auth.uid()) AND role = 'student');

-- -------------------------------------------------------------------------
-- app.mastery_events / app.learner_state: apollo/projections/mastery.py::
-- update_mastery_from_artifact, reached from handle_done::_project_mastery
-- (gated on APOLLO_GRADING_ARTIFACT_ENABLED, off by default, but the code
-- path is live and enforced). mastery_events is append-only (INSERT only --
-- no route ever updates an existing row, and its UNIQUE(attempt_id,
-- entity_id, event_kind) constraint makes a second write for the same
-- attempt/entity a no-op skip in application code, not an UPDATE).
-- learner_state is upserted in place (INSERT on first evidence, UPDATE on
-- every subsequent one) -- UPDATE only ever touches belief/mastery/
-- confidence/evidence_count/timestamps, never course_id or entity_id, so its
-- WITH CHECK does not re-verify course membership (mirrors the actual write
-- shape; USING already scopes to the caller's own rows).
-- -------------------------------------------------------------------------
CREATE POLICY mastery_events__owner_insert ON app.mastery_events FOR INSERT TO authenticated
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND (SELECT internal.has_course_role(course_id, ARRAY['student', 'teacher']))
    );

CREATE POLICY learner_state__owner_insert ON app.learner_state FOR INSERT TO authenticated
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND (SELECT internal.has_course_role(course_id, ARRAY['student', 'teacher']))
    );
CREATE POLICY learner_state__owner_update ON app.learner_state FOR UPDATE TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (user_id = (SELECT auth.uid()));

-- -------------------------------------------------------------------------
-- app.tutoring_messages: apollo/handlers/restart_problem.py::
-- handle_restart_problem -- DELETE FROM tutoring_messages WHERE attempt_id =
-- <the session's current attempt>, reached via require_session_owner. SELECT/
-- INSERT/UPDATE policies already existed; DELETE did not.
-- -------------------------------------------------------------------------
CREATE POLICY tutoring_messages__owner_delete ON app.tutoring_messages FOR DELETE TO authenticated
    USING (EXISTS (
        SELECT 1 FROM app.learning_activities AS parent
        WHERE parent.id = learning_activity_id
          AND parent.modality = 'tutoring'
          AND parent.course_id = course_id
          AND parent.user_id = (SELECT auth.uid())
    ));

-- -------------------------------------------------------------------------
-- app.concepts: apollo/provisioning/tag_mint_persist.py::
-- resolve_or_create_concept (INSERT, called from concepts_api.py::
-- create_teacher_concept and from tag_and_mint <- approve_held_row, both
-- enforced approve-held-{problem,generated}-problem routes). DELETE from
-- concepts_api.py::delete_teacher_concept and authored_sets/api.py::
-- delete_authored_set (orphaned-concept cleanup). SELECT/UPDATE already
-- existed.
-- -------------------------------------------------------------------------
CREATE POLICY concepts__teacher_insert ON app.concepts FOR INSERT TO authenticated
    WITH CHECK ((SELECT internal.has_course_role(course_id, ARRAY['teacher'])));
CREATE POLICY concepts__teacher_delete ON app.concepts FOR DELETE TO authenticated
    USING ((SELECT internal.has_course_role(course_id, ARRAY['teacher'])));

-- -------------------------------------------------------------------------
-- app.problems: authored_sets/api.py::delete_authored_set -- DELETE minted
-- and orphaned tier-1 problem rows on set teardown. SELECT/UPDATE already
-- existed.
-- -------------------------------------------------------------------------
CREATE POLICY problems__teacher_delete ON app.problems FOR DELETE TO authenticated
    USING ((SELECT internal.has_course_role(course_id, ARRAY['teacher'])));

-- -------------------------------------------------------------------------
-- app.documents: authored_sets/api.py::delete_authored_set -- DELETE the
-- set's hidden problem/solution documents on teardown. Only a SELECT/UPDATE
-- policy existed before.
-- -------------------------------------------------------------------------
CREATE POLICY documents__teacher_delete ON app.documents FOR DELETE TO authenticated
    USING ((SELECT internal.has_course_role(course_id, ARRAY['teacher'])));

-- -------------------------------------------------------------------------
-- app.learner_entities: tag_mint_persist.py::upsert_entity (INSERT, new-row
-- branch) and ::drop_unlinkable_minted_misconceptions (DELETE), both reached
-- via tag_and_mint <- approve_held_row on the two enforced approve-held-*-
-- problem routes. SELECT/UPDATE already existed.
-- -------------------------------------------------------------------------
CREATE POLICY learner_entities__teacher_insert ON app.learner_entities FOR INSERT TO authenticated
    WITH CHECK ((SELECT internal.has_course_role(course_id, ARRAY['teacher'])));
CREATE POLICY learner_entities__teacher_delete ON app.learner_entities FOR DELETE TO authenticated
    USING ((SELECT internal.has_course_role(course_id, ARRAY['teacher'])));

DO $postconditions$
DECLARE
    app_policy_count integer;
BEGIN
    SELECT count(*) INTO app_policy_count FROM pg_policies WHERE schemaname = 'app';
    IF app_policy_count <> 45 THEN
        RAISE EXCEPTION
            'DB-08c postcondition failed: expected 32 DB-04 + 13 new = 45 app-schema policies, got %',
            app_policy_count;
    END IF;
END
$postconditions$;

COMMIT;
