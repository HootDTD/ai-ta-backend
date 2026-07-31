/*
 * DB-08d: rewrite the app-schema RLS policies so the membership check inlines
 * into the query plan instead of calling internal.has_course_role() per row.
 *
 * WHY
 * ---
 * DB-08b (20260722120000) flipped request-path traffic onto the FORCE-RLS
 * `app_runtime` role. Every DB-04/DB-08c policy expresses its membership
 * check as `(SELECT internal.has_course_role(<course col>, ARRAY[...]))`.
 * has_course_role is a SECURITY DEFINER SQL function, and Postgres will not
 * inline a SECURITY DEFINER function -- it stays an opaque function call.
 * Because the first argument is a column of the row being tested, the
 * wrapping scalar sub-SELECT is CORRELATED, so the planner emits a per-row
 * SubPlan: one function call, and therefore one index probe into
 * app.course_memberships, for every candidate row of every scan on all 18
 * RLS-guarded app tables. On the pilot corpus that is the single largest
 * database-side cost on the request path.
 *
 * WHAT CHANGES
 * ------------
 * Each `(SELECT internal.has_course_role(X, ARRAY[...]))` is replaced by the
 * UNCORRELATED membership subquery
 *
 *     X IN (SELECT cm.course_id
 *             FROM app.course_memberships AS cm
 *            WHERE cm.user_id = (SELECT auth.uid())
 *              AND cm.role = <same role predicate>)
 *
 * Nothing inside the subquery references the outer row, so the planner
 * evaluates it ONCE per query (hashed SubPlan / semijoin) and probes it with
 * a hash lookup per row. `cm.user_id = (SELECT auth.uid())` is itself an
 * uncorrelated InitPlan, and the (user_id, course_id) primary key index makes
 * the one evaluation an index scan over just the caller's own memberships.
 * `course_memberships__course_role_user__idx (course_id, role, user_id)` from
 * DB-04 remains available for the plans that prefer it.
 *
 * WHY THIS IS SEMANTICS-PRESERVING
 * --------------------------------
 * 1. Same predicate body. has_course_role's body is
 *    `EXISTS (SELECT 1 FROM app.course_memberships WHERE course_id = $1
 *     AND user_id = (SELECT auth.uid()) AND role = ANY ($2))`. The
 *    replacement is that same restriction re-associated as an IN-list; for a
 *    fixed X the two are the same set membership test.
 * 2. Same current-user expression. `(SELECT auth.uid())` is reused verbatim --
 *    the same `request.jwt.claims` -> `sub` resolution that
 *    database/session.py binds with `set_config(..., true)` after
 *    `SET LOCAL ROLE app_runtime`. No new claim, GUC, or session variable.
 * 3. No three-valued-logic drift. `X IN (subquery)` can only return NULL if X
 *    is NULL or the subquery yields a NULL, whereas EXISTS always returns
 *    true/false. Neither can happen here: `app.course_memberships.course_id`
 *    is NOT NULL (it is half the primary key), and every `course_id` column
 *    referenced by a rewritten policy -- and `app.courses.id` -- is NOT NULL.
 *    So the new expression is strictly two-valued and matches the old boolean
 *    everywhere. (Even in the impossible NULL case the observable behaviour
 *    would be unchanged: RLS treats NULL exactly like false in both USING --
 *    row filtered -- and WITH CHECK -- row rejected.)
 * 4. RLS on the referenced table does not narrow the result. Unlike the
 *    SECURITY DEFINER function, an inline subquery over app.course_memberships
 *    is itself subject to that table's RLS. Its only SELECT policy is
 *    `course_memberships__self_or_teacher_select`:
 *    `user_id = (SELECT auth.uid()) OR <teacher of that course>`. Every row
 *    the new subquery could return already satisfies `cm.user_id =
 *    (SELECT auth.uid())` by its own WHERE clause, so the first disjunct is
 *    unconditionally true for exactly those rows: the policy removes nothing
 *    the subquery selects, and the visible membership set is identical to the
 *    RLS-bypassing view has_course_role had. (It also costs nothing: with the
 *    caller's rows already isolated by the PK index, the OR short-circuits on
 *    its cheap left arm and has_course_role is never reached.)
 * 5. Privileges are already in place. DB-04's blanket
 *    `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO
 *    authenticated, app_runtime` gives both roles the SELECT on
 *    app.course_memberships that the inline subquery now needs and the
 *    SECURITY DEFINER call did not. Verified in the preconditions below.
 * 6. Column binding is preserved exactly, including a latent quirk. In the
 *    policies whose predicate lives inside `EXISTS (SELECT 1 FROM
 *    app.learning_activities AS parent ...)` / `... app.problem_attempts AS
 *    parent ...`, the unqualified `course_id` in the original
 *    has_course_role() call binds to the INNER scope, i.e. `parent.course_id`,
 *    not to the outer table's column (confirmed against pg_get_expr of the
 *    live policies). The rewrite therefore spells `parent.course_id`
 *    explicitly. The neighbouring `parent.course_id = course_id` conjunct
 *    deparses as the tautology `parent.course_id = parent.course_id` for the
 *    same reason; it is left exactly as-is. This migration preserves
 *    semantics and does NOT fix that latent no-op -- changing it would tighten
 *    the policy and is out of scope here.
 *
 * WHAT IS DELIBERATELY NOT CHANGED
 * --------------------------------
 * * `internal.has_course_role()` itself: not dropped, not altered, not
 *   re-owned. After this migration exactly ONE caller remains --
 *   `course_memberships__self_or_teacher_select`, the policy ON
 *   app.course_memberships. That one CANNOT be inlined: a policy on
 *   app.course_memberships whose expression selects from app.course_memberships
 *   raises "infinite recursion detected in policy for relation
 *   course_memberships". Its SECURITY DEFINER RLS bypass is precisely what
 *   makes it terminate, and it is also what keeps point 4 above cheap. The
 *   per-row cost there is bounded by that one small table, not by the scans
 *   this migration fixes.
 * * Grants, role memberships, table/column DDL, indexes, functions: untouched.
 *   This migration creates and drops POLICY objects and nothing else.
 * * The two permissive SELECT policies on app.learning_activities
 *   (`learning_activities__owner_all` + `learning_activities__teacher_tutoring_select`,
 *   the Supabase `multiple_permissive_policies` advisor WARN) are left as two
 *   policies. Merging them is NOT trivially semantics-preserving: `owner_all`
 *   is `FOR ALL`, and Postgres offers no way to exclude SELECT from a FOR ALL
 *   policy, so a merge means demoting it into three per-action policies
 *   (INSERT / UPDATE / DELETE) plus one OR'd SELECT policy -- a net +2 policy
 *   objects and a hand-rebuilt UPDATE USING/WITH CHECK pairing, for a change
 *   whose entire motivation (evaluating two policy expressions per row) is
 *   already neutralised by this migration: both expressions become hashed
 *   subplan probes. The risk/benefit does not justify it inside a
 *   semantics-must-be-identical change.
 *
 * SCOPE: 38 of the 45 app-schema policies are rewritten (every policy that
 * called has_course_role, except course_memberships__self_or_teacher_select).
 * The other 7 -- ai_usage_reports__owner_select, course_memberships__self_insert,
 * learner_state__owner_update, question_opportunities__owner_insert,
 * question_opportunities__owner_update, tutoring_messages__owner_delete, and
 * course_memberships__self_or_teacher_select -- never referenced the function
 * and are not touched. The policy count stays 45.
 *
 * Re-runnable: every statement is DROP POLICY IF EXISTS + CREATE POLICY, in
 * the same style as the rest of this chain. Applying to remote projects
 * (TEST, prod) is a human step -- agents never apply remote migrations.
 */

SET lock_timeout = '5s';
SET statement_timeout = '5min';

BEGIN;

DO $preconditions$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        RAISE EXCEPTION 'DB-08d precondition failed: role app_runtime is missing';
    END IF;
    IF to_regprocedure('internal.has_course_role(bigint, text[])') IS NULL THEN
        RAISE EXCEPTION 'DB-08d precondition failed: internal.has_course_role is missing';
    END IF;
    IF (SELECT count(*) FROM pg_policies WHERE schemaname = 'app') <> 45 THEN
        RAISE EXCEPTION
            'DB-08d precondition failed: expected the DB-04+DB-08c baseline of 45 app-schema policies, found %',
            (SELECT count(*) FROM pg_policies WHERE schemaname = 'app');
    END IF;
    -- The inline subquery reads app.course_memberships as the CALLER, which
    -- the SECURITY DEFINER function did not require. DB-04's blanket app-schema
    -- grant already covers this for both roles; fail loudly if it ever does not.
    IF NOT has_table_privilege('app_runtime', 'app.course_memberships', 'SELECT') THEN
        RAISE EXCEPTION
            'DB-08d precondition failed: app_runtime lacks SELECT on app.course_memberships';
    END IF;
    IF NOT has_table_privilege('authenticated', 'app.course_memberships', 'SELECT') THEN
        RAISE EXCEPTION
            'DB-08d precondition failed: authenticated lacks SELECT on app.course_memberships';
    END IF;
END
$preconditions$;

-- ---------------------------------------------------------------------------
-- app.courses -- membership keys off courses.id, not a course_id column.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS courses__member_select ON app.courses;
CREATE POLICY courses__member_select ON app.courses FOR SELECT TO authenticated
    USING (id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid())
          AND cm.role = ANY (ARRAY['student', 'teacher'])
    ));

DROP POLICY IF EXISTS courses__teacher_update ON app.courses;
CREATE POLICY courses__teacher_update ON app.courses FOR UPDATE TO authenticated
    USING (id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ))
    WITH CHECK (id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

-- ---------------------------------------------------------------------------
-- app.documents
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS documents__member_select ON app.documents;
CREATE POLICY documents__member_select ON app.documents FOR SELECT TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid())
          AND cm.role = ANY (ARRAY['student', 'teacher'])
    ));

DROP POLICY IF EXISTS documents__teacher_update ON app.documents;
CREATE POLICY documents__teacher_update ON app.documents FOR UPDATE TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ))
    WITH CHECK (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

DROP POLICY IF EXISTS documents__teacher_delete ON app.documents;
CREATE POLICY documents__teacher_delete ON app.documents FOR DELETE TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

-- ---------------------------------------------------------------------------
-- app.concepts
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS concepts__member_select ON app.concepts;
CREATE POLICY concepts__member_select ON app.concepts FOR SELECT TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid())
          AND cm.role = ANY (ARRAY['student', 'teacher'])
    ));

DROP POLICY IF EXISTS concepts__teacher_update ON app.concepts;
CREATE POLICY concepts__teacher_update ON app.concepts FOR UPDATE TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ))
    WITH CHECK (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

DROP POLICY IF EXISTS concepts__teacher_insert ON app.concepts;
CREATE POLICY concepts__teacher_insert ON app.concepts FOR INSERT TO authenticated
    WITH CHECK (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

DROP POLICY IF EXISTS concepts__teacher_delete ON app.concepts;
CREATE POLICY concepts__teacher_delete ON app.concepts FOR DELETE TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

-- ---------------------------------------------------------------------------
-- app.problems
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS problems__member_select ON app.problems;
CREATE POLICY problems__member_select ON app.problems FOR SELECT TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid())
          AND cm.role = ANY (ARRAY['student', 'teacher'])
    ));

DROP POLICY IF EXISTS problems__teacher_update ON app.problems;
CREATE POLICY problems__teacher_update ON app.problems FOR UPDATE TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ))
    WITH CHECK (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

DROP POLICY IF EXISTS problems__teacher_delete ON app.problems;
CREATE POLICY problems__teacher_delete ON app.problems FOR DELETE TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

-- ---------------------------------------------------------------------------
-- app.course_invites / app.uploads / app.provisioning_runs -- teacher FOR ALL.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS course_invites__teacher_all ON app.course_invites;
CREATE POLICY course_invites__teacher_all ON app.course_invites FOR ALL TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ))
    WITH CHECK (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

DROP POLICY IF EXISTS uploads__teacher_all ON app.uploads;
CREATE POLICY uploads__teacher_all ON app.uploads FOR ALL TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ))
    WITH CHECK (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

DROP POLICY IF EXISTS provisioning_runs__teacher_all ON app.provisioning_runs;
CREATE POLICY provisioning_runs__teacher_all ON app.provisioning_runs FOR ALL TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ))
    WITH CHECK (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

-- ---------------------------------------------------------------------------
-- app.learning_activities -- the owner FOR ALL policy and the teacher
-- tutoring-read policy stay two separate policies (see the header).
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS learning_activities__owner_all ON app.learning_activities;
CREATE POLICY learning_activities__owner_all ON app.learning_activities FOR ALL TO authenticated
    USING (
        user_id = (SELECT auth.uid())
        AND course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid())
              AND cm.role = ANY (ARRAY['student', 'teacher'])
        )
    )
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid())
              AND cm.role = ANY (ARRAY['student', 'teacher'])
        )
    );

DROP POLICY IF EXISTS learning_activities__teacher_tutoring_select ON app.learning_activities;
CREATE POLICY learning_activities__teacher_tutoring_select
    ON app.learning_activities FOR SELECT TO authenticated
    USING (
        modality = 'tutoring'
        AND course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
        )
    );

-- ---------------------------------------------------------------------------
-- app.chat_messages -- the membership check lives INSIDE the parent EXISTS and
-- binds to parent.course_id (see header point 6); `parent.course_id =
-- course_id` is the pre-existing tautology and is reproduced verbatim.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS chat_messages__owner_select ON app.chat_messages;
CREATE POLICY chat_messages__owner_select ON app.chat_messages FOR SELECT TO authenticated
    USING (EXISTS (
        SELECT 1 FROM app.learning_activities AS parent
        WHERE parent.id = learning_activity_id
          AND parent.modality = 'chat'
          AND parent.course_id = course_id
          AND parent.user_id = (SELECT auth.uid())
          AND parent.course_id IN (
              SELECT cm.course_id FROM app.course_memberships AS cm
              WHERE cm.user_id = (SELECT auth.uid())
                AND cm.role = ANY (ARRAY['student', 'teacher'])
          )
    ));

DROP POLICY IF EXISTS chat_messages__owner_insert ON app.chat_messages;
CREATE POLICY chat_messages__owner_insert ON app.chat_messages FOR INSERT TO authenticated
    WITH CHECK (EXISTS (
        SELECT 1 FROM app.learning_activities AS parent
        WHERE parent.id = learning_activity_id
          AND parent.modality = 'chat'
          AND parent.course_id = course_id
          AND parent.user_id = (SELECT auth.uid())
          AND parent.course_id IN (
              SELECT cm.course_id FROM app.course_memberships AS cm
              WHERE cm.user_id = (SELECT auth.uid())
                AND cm.role = ANY (ARRAY['student', 'teacher'])
          )
    ));

-- ---------------------------------------------------------------------------
-- app.tutoring_messages -- owner_delete has no membership check and is not
-- touched.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS tutoring_messages__owner_or_teacher_select ON app.tutoring_messages;
CREATE POLICY tutoring_messages__owner_or_teacher_select ON app.tutoring_messages
    FOR SELECT TO authenticated USING (EXISTS (
        SELECT 1 FROM app.learning_activities AS parent
        WHERE parent.id = learning_activity_id
          AND parent.modality = 'tutoring'
          AND parent.course_id = course_id
          AND (
            parent.user_id = (SELECT auth.uid())
            OR parent.course_id IN (
                SELECT cm.course_id FROM app.course_memberships AS cm
                WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
            )
        )
    ));

DROP POLICY IF EXISTS tutoring_messages__owner_insert ON app.tutoring_messages;
CREATE POLICY tutoring_messages__owner_insert ON app.tutoring_messages FOR INSERT TO authenticated
    WITH CHECK (EXISTS (
        SELECT 1 FROM app.learning_activities AS parent
        WHERE parent.id = learning_activity_id
          AND parent.modality = 'tutoring'
          AND parent.course_id = course_id
          AND parent.user_id = (SELECT auth.uid())
          AND parent.course_id IN (
              SELECT cm.course_id FROM app.course_memberships AS cm
              WHERE cm.user_id = (SELECT auth.uid())
                AND cm.role = ANY (ARRAY['student', 'teacher'])
          )
    ));

DROP POLICY IF EXISTS tutoring_messages__owner_update ON app.tutoring_messages;
CREATE POLICY tutoring_messages__owner_update ON app.tutoring_messages FOR UPDATE TO authenticated
    USING (EXISTS (
        SELECT 1 FROM app.learning_activities AS parent
        WHERE parent.id = learning_activity_id
          AND parent.modality = 'tutoring'
          AND parent.course_id = course_id
          AND parent.user_id = (SELECT auth.uid())
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM app.learning_activities AS parent
        WHERE parent.id = learning_activity_id
          AND parent.modality = 'tutoring'
          AND parent.course_id = course_id
          AND parent.user_id = (SELECT auth.uid())
          AND parent.course_id IN (
              SELECT cm.course_id FROM app.course_memberships AS cm
              WHERE cm.user_id = (SELECT auth.uid())
                AND cm.role = ANY (ARRAY['student', 'teacher'])
          )
    ));

-- ---------------------------------------------------------------------------
-- app.problem_attempts
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS problem_attempts__owner_or_teacher_select ON app.problem_attempts;
CREATE POLICY problem_attempts__owner_or_teacher_select ON app.problem_attempts
    FOR SELECT TO authenticated USING (
        (user_id = (SELECT auth.uid()) AND course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid())
              AND cm.role = ANY (ARRAY['student', 'teacher'])
        ))
        OR course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
        )
    );

DROP POLICY IF EXISTS problem_attempts__owner_insert ON app.problem_attempts;
CREATE POLICY problem_attempts__owner_insert ON app.problem_attempts FOR INSERT TO authenticated
    WITH CHECK (user_id = (SELECT auth.uid()) AND course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid())
          AND cm.role = ANY (ARRAY['student', 'teacher'])
    ));

DROP POLICY IF EXISTS problem_attempts__owner_update ON app.problem_attempts;
CREATE POLICY problem_attempts__owner_update ON app.problem_attempts FOR UPDATE TO authenticated
    USING (user_id = (SELECT auth.uid()) AND course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid())
          AND cm.role = ANY (ARRAY['student', 'teacher'])
    ))
    WITH CHECK (user_id = (SELECT auth.uid()) AND course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid())
          AND cm.role = ANY (ARRAY['student', 'teacher'])
    ));

-- ---------------------------------------------------------------------------
-- app.question_opportunities -- only the SELECT policy checks membership; its
-- teacher arm binds to parent.course_id on app.problem_attempts. owner_insert
-- and owner_update never called has_course_role and are not touched.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS question_opportunities__owner_or_teacher_select ON app.question_opportunities;
CREATE POLICY question_opportunities__owner_or_teacher_select ON app.question_opportunities
    FOR SELECT TO authenticated USING (EXISTS (
        SELECT 1 FROM app.problem_attempts AS parent
        WHERE parent.id = attempt_id AND (
            parent.user_id = (SELECT auth.uid())
            OR parent.course_id IN (
                SELECT cm.course_id FROM app.course_memberships AS cm
                WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
            )
        )
    ));

-- ---------------------------------------------------------------------------
-- app.student_progress -- owner_update's USING has no membership check.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS student_progress__owner_or_teacher_select ON app.student_progress;
CREATE POLICY student_progress__owner_or_teacher_select ON app.student_progress
    FOR SELECT TO authenticated USING (
        user_id = (SELECT auth.uid())
        OR course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
        )
    );

DROP POLICY IF EXISTS student_progress__owner_insert ON app.student_progress;
CREATE POLICY student_progress__owner_insert ON app.student_progress FOR INSERT TO authenticated
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid())
              AND cm.role = ANY (ARRAY['student', 'teacher'])
        )
    );

DROP POLICY IF EXISTS student_progress__owner_update ON app.student_progress;
CREATE POLICY student_progress__owner_update ON app.student_progress FOR UPDATE TO authenticated
    USING (user_id = (SELECT auth.uid()))
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid())
              AND cm.role = ANY (ARRAY['student', 'teacher'])
        )
    );

-- ---------------------------------------------------------------------------
-- app.learner_state -- owner_update has no membership check.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS learner_state__owner_or_teacher_select ON app.learner_state;
CREATE POLICY learner_state__owner_or_teacher_select ON app.learner_state
    FOR SELECT TO authenticated USING (
        user_id = (SELECT auth.uid())
        OR course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
        )
    );

DROP POLICY IF EXISTS learner_state__owner_insert ON app.learner_state;
CREATE POLICY learner_state__owner_insert ON app.learner_state FOR INSERT TO authenticated
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid())
              AND cm.role = ANY (ARRAY['student', 'teacher'])
        )
    );

-- ---------------------------------------------------------------------------
-- app.mastery_events
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS mastery_events__owner_or_teacher_select ON app.mastery_events;
CREATE POLICY mastery_events__owner_or_teacher_select ON app.mastery_events
    FOR SELECT TO authenticated USING (
        user_id = (SELECT auth.uid())
        OR course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
        )
    );

DROP POLICY IF EXISTS mastery_events__owner_insert ON app.mastery_events;
CREATE POLICY mastery_events__owner_insert ON app.mastery_events FOR INSERT TO authenticated
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid())
              AND cm.role = ANY (ARRAY['student', 'teacher'])
        )
    );

-- ---------------------------------------------------------------------------
-- app.learner_entities
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS learner_entities__member_select ON app.learner_entities;
CREATE POLICY learner_entities__member_select ON app.learner_entities FOR SELECT TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid())
          AND cm.role = ANY (ARRAY['student', 'teacher'])
    ));

DROP POLICY IF EXISTS learner_entities__teacher_update ON app.learner_entities;
CREATE POLICY learner_entities__teacher_update ON app.learner_entities FOR UPDATE TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ))
    WITH CHECK (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

DROP POLICY IF EXISTS learner_entities__teacher_insert ON app.learner_entities;
CREATE POLICY learner_entities__teacher_insert ON app.learner_entities FOR INSERT TO authenticated
    WITH CHECK (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

DROP POLICY IF EXISTS learner_entities__teacher_delete ON app.learner_entities;
CREATE POLICY learner_entities__teacher_delete ON app.learner_entities FOR DELETE TO authenticated
    USING (course_id IN (
        SELECT cm.course_id FROM app.course_memberships AS cm
        WHERE cm.user_id = (SELECT auth.uid()) AND cm.role = 'teacher'
    ));

-- ---------------------------------------------------------------------------
-- app.ai_usage_reports -- owner_select has no membership check. The membership
-- arm here is at the TOP level, so it binds to ai_usage_reports.course_id; the
-- `parent.course_id = course_id` conjunct inside the EXISTS is the same
-- pre-existing tautology and is reproduced verbatim.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS ai_usage_reports__owner_insert ON app.ai_usage_reports;
CREATE POLICY ai_usage_reports__owner_insert ON app.ai_usage_reports FOR INSERT TO authenticated
    WITH CHECK (
        user_id = (SELECT auth.uid())
        AND course_id IN (
            SELECT cm.course_id FROM app.course_memberships AS cm
            WHERE cm.user_id = (SELECT auth.uid())
              AND cm.role = ANY (ARRAY['student', 'teacher'])
        )
        AND EXISTS (
            SELECT 1 FROM app.learning_activities AS parent
            WHERE parent.external_id = chat_id
              AND parent.modality = 'chat'
              AND parent.user_id = (SELECT auth.uid())
              AND parent.course_id = course_id
        )
    );

DO $postconditions$
DECLARE
    app_policy_count integer;
    remaining_callers text;
BEGIN
    SELECT count(*) INTO app_policy_count FROM pg_policies WHERE schemaname = 'app';
    IF app_policy_count <> 45 THEN
        RAISE EXCEPTION
            'DB-08d postcondition failed: policy count must stay 45 (rewrite-in-place only), got %',
            app_policy_count;
    END IF;

    -- Exactly one policy may still call has_course_role: the policy ON
    -- app.course_memberships, whose inline form would recurse.
    SELECT string_agg(p.polname, ', ' ORDER BY p.polname) INTO remaining_callers
    FROM pg_policy AS p
    JOIN pg_class AS c ON c.oid = p.polrelid
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = 'app'
      AND (coalesce(pg_get_expr(p.polqual, p.polrelid), '')
           || coalesce(pg_get_expr(p.polwithcheck, p.polrelid), ''))
          LIKE '%has_course_role%';

    IF coalesce(remaining_callers, '') <> 'course_memberships__self_or_teacher_select' THEN
        RAISE EXCEPTION
            'DB-08d postcondition failed: expected has_course_role to remain only in course_memberships__self_or_teacher_select, found [%]',
            coalesce(remaining_callers, '<none>');
    END IF;
END
$postconditions$;

COMMIT;
