-- DB-05 forward-copy reconciliation. Run with psql after copy_app_schema_v1,
-- in the same session when its quarantine rows are needed for operator review.
-- Read-only apart from transaction-local temporary objects and identity-sequence
-- repair. Every mismatch raises an exception so local rehearsal/CI cannot
-- continue on partial data. Sequence changes survive the final ROLLBACK.

-- Standalone reconciliation (for example after reverse delta) still exposes an
-- empty, session-local quarantine. IF NOT EXISTS preserves rows populated by a
-- forward copy performed earlier on this connection.
CREATE TEMP TABLE IF NOT EXISTS db05_copy_quarantine (
    source_table text NOT NULL,
    source_id text NOT NULL,
    reason text NOT NULL,
    source_row jsonb NOT NULL,
    PRIMARY KEY (source_table, source_id, reason)
) ON COMMIT PRESERVE ROWS;

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET CONSTRAINTS ALL IMMEDIATE;

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
            'copy reconciliation failed for %: source count/checksum=%/%, target=%/%',
            mapping_name, source_count, source_checksum, target_count, target_checksum;
    END IF;
END
$function$;

-- The checksum columns are stable identities plus the material transformed
-- fields. Count checks still cover every row in each mapping.
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

DO $structural_reconciliation$
DECLARE
    fk record;
    null_column record;
    identity_column record;
    orphan_exists boolean;
    null_exists boolean;
    sequence_name text;
    sequence_last bigint;
    sequence_called boolean;
    maximum_id bigint;
    next_identity bigint;
    drop_count integer;
    target_count integer;
BEGIN
    -- Generic zero-orphan assertion for every target FK (all DB-04 FKs are single-column).
    FOR fk IN
        SELECT con.oid,
               con.conrelid::regclass::text AS child_table,
               con.confrelid::regclass::text AS parent_table,
               child_att.attname AS child_column,
               parent_att.attname AS parent_column
        FROM pg_constraint con
        JOIN pg_attribute child_att
          ON child_att.attrelid=con.conrelid AND child_att.attnum=con.conkey[1]
        JOIN pg_attribute parent_att
          ON parent_att.attrelid=con.confrelid AND parent_att.attnum=con.confkey[1]
        WHERE con.contype='f'
          AND con.connamespace IN ('app'::regnamespace, 'internal'::regnamespace)
          AND cardinality(con.conkey)=1
    LOOP
        EXECUTE format(
            'SELECT EXISTS (SELECT 1 FROM %s c LEFT JOIN %s p ON c.%I=p.%I WHERE c.%I IS NOT NULL AND p.%I IS NULL)',
            fk.child_table, fk.parent_table, fk.child_column, fk.parent_column,
            fk.child_column, fk.parent_column
        ) INTO orphan_exists;
        IF orphan_exists THEN
            RAISE EXCEPTION 'orphan FK rows: %.% -> %.%', fk.child_table,
                fk.child_column, fk.parent_table, fk.parent_column;
        END IF;
    END LOOP;

    -- Promised NOT NULL columns must be populated, independently of constraints.
    FOR null_column IN
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE table_schema IN ('app','internal') AND is_nullable='NO'
    LOOP
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I.%I WHERE %I IS NULL)',
            null_column.table_schema, null_column.table_name, null_column.column_name)
        INTO null_exists;
        IF null_exists THEN
            RAISE EXCEPTION 'NULL in promised NOT NULL target %.%.%',
                null_column.table_schema, null_column.table_name, null_column.column_name;
        END IF;
    END LOOP;

    -- Explicit identity values do not advance their sequences. Repair any lag
    -- left by the copy/reverse-copy path, then prove the next value is safe.
    FOR identity_column IN
        SELECT table_schema, table_name, column_name, identity_increment::bigint AS increment_by
        FROM information_schema.columns
        WHERE table_schema IN ('app','internal') AND is_identity='YES'
    LOOP
        sequence_name := pg_get_serial_sequence(
            format('%I.%I',identity_column.table_schema,identity_column.table_name),
            identity_column.column_name);
        EXECUTE format('SELECT last_value, is_called FROM %s', sequence_name)
            INTO sequence_last, sequence_called;
        EXECUTE format('SELECT max(%I)::bigint FROM %I.%I', identity_column.column_name,
            identity_column.table_schema, identity_column.table_name) INTO maximum_id;
        next_identity := CASE WHEN sequence_called THEN sequence_last + identity_column.increment_by ELSE sequence_last END;
        IF maximum_id IS NOT NULL AND next_identity <= maximum_id THEN
            PERFORM setval(sequence_name::regclass, maximum_id, true);
            EXECUTE format('SELECT last_value, is_called FROM %s', sequence_name)
                INTO sequence_last, sequence_called;
            next_identity := CASE WHEN sequence_called THEN sequence_last + identity_column.increment_by ELSE sequence_last END;
            IF next_identity <= maximum_id THEN
                RAISE EXCEPTION 'identity sequence % next value % is not above max % after repair',
                    sequence_name, next_identity, maximum_id;
            END IF;
        END IF;
    END LOOP;

    SELECT count(*) INTO drop_count
    FROM (VALUES
        ('apollo_kg_entries'), ('apollo_clarifications'), ('apollo_misconceptions'),
        ('apollo_misconception_observations'), ('apollo_provisioning_jobs'),
        ('apollo_rejected_problems'), ('apollo_kg_negotiations')
    ) AS approved(table_name)
    WHERE to_regclass('public.' || table_name) IS NOT NULL;
    IF drop_count <> 7 THEN
        RAISE EXCEPTION 'expected exactly seven intentionally uncopied source tables, found %', drop_count;
    END IF;

    SELECT count(*) INTO target_count
    FROM information_schema.tables
    WHERE table_schema IN ('app','internal') AND table_type='BASE TABLE';
    IF target_count <> 28 THEN
        RAISE EXCEPTION 'expected exactly 28 target tables, found %', target_count;
    END IF;
END
$structural_reconciliation$;

DO $tally_only_notice$
DECLARE tally_only_count bigint; quarantine_count bigint := 0;
BEGIN
    SELECT count(*) INTO tally_only_count
    FROM public.apollo_question_tally t
    LEFT JOIN public.apollo_reference_question_opportunities o
      USING (attempt_id,reference_node_id)
    WHERE o.id IS NULL;
    IF to_regclass('pg_temp.db05_copy_quarantine') IS NOT NULL THEN
        SELECT count(*) INTO quarantine_count FROM pg_temp.db05_copy_quarantine;
    END IF;
    RAISE NOTICE 'DB-05 reconciliation passed; tally-only question rows copied: %, quarantined source rows: %',
        tally_only_count, quarantine_count;
END
$tally_only_notice$;

ROLLBACK;
