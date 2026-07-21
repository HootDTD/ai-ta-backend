-- DB-16: delayed, SEPARATELY-GATED legacy `public` schema teardown
-- (plan section 5.2's `remove_legacy_public_schema`).
--
-- WHY THIS IS NOT UNDER supabase/migrations/: `supabase db reset` /
-- `node scripts/db/reset-local.mjs` applies every file under
-- supabase/migrations/ unconditionally, and this drop must NOT run as part of
-- that forward chain -- it is a one-shot, irreversible, HUMAN-APPLIED step
-- taken only after production soak + sign-off (plan section 8.3 step 7: "a
-- separate later window ... never combine this irreversible cleanup with the
-- initial cutover"). Living under scripts/db/ mirrors the two other
-- human-run, non-auto-applied scripts already in this directory
-- (reconcile_copy.sql, rollback_reverse_copy.sql) -- neither
-- check-migration-drift.mjs nor reset-local.mjs reads this directory, and CI's
-- migration-drift job only inspects database/migrations/ (frozen) and
-- supabase/migrations/ (active chain), so this file is inert to both.
--
-- APPLY (human, remote, only after the 14-day soak in plan section 8.3):
--   1. Confirm zero in-flight legacy reads: target-aware backend code has
--      been live since activate_app_schema_v1, and this repo's code sweep
--      (see DB-16 report) found zero runtime references to any of the 42
--      legacy public tables outside historical comments and frozen-migration
--      test pins.
--   2. psql "$SUPABASE_DB_URL" -f scripts/db/reconcile_copy.sql   (informational;
--      confirms per-row copy fidelity + reports the quarantine/tally-only counts)
--   3. psql "$SUPABASE_DB_URL" -f scripts/db/remove_legacy_public_schema.sql
--      (this file re-asserts the same copy fidelity itself -- see section 2
--      below -- so step 2 is a redundant human sanity read, not a hard
--      dependency, but run it anyway for the quarantine notice this file does
--      not reproduce).
--
-- REHEARSED LOCALLY by tests/database/test_remove_legacy_public_schema.py
-- (Testcontainers Postgres): forward chain -> copy -> this script, proving
-- (a) preconditions pass on a correctly-copied database, (b) the full
-- child-first DROP order succeeds with no CASCADE anywhere, and (c) a
-- tampered precondition (a deleted copied row, an over-ceiling no-copy table)
-- aborts loudly before any DROP runs.
--
-- Never edit an applied migration; this file is not one (see above), so it is
-- edited in place across DB-16 iterations until the day it is actually run.

SET lock_timeout = '5s';
SET statement_timeout = '5min';

BEGIN;
SET CONSTRAINTS ALL IMMEDIATE;

-- ============================================================================
-- SECTION 0 -- structural precondition: this script matches the schema it was
-- authored against. If the legacy table count drifted (someone already
-- dropped something, or a new legacy table appeared), stop rather than run a
-- stale drop list against an unknown shape.
-- ============================================================================
DO $structural_precheck$
DECLARE
    legacy_count integer;
BEGIN
    SELECT count(*) INTO legacy_count
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
    IF legacy_count <> 42 THEN
        RAISE EXCEPTION
            'remove_legacy_public_schema precondition failed: expected exactly 42 legacy public base tables, found % -- this script is stale or the schema has drifted since it was authored; do not proceed blindly',
            legacy_count;
    END IF;

    IF to_regclass('app.courses') IS NULL OR to_regclass('internal.grading_runs') IS NULL THEN
        RAISE EXCEPTION
            'remove_legacy_public_schema requires create_app_schema_v1 (and copy_app_schema_v1) to have run first';
    END IF;
END
$structural_precheck$;

-- ============================================================================
-- SECTION 1 -- the 7 approved no-copy tables (plan section 4's six, plus
-- apollo_kg_negotiations per amendment A6). copy_app_schema_v1 deliberately
-- never copies these, so there is no target-side row to reconcile against;
-- the only available signal is that each stayed within its approved
-- row-count expectation. This is a generous-headroom ceiling, not an
-- exact-match assertion: apollo_misconception_observations/misconceptions/
-- clarifications/rejected_problems/provisioning_jobs/kg_entries were all
-- 0-3 rows as of the 2026-07-16 analysis (.planning/cleanup/findings/
-- c-db-usage.md) and their writers were removed in this same cleanup
-- (T-E/T-F/U2/U3/U4/DB-13), so they are frozen by the time this runs.
-- apollo_kg_negotiations is the one exception: DECISIONS.md ruling #3
-- (2026-07-16) found it WAS unconditionally live-written until amendment A6
-- (2026-07-20) reversed course and DB-13 (2026-07-21) removed the write path
-- -- so it may hold a real, larger prod count than the others. The ceiling
-- below tolerates that organic accrual; a breach means STOP and get human
-- sign-off, not "raise the ceiling and retry".
-- ============================================================================
DO $no_copy_row_counts$
DECLARE
    approved_ceiling CONSTANT integer := 10000;
    row_count integer;
    table_name text;
BEGIN
    FOR table_name IN
        SELECT unnest(ARRAY[
            'apollo_clarifications',
            'apollo_misconception_observations',
            'apollo_misconceptions',
            'apollo_rejected_problems',
            'apollo_provisioning_jobs',
            'apollo_kg_entries',
            'apollo_kg_negotiations'
        ])
    LOOP
        IF to_regclass('public.' || table_name) IS NULL THEN
            RAISE EXCEPTION
                'remove_legacy_public_schema precondition failed: approved no-copy table public.% is missing (already dropped, renamed, or never existed) -- stop and investigate before proceeding',
                table_name;
        END IF;
        EXECUTE format('SELECT count(*) FROM public.%I', table_name) INTO row_count;
        IF row_count > approved_ceiling THEN
            RAISE EXCEPTION
                'remove_legacy_public_schema precondition failed: public.% has % rows, above the approved no-copy ceiling of % -- it may still be live-written; get human sign-off before dropping',
                table_name, row_count, approved_ceiling;
        END IF;
        RAISE NOTICE 'remove_legacy_public_schema: public.% has % row(s) (within approved no-copy ceiling %)',
            table_name, row_count, approved_ceiling;
    END LOOP;
END
$no_copy_row_counts$;

-- ============================================================================
-- SECTION 2 -- apollo_graph_comparison_runs / apollo_graph_comparison_findings:
-- discovered during DB-16 authoring, NOT on the original approved-7 list.
-- Amendment A7 (2026-07-20, "graph-grader roadmap ABANDONED") deleted the
-- shadow chain that wrote them and dropped `internal.grading_findings` /
-- the comparison-run leg of `internal.grading_runs` from the target
-- entirely (confirmed: copy_app_schema_v1.sql's `internal.grading_runs`
-- INSERT sources only from apollo_grading_artifacts, never from either
-- comparison table; DB-14 deleted the GraphComparisonRun/GraphComparisonFinding
-- ORM models outright with zero remaining runtime importers). They are
-- therefore superseded with NO copy target, same as the approved-7 --
-- MAJOR-3 / amendment A4 require the full teardown to cover every
-- superseded legacy table, not only the originally-named six/seven.
-- apollo_graph_comparison_findings held 113 rows and
-- apollo_graph_comparison_runs held 3 (shadow-writing) as of 2026-07-16;
-- same generous-headroom ceiling logic as section 1.
-- ============================================================================
DO $abandoned_roadmap_row_counts$
DECLARE
    approved_ceiling CONSTANT integer := 10000;
    row_count integer;
    table_name text;
BEGIN
    FOR table_name IN
        SELECT unnest(ARRAY[
            'apollo_graph_comparison_runs',
            'apollo_graph_comparison_findings'
        ])
    LOOP
        IF to_regclass('public.' || table_name) IS NULL THEN
            RAISE EXCEPTION
                'remove_legacy_public_schema precondition failed: abandoned-roadmap table public.% is missing (already dropped, renamed, or never existed) -- stop and investigate before proceeding',
                table_name;
        END IF;
        EXECUTE format('SELECT count(*) FROM public.%I', table_name) INTO row_count;
        IF row_count > approved_ceiling THEN
            RAISE EXCEPTION
                'remove_legacy_public_schema precondition failed: public.% has % rows, above the abandoned-roadmap ceiling of % -- get human sign-off before dropping',
                table_name, row_count, approved_ceiling;
        END IF;
        RAISE NOTICE 'remove_legacy_public_schema: public.% has % row(s) (within abandoned-roadmap ceiling %)',
            table_name, row_count, approved_ceiling;
    END LOOP;
END
$abandoned_roadmap_row_counts$;

-- ============================================================================
-- SECTION 3 -- the remaining 33 legacy tables ARE copied by copy_app_schema_v1.
-- Assert the copy actually landed before dropping the source: same
-- count+checksum mapping technique as scripts/db/reconcile_copy.sql (the
-- assert_copy_mapping helper and every mapping query below are copied
-- verbatim from that script so this migration does not silently drift from
-- the reviewed reconciliation logic). Run reconcile_copy.sql by hand first
-- for the human-readable quarantine/tally-only notice; this section is the
-- hard machine gate this migration depends on.
-- ============================================================================
CREATE OR REPLACE FUNCTION pg_temp.assert_copy_mapping(
    mapping_name text,
    source_query text,
    target_query text
) RETURNS void
LANGUAGE plpgsql
AS $function$
DECLARE
    source_count bigint;
    target_count bigint;
    source_checksum text;
    target_checksum text;
BEGIN
    EXECUTE format(
        'SELECT count(*), COALESCE(md5(string_agg(row_hash, '''' ORDER BY row_hash)), md5(''''))
           FROM (SELECT md5(to_jsonb(q)::text) AS row_hash FROM (%s) AS q) AS hashed',
        source_query
    ) INTO source_count, source_checksum;
    EXECUTE format(
        'SELECT count(*), COALESCE(md5(string_agg(row_hash, '''' ORDER BY row_hash)), md5(''''))
           FROM (SELECT md5(to_jsonb(q)::text) AS row_hash FROM (%s) AS q) AS hashed',
        target_query
    ) INTO target_count, target_checksum;

    IF source_count <> target_count OR source_checksum <> target_checksum THEN
        RAISE EXCEPTION
            'remove_legacy_public_schema precondition failed: copy reconciliation failed for %: source count/checksum=%/%, target=%/% -- the forward copy did not land cleanly, do not drop the source',
            mapping_name, source_count, source_checksum, target_count, target_checksum;
    END IF;
END
$function$;

SELECT pg_temp.assert_copy_mapping('courses',
    $q$SELECT s.id, s.slug::text, COALESCE(tc.current_week, 1)::smallint AS current_week,
              COALESCE(s.weight_overrides, '{}'::jsonb) || COALESCE(tc.weights, '{}'::jsonb) AS retrieval_weights
       FROM public.aita_search_spaces s
       LEFT JOIN public.teacher_courses tc ON tc.search_space_id = s.id$q$,
    $q$SELECT id, slug, current_week, retrieval_weights FROM app.courses$q$);

SELECT pg_temp.assert_copy_mapping('course_memberships',
    $q$SELECT user_id, search_space_id::bigint AS course_id, role FROM public.course_memberships$q$,
    $q$SELECT user_id, course_id, role FROM app.course_memberships$q$);

SELECT pg_temp.assert_copy_mapping('course_invites',
    $q$SELECT id, code, search_space_id::bigint AS course_id, role, is_active FROM public.course_invite_links$q$,
    $q$SELECT id, code, course_id, role, is_active FROM app.course_invites$q$);

SELECT pg_temp.assert_copy_mapping('documents',
    $q$SELECT id::bigint AS id, search_space_id::bigint AS course_id, content_hash::text,
              CASE WHEN status->>'state' IN ('queued','processing','ready','failed')
                   THEN status->>'state' ELSE 'ready' END AS status
       FROM public.aita_documents$q$,
    $q$SELECT id, course_id, content_hash, status FROM app.documents$q$);

SELECT pg_temp.assert_copy_mapping('document_chunks',
    $q$SELECT c.id::bigint AS id, d.search_space_id::bigint AS course_id,
              c.document_id::bigint AS document_id, md5(c.content) AS content_hash
       FROM public.aita_chunks c JOIN public.aita_documents d ON d.id=c.document_id$q$,
    $q$SELECT id, course_id, document_id, md5(content) AS content_hash FROM internal.document_chunks$q$);

SELECT pg_temp.assert_copy_mapping('uploads',
    $q$SELECT id, search_space_id::bigint AS course_id, doc_id::bigint AS document_id,
              week, kind, status FROM public.teacher_uploads$q$,
    $q$SELECT id, course_id, document_id, week, kind, status FROM app.uploads$q$);

SELECT pg_temp.assert_copy_mapping('upload_jobs',
    $q$SELECT j.id, u.search_space_id::bigint AS course_id, j.upload_id, j.state
       FROM public.teacher_upload_jobs j JOIN public.teacher_uploads u ON u.id=j.upload_id$q$,
    $q$SELECT id, course_id, upload_id, state FROM internal.upload_jobs$q$);

SELECT pg_temp.assert_copy_mapping('learning_activities_chat',
    $q$SELECT s.id, 'chat'::text AS modality, s.chat_id AS external_id, s.user_id,
              s.search_space_id::bigint AS course_id, 'active'::text AS status
       FROM public.chat_sessions s
       JOIN app.learning_activities a ON a.id=s.id AND a.modality='chat'$q$,
    $q$SELECT id, modality, external_id, user_id, course_id, status
       FROM app.learning_activities WHERE modality='chat'$q$);

SELECT pg_temp.assert_copy_mapping('chat_messages',
    $q$SELECT t.id, s.search_space_id::bigint AS course_id, t.chat_session_id,
               t.turn_index, t.turn_id AS external_id, 'message'::text AS kind
       FROM public.chat_turns t JOIN public.chat_sessions s ON s.id=t.chat_session_id
       JOIN app.learning_activities la ON la.id=s.id AND la.modality='chat'$q$,
    $q$SELECT id, course_id, learning_activity_id AS chat_session_id,
              turn_index, external_id, kind FROM app.chat_messages$q$);

SELECT pg_temp.assert_copy_mapping('chat_session_snippets',
    $q$SELECT sn.chat_session_id, sn.chunk_id, s.search_space_id::bigint AS course_id
       FROM public.chat_session_snippets sn JOIN public.chat_sessions s ON s.id=sn.chat_session_id
       JOIN app.learning_activities la ON la.id=s.id AND la.modality='chat'$q$,
    $q$SELECT learning_activity_id AS chat_session_id, chunk_id, course_id
       FROM internal.chat_session_snippets$q$);

SELECT pg_temp.assert_copy_mapping('chat_routing_decisions',
    $q$SELECT r.id, s.search_space_id::bigint AS course_id, r.chat_session_id, r.turn_id
       FROM public.chat_router_decisions r JOIN public.chat_sessions s ON s.id=r.chat_session_id
       JOIN app.learning_activities la ON la.id=s.id AND la.modality='chat'$q$,
    $q$SELECT id, course_id, learning_activity_id AS chat_session_id, turn_id
       FROM internal.chat_routing_decisions$q$);

SELECT pg_temp.assert_copy_mapping('learning_activities_tutoring',
    $q$SELECT s.id+1000000 AS id, 'tutoring'::text AS modality, s.user_id,
              s.search_space_id::bigint AS course_id, s.concept_id, s.status, s.phase
       FROM public.apollo_sessions s
       JOIN app.learning_activities a ON a.id=s.id+1000000 AND a.modality='tutoring'$q$,
    $q$SELECT id, modality, user_id, course_id, concept_id, status, phase
       FROM app.learning_activities WHERE modality='tutoring'$q$);

SELECT pg_temp.assert_copy_mapping('tutoring_messages',
    $q$SELECT m.id, s.search_space_id::bigint AS course_id,
              m.session_id+1000000 AS session_id, m.attempt_id, m.turn_index
       FROM public.apollo_messages m JOIN public.apollo_sessions s ON s.id=m.session_id
       JOIN app.learning_activities la
         ON la.id=s.id+1000000 AND la.modality='tutoring'$q$,
    $q$SELECT id, course_id, learning_activity_id AS session_id,
              attempt_id, turn_index FROM app.tutoring_messages$q$);

SELECT pg_temp.assert_copy_mapping('problem_attempts',
    $q$SELECT a.id, s.search_space_id::bigint AS course_id, s.user_id,
              a.session_id+1000000 AS session_id,
              p.id AS problem_id, a.result
       FROM public.apollo_problem_attempts a
       JOIN public.apollo_sessions s ON s.id=a.session_id
       JOIN app.learning_activities la
         ON la.id=s.id+1000000 AND la.modality='tutoring'
       JOIN app.problems p ON p.course_id=s.search_space_id AND p.problem_code=a.problem_id
          AND (s.concept_id IS NULL OR p.concept_id=s.concept_id)$q$,
    $q$SELECT id, course_id, user_id, learning_activity_id AS session_id,
              problem_id, result FROM app.problem_attempts$q$);

SELECT pg_temp.assert_copy_mapping('student_progress',
    $q$WITH candidates AS (
         SELECT p.user_id, s.search_space_id::bigint AS course_id
         FROM public.apollo_student_progress p JOIN public.apollo_sessions s ON s.user_id=p.user_id
         UNION
         SELECT p.user_id, m.search_space_id::bigint
         FROM public.apollo_student_progress p JOIN public.course_memberships m ON m.user_id=p.user_id
       )
       SELECT p.user_id, min(c.course_id) AS course_id, p.xp_total, p.level
       FROM public.apollo_student_progress p JOIN candidates c ON c.user_id=p.user_id
       GROUP BY p.user_id, p.xp_total, p.level$q$,
    $q$SELECT user_id, course_id, xp_total, level FROM app.student_progress$q$);

SELECT pg_temp.assert_copy_mapping('concepts',
    $q$SELECT c.id, s.search_space_id::bigint AS course_id,
              s.slug AS subject_slug, s.display_name AS subject_display_name, c.slug
       FROM public.apollo_concepts c JOIN public.apollo_subjects s ON s.id=c.subject_id$q$,
    $q$SELECT id, course_id, subject_slug, subject_display_name, slug FROM app.concepts$q$);

SELECT pg_temp.assert_copy_mapping('problems',
    $q$SELECT p.id, COALESCE(p.search_space_id,s.search_space_id)::bigint AS course_id,
              p.concept_id, p.problem_code, p.payload->>'problem_text' AS problem_text
       FROM public.apollo_concept_problems p
       JOIN public.apollo_concepts c ON c.id=p.concept_id
       JOIN public.apollo_subjects s ON s.id=c.subject_id$q$,
    $q$SELECT id, course_id, concept_id, problem_code, problem_text FROM app.problems$q$);

SELECT pg_temp.assert_copy_mapping('provisioning_runs',
    $q$SELECT authored.id, authored.search_space_id::bigint AS course_id,
              'authored_set'::text AS kind, authored.set_index,
              authored.problem_document_id, authored.solution_document_id,
              NULL::bigint AS concept_id, NULL::bigint AS ingest_run_id, authored.status
       FROM public.apollo_authored_sets AS authored
       UNION ALL
       SELECT generation.id+1000000, generation.search_space_id::bigint,
              'generation'::text, NULL::integer, NULL::bigint, NULL::bigint,
              generation.concept_id, generation.ingest_run_id, generation.status
       FROM public.apollo_generation_runs AS generation$q$,
    $q$SELECT id, course_id, kind, set_index, problem_document_id,
              solution_document_id, concept_id, ingest_run_id, status
       FROM app.provisioning_runs$q$);

SELECT pg_temp.assert_copy_mapping('question_opportunities',
    $q$SELECT a.attempt_id, pa.session_id+1000000 AS learning_activity_id,
              a.reference_node_id,
              COALESCE(t.status,a.state) AS state, COALESCE(t.times_asked,0) AS times_asked
       FROM public.apollo_reference_question_opportunities a
       JOIN public.apollo_problem_attempts pa ON pa.id=a.attempt_id
       LEFT JOIN public.apollo_question_tally t
         ON t.attempt_id=a.attempt_id AND t.reference_node_id=a.reference_node_id
       UNION ALL
       SELECT t.attempt_id, pa.session_id+1000000, t.reference_node_id,
              t.status, t.times_asked
       FROM public.apollo_question_tally t
       JOIN public.apollo_problem_attempts pa ON pa.id=t.attempt_id
       LEFT JOIN public.apollo_reference_question_opportunities a
         ON a.attempt_id=t.attempt_id AND a.reference_node_id=t.reference_node_id
       WHERE a.id IS NULL$q$,
    $q$SELECT attempt_id, learning_activity_id, reference_node_id, state, times_asked
       FROM app.question_opportunities$q$);

SELECT pg_temp.assert_copy_mapping('learner_entities',
    $q$SELECT e.id, s.search_space_id::bigint AS course_id, e.concept_id, e.canonical_key, e.kind
       FROM public.apollo_kg_entities e
       JOIN public.apollo_concepts c ON c.id=e.concept_id
       JOIN public.apollo_subjects s ON s.id=c.subject_id
       WHERE e.kind <> 'misconception'$q$,
    $q$SELECT id, course_id, concept_id, canonical_key, kind FROM app.learner_entities$q$);

SELECT pg_temp.assert_copy_mapping('entity_prerequisites',
    $q$SELECT f.course_id, p.from_entity_id, p.to_entity_id
       FROM public.apollo_entity_prereqs p
       JOIN app.learner_entities f ON f.id=p.from_entity_id
       JOIN app.learner_entities t ON t.id=p.to_entity_id AND t.course_id=f.course_id$q$,
    $q$SELECT course_id, from_entity_id, to_entity_id FROM internal.entity_prerequisites$q$);

SELECT pg_temp.assert_copy_mapping('learner_state',
    $q$SELECT ls.user_id, ls.search_space_id::bigint AS course_id, ls.entity_id
       FROM public.apollo_learner_state ls JOIN app.learner_entities e ON e.id=ls.entity_id$q$,
    $q$SELECT user_id, course_id, entity_id FROM app.learner_state$q$);

SELECT pg_temp.assert_copy_mapping('mastery_events',
    $q$SELECT me.id, me.user_id, me.search_space_id::bigint AS course_id, me.entity_id,
              me.attempt_id, me.event_kind
       FROM public.apollo_mastery_events me JOIN app.learner_entities e ON e.id=me.entity_id$q$,
    $q$SELECT id, user_id, course_id, entity_id, attempt_id, event_kind FROM app.mastery_events$q$);

SELECT pg_temp.assert_copy_mapping('content_ingest_runs',
    $q$SELECT id, search_space_id::bigint AS course_id, document_id, content_hash, status FROM public.apollo_ingest_runs$q$,
    $q$SELECT id, course_id, document_id, content_hash, status FROM internal.content_ingest_runs$q$);

SELECT pg_temp.assert_copy_mapping('content_ingest_errors',
    $q$SELECT id, search_space_id::bigint AS course_id, ingest_run_id, stage, error_class FROM public.apollo_ingest_errors$q$,
    $q$SELECT id, course_id, ingest_run_id, stage, error_class FROM internal.content_ingest_errors$q$);

SELECT pg_temp.assert_copy_mapping('ingest_page_evidence',
    $q$SELECT id, search_space_id::bigint AS course_id, ingest_run_id, document_id, role, page_number
       FROM public.apollo_ingest_page_evidence$q$,
    $q$SELECT id, course_id, ingest_run_id, document_id, role, page_number FROM internal.ingest_page_evidence$q$);

SELECT pg_temp.assert_copy_mapping('dedup_decisions',
    $q$SELECT d.id, d.search_space_id::bigint AS course_id, d.ingest_run_id, d.concept_id,
              e.id AS matched_entity_id, d.candidate_key, d.verdict
       FROM public.apollo_dedup_decisions d LEFT JOIN app.learner_entities e ON e.id=d.matched_entity_id$q$,
    $q$SELECT id, course_id, ingest_run_id, concept_id, matched_entity_id, candidate_key, verdict FROM internal.dedup_decisions$q$);

SELECT pg_temp.assert_copy_mapping('grading_runs',
    $q$SELECT attempt_id, role, COALESCE(versions->>'grader','legacy') AS grader_version
       FROM public.apollo_grading_artifacts$q$,
    $q$SELECT attempt_id, role, grader_version FROM internal.grading_runs$q$);

SELECT pg_temp.assert_copy_mapping('ai_usage_reports',
    $q$SELECT r.id, s.user_id, s.search_space_id::bigint AS course_id, r.chat_id
       FROM public.ai_use_reports r JOIN public.chat_sessions s ON s.chat_id=r.chat_id$q$,
    $q$SELECT id, user_id, course_id, chat_id FROM app.ai_usage_reports$q$);

-- Belt-and-braces structural check matching reconcile_copy.sql's own gate:
-- the target inventory is 28 tables (MIG-AMEND, 2026-07-20).
DO $target_count_check$
DECLARE
    target_count integer;
BEGIN
    SELECT count(*) INTO target_count
    FROM information_schema.tables
    WHERE table_schema IN ('app', 'internal') AND table_type = 'BASE TABLE';
    IF target_count <> 28 THEN
        RAISE EXCEPTION
            'remove_legacy_public_schema precondition failed: expected exactly 28 target tables, found %',
            target_count;
    END IF;
END
$target_count_check$;

-- ============================================================================
-- SECTION 4 -- remove superseded legacy functions. public.fetch_items,
-- public.fts_count, and public.hybrid_search were hand-created directly on
-- the live Supabase project (never captured in a migration file -- same
-- provenance gap as the old ai_use_reports RLS policies; see
-- .planning/cleanup/inputs/db-preflight.md for the captured bodies) and are
-- superseded by DB-06's internal.fetch_items / internal.fts_count /
-- internal.hybrid_search. Found by name+namespace and dropped via
-- ::regprocedure so the statement never has to spell out an unqualified
-- `vector` parameter type whose resolution depends on where the pgvector
-- extension currently lives (public vs extensions, plan section 7). Only
-- fetch_items's `RETURNS SETOF aita_chunks` creates a real catalog
-- dependency that would block a plain DROP TABLE aita_chunks below; all
-- three are dropped for completeness regardless. IF-EXISTS by construction:
-- the loop body is a no-op wherever these were never hand-created (e.g. this
-- local rehearsal, whose snapshot never created them).
-- ============================================================================
DO $drop_legacy_functions$
DECLARE
    legacy_function record;
BEGIN
    FOR legacy_function IN
        SELECT p.oid
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proname IN ('fetch_items', 'fts_count', 'hybrid_search')
    LOOP
        EXECUTE format('DROP FUNCTION %s', legacy_function.oid::regprocedure);
    END LOOP;
END
$drop_legacy_functions$;

-- ============================================================================
-- SECTION 5 -- the 7 approved no-copy drops, plan section 4's stated
-- child-first order (the six have no FKs to each other) with
-- apollo_kg_negotiations appended per amendment A6. Plain DROP TABLE, no
-- CASCADE anywhere in this file: a failure here is a safety signal that an
-- unexpected dependency exists, not friction to route around.
-- ============================================================================
DROP TABLE public.apollo_clarifications;
DROP TABLE public.apollo_misconception_observations;
DROP TABLE public.apollo_misconceptions;
DROP TABLE public.apollo_rejected_problems;
DROP TABLE public.apollo_provisioning_jobs;
DROP TABLE public.apollo_kg_entries;
DROP TABLE public.apollo_kg_negotiations;

-- ============================================================================
-- SECTION 6 -- every other superseded legacy public table (35 remaining),
-- full child-first FK-safe order derived from the FK web in
-- .planning/cleanup/findings/c-db-usage.md (see the DB-16 report for the
-- round-by-round topological derivation). aita_search_spaces is the global
-- FK root (21 inbound references) and drops last, per section 4/5.2.
-- ============================================================================

-- 6a. Leaves: nothing else references these via FK (order within this group
--     is otherwise unconstrained by the FK web). course_memberships is
--     deliberately NOT here despite having no FK dependents: its RLS
--     policies are referenced BY policies on teacher_courses, teacher_uploads,
--     and course_invite_links (migrations 006/008 -- a POLICY dependency,
--     which plain DROP TABLE enforces the same as an FK and c-db-usage.md's
--     FK-only web did not surface), so it must drop after all three -- see
--     the end of 6b, once teacher_uploads (round-2, not a leaf) is gone too.
DROP TABLE public.chat_turns;
DROP TABLE public.chat_session_snippets;
DROP TABLE public.chat_router_decisions;
DROP TABLE public.teacher_upload_jobs;
DROP TABLE public.course_invite_links;
DROP TABLE public.teacher_courses;
DROP TABLE public.apollo_messages;
DROP TABLE public.apollo_grading_artifacts;
DROP TABLE public.apollo_reference_question_opportunities;
DROP TABLE public.apollo_question_tally;
DROP TABLE public.apollo_authored_sets;
DROP TABLE public.apollo_entity_prereqs;
DROP TABLE public.apollo_learner_state;
DROP TABLE public.apollo_mastery_events;
DROP TABLE public.apollo_graph_comparison_findings;
DROP TABLE public.apollo_dedup_decisions;
DROP TABLE public.apollo_ingest_errors;
DROP TABLE public.apollo_ingest_page_evidence;
DROP TABLE public.apollo_generation_runs;
DROP TABLE public.ai_use_reports;
DROP TABLE public.apollo_student_progress;

-- 6b. Now-childless after 6a. course_memberships is appended last: by this
--     point teacher_courses/course_invite_links (6a) and teacher_uploads
--     (this round) -- the three tables whose RLS policies reference it --
--     have all dropped, releasing course_memberships's only remaining
--     (policy, not FK) dependents.
DROP TABLE public.aita_chunks;
DROP TABLE public.teacher_uploads;
DROP TABLE public.chat_sessions;
DROP TABLE public.apollo_kg_entities;
DROP TABLE public.apollo_ingest_runs;
DROP TABLE public.apollo_graph_comparison_runs;
DROP TABLE public.apollo_concept_problems;
DROP TABLE public.course_memberships;

-- 6c. Now-childless after 6b.
DROP TABLE public.aita_documents;
DROP TABLE public.apollo_problem_attempts;

-- 6d. Now-childless after 6c.
DROP TABLE public.apollo_sessions;

-- 6e. Now-childless after 6d.
DROP TABLE public.apollo_concepts;

-- 6f. Now-childless after 6e.
DROP TABLE public.apollo_subjects;

-- 6g. The global FK root. LAST.
DROP TABLE public.aita_search_spaces;

-- ============================================================================
-- SECTION 7 -- post-check: every one of the 42 known legacy tables is gone.
-- Checked by exact name (not a blanket "public schema is empty" assertion)
-- so any unrelated Supabase-managed public object is left untouched and
-- does not trip a false failure, per plan section 5.2 ("Supabase-managed
-- public objects are not dropped").
-- ============================================================================
DO $post_check$
DECLARE
    remaining_count integer;
BEGIN
    -- Aliased ``legacy_name`` (not ``table_name``): PL/pgSQL raises
    -- AmbiguousColumnError if a query column alias collides with a variable
    -- name declared in the same DO block's DECLARE section.
    SELECT count(*) INTO remaining_count
    FROM unnest(ARRAY[
        'apollo_clarifications', 'apollo_misconception_observations', 'apollo_misconceptions',
        'apollo_rejected_problems', 'apollo_provisioning_jobs', 'apollo_kg_entries',
        'apollo_kg_negotiations',
        'chat_turns', 'chat_session_snippets', 'chat_router_decisions', 'teacher_upload_jobs',
        'course_memberships', 'course_invite_links', 'teacher_courses', 'apollo_messages',
        'apollo_grading_artifacts', 'apollo_reference_question_opportunities',
        'apollo_question_tally', 'apollo_authored_sets', 'apollo_entity_prereqs',
        'apollo_learner_state', 'apollo_mastery_events', 'apollo_graph_comparison_findings',
        'apollo_dedup_decisions', 'apollo_ingest_errors', 'apollo_ingest_page_evidence',
        'apollo_generation_runs', 'ai_use_reports', 'apollo_student_progress',
        'aita_chunks', 'teacher_uploads', 'chat_sessions', 'apollo_kg_entities',
        'apollo_ingest_runs', 'apollo_graph_comparison_runs', 'apollo_concept_problems',
        'aita_documents', 'apollo_problem_attempts', 'apollo_sessions', 'apollo_concepts',
        'apollo_subjects', 'aita_search_spaces'
    ]) AS legacy_name
    WHERE to_regclass('public.' || legacy_name) IS NOT NULL;

    IF remaining_count <> 0 THEN
        RAISE EXCEPTION
            'remove_legacy_public_schema post-check failed: % of the 42 legacy tables still exist after the drop sequence',
            remaining_count;
    END IF;
END
$post_check$;

COMMIT;
