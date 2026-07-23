-- DB-05: non-destructive, idempotent copy from the frozen legacy public schema.
--
-- The following SEVEN approved-drop tables are intentionally NOT copied:
--   public.apollo_kg_entries
--   public.apollo_clarifications
--   public.apollo_misconceptions
--   public.apollo_misconception_observations
--   public.apollo_provisioning_jobs
--   public.apollo_rejected_problems
--   public.apollo_kg_negotiations
--
-- No statement in this migration deletes from or alters a legacy table.
-- Cutover requires a maintenance window with zero in-flight Apollo sessions;
-- legacy tutoring ids are shifted by +1000000, while chat ids remain unchanged.

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DO $copy_preconditions$
BEGIN
    IF to_regclass('public.aita_search_spaces') IS NULL THEN
        RAISE EXCEPTION 'copy_app_schema_v1 requires the legacy public snapshot';
    END IF;
    IF to_regclass('app.courses') IS NULL OR to_regclass('internal.grading_runs') IS NULL THEN
        RAISE EXCEPTION 'copy_app_schema_v1 requires create_app_schema_v1 first';
    END IF;
END
$copy_preconditions$;

-- Re-routed rows that cannot be attributed without inventing ownership are
-- retained in a session-local quarantine for reconciliation/operator review.
-- Run reconciliation in this same session so it can balance these rows.
CREATE TEMP TABLE IF NOT EXISTS db05_copy_quarantine (
    source_table text NOT NULL,
    source_id text NOT NULL,
    reason text NOT NULL,
    source_row jsonb NOT NULL,
    PRIMARY KEY (source_table, source_id, reason)
) ON COMMIT PRESERVE ROWS;
TRUNCATE pg_temp.db05_copy_quarantine;

DO $id_remap_boundaries$
BEGIN
    IF EXISTS (SELECT 1 FROM public.chat_sessions WHERE id <= 0)
       OR EXISTS (SELECT 1 FROM public.apollo_sessions WHERE id <= 0)
       OR EXISTS (SELECT 1 FROM public.apollo_generation_runs WHERE id <= 0)
       OR EXISTS (SELECT 1 FROM public.apollo_authored_sets WHERE id <= 0) THEN
        RAISE EXCEPTION 'copy id remap requires positive legacy ids';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.chat_sessions AS chat
        JOIN public.apollo_sessions AS tutoring
          ON chat.id = tutoring.id + 1000000
    ) THEN
        RAISE EXCEPTION 'learning activity id remap collides at the +1000000 boundary';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.apollo_authored_sets AS authored
        JOIN public.apollo_generation_runs AS generation
          ON authored.id = generation.id + 1000000
    ) THEN
        RAISE EXCEPTION 'provisioning run id remap collides at the +1000000 boundary';
    END IF;
END
$id_remap_boundaries$;

-- Courses merge search-space identity and teacher-owned runtime controls.
INSERT INTO app.courses (
    id, name, slug, subject_name, current_week, retrieval_weights,
    retrieval_weight_min, retrieval_weight_max, metadata, created_at, updated_at
)
SELECT
    s.id::bigint,
    s.name::text,
    s.slug::text,
    s.subject_name::text,
    COALESCE(tc.current_week, 1)::smallint,
    COALESCE(s.weight_overrides, '{}'::jsonb) || COALESCE(tc.weights, '{}'::jsonb),
    CASE WHEN jsonb_typeof(tc.weight_bounds -> 'min') = 'number'
        THEN (tc.weight_bounds ->> 'min')::real ELSE 0 END,
    CASE WHEN jsonb_typeof(tc.weight_bounds -> 'max') = 'number'
        THEN (tc.weight_bounds ->> 'max')::real ELSE 1 END,
    COALESCE(s.metadata, '{}'::jsonb),
    s.created_at,
    s.updated_at
FROM public.aita_search_spaces AS s
LEFT JOIN public.teacher_courses AS tc ON tc.search_space_id = s.id
ON CONFLICT DO NOTHING;

INSERT INTO app.course_memberships (user_id, course_id, role, created_at, updated_at)
SELECT user_id, search_space_id::bigint, role, created_at, updated_at
FROM public.course_memberships
ON CONFLICT DO NOTHING;

INSERT INTO app.course_invites (
    id, code, course_id, role, created_by, is_active, max_uses, use_count,
    expires_at, created_at, updated_at
)
SELECT id, code, search_space_id::bigint, role, created_by, is_active,
       max_uses, use_count, expires_at, created_at, updated_at
FROM public.course_invite_links
ON CONFLICT DO NOTHING;

INSERT INTO app.documents (
    id, course_id, title, document_type, material_kind, content,
    source_markdown, content_hash, unique_identifier_hash, embedding, metadata,
    page_count, week, status, failure_reason, created_at, updated_at
)
SELECT
    id::bigint,
    search_space_id::bigint,
    title::text,
    document_type::text,
    material_kind::text,
    content,
    source_markdown,
    content_hash::text,
    unique_identifier_hash::text,
    embedding,
    COALESCE(document_metadata, '{}'::jsonb),
    page_count,
    week,
    CASE
        WHEN status ->> 'state' IN ('queued', 'processing', 'ready', 'failed')
            THEN status ->> 'state'
        ELSE 'ready'
    END,
    CASE WHEN status ->> 'state' = 'failed' THEN status ->> 'reason' END,
    created_at,
    updated_at
FROM public.aita_documents
ON CONFLICT DO NOTHING;

INSERT INTO internal.document_chunks (
    id, course_id, document_id, content, embedding, page_number, section_path,
    chunk_type, figure_id, created_at
)
SELECT c.id::bigint, d.search_space_id::bigint, c.document_id::bigint, c.content,
       c.embedding, c.page_number, c.section_path, c.chunk_type::text,
       c.figure_id::text, c.created_at
FROM public.aita_chunks AS c
JOIN public.aita_documents AS d ON d.id = c.document_id
ON CONFLICT DO NOTHING;

INSERT INTO app.uploads (
    id, course_id, document_id, week, kind, title, source_name, page_count,
    is_latest, uploaded_by, metadata, status, storage_key, artifact_manifest,
    ocr_provider, ocr_details, warning_count, error_message, started_at,
    completed_at, attempt_count, uploaded_at, created_at, updated_at
)
SELECT
    id, search_space_id::bigint, doc_id::bigint, week, kind, title, source_name,
    page_count, is_latest, uploaded_by, COALESCE(metadata, '{}'::jsonb), status,
    storage_key, COALESCE(artifact_manifest, '{}'::jsonb), ocr_provider,
    COALESCE(ocr_summary, '{}'::jsonb), warning_count, error_message, started_at,
    completed_at, attempt_count, uploaded_at, created_at, updated_at
FROM public.teacher_uploads
ON CONFLICT DO NOTHING;

INSERT INTO internal.upload_jobs (
    id, course_id, upload_id, state, lease_owner, lease_expires_at,
    attempt_count, last_error, created_at, updated_at
)
SELECT j.id, u.search_space_id::bigint, j.upload_id, j.state, j.lease_owner,
       j.lease_expires_at, j.attempt_count, j.last_error, j.created_at, j.updated_at
FROM public.teacher_upload_jobs AS j
JOIN public.teacher_uploads AS u ON u.id = j.upload_id
ON CONFLICT DO NOTHING;

INSERT INTO pg_temp.db05_copy_quarantine (source_table, source_id, reason, source_row)
SELECT 'chat_sessions', s.id::text, 'missing target course', to_jsonb(s)
FROM public.chat_sessions AS s
LEFT JOIN app.courses AS course ON course.id = s.search_space_id
WHERE course.id IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO app.learning_activities (
    id, modality, external_id, user_id, course_id, metadata, memory_summary,
    status, created_at, updated_at
)
SELECT s.id, 'chat', s.chat_id, s.user_id, s.search_space_id::bigint,
       COALESCE(s.meta, '{}'::jsonb), s.memory_summary, 'active', s.created_at, s.updated_at
FROM public.chat_sessions AS s
JOIN app.courses AS course ON course.id = s.search_space_id
ON CONFLICT DO NOTHING;

INSERT INTO pg_temp.db05_copy_quarantine (source_table, source_id, reason, source_row)
SELECT 'chat_turns', t.id::text, 'missing target chat activity', to_jsonb(t)
FROM public.chat_turns AS t
LEFT JOIN app.learning_activities AS activity
  ON activity.id = t.chat_session_id AND activity.modality = 'chat'
WHERE activity.id IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO app.chat_messages (
    id, course_id, learning_activity_id, kind, turn_index, external_id, role, content,
    model, tool_name, tool_inputs, attachments, citations, keywords, created_at
)
SELECT
    t.id,
    s.search_space_id::bigint,
    t.chat_session_id,
    'message',
    t.turn_index,
    t.turn_id,
    t.role,
    t.content,
    t.model,
    t.tool_name,
    t.tool_inputs,
    COALESCE(t.attachments, '[]'::jsonb),
    COALESCE(t.citations, '[]'::jsonb),
    CASE WHEN jsonb_typeof(t.keywords) = 'array' THEN
        ARRAY(SELECT jsonb_array_elements_text(t.keywords))
    ELSE '{}'::text[] END,
    t.created_at
FROM public.chat_turns AS t
JOIN public.chat_sessions AS s ON s.id = t.chat_session_id
JOIN app.learning_activities AS activity
  ON activity.id = s.id AND activity.modality = 'chat'
ON CONFLICT DO NOTHING;

INSERT INTO internal.chat_session_snippets (
    learning_activity_id, chunk_id, course_id, original_score, first_seen_turn,
    last_used_turn, snippet_payload, created_at
)
SELECT sn.chat_session_id, sn.chunk_id, s.search_space_id::bigint,
       sn.original_score, sn.first_seen_turn, sn.last_used_turn,
       sn.snippet_payload, sn.created_at
FROM public.chat_session_snippets AS sn
JOIN public.chat_sessions AS s ON s.id = sn.chat_session_id
JOIN app.learning_activities AS activity
  ON activity.id = s.id AND activity.modality = 'chat'
ON CONFLICT DO NOTHING;

INSERT INTO internal.chat_routing_decisions (
    id, course_id, learning_activity_id, turn_id, query, stage1_top1, stage1_margin,
    stage1_route, stage2_invoked, stage2_route, stage2_confidence,
    retrieval_mode, retrieval_top_score, final_route, was_clarified,
    clarify_cause, latency_router_ms, latency_retrieval_ms, latency_answer_ms,
    created_at
)
SELECT r.id, s.search_space_id::bigint, r.chat_session_id, r.turn_id, r.query,
       r.stage1_top1, r.stage1_margin, r.stage1_route, r.stage2_invoked,
       r.stage2_route, r.stage2_confidence, r.retrieval_mode,
       r.retrieval_top_score, r.final_route, r.was_clarified, r.clarify_cause,
       r.latency_router_ms, r.latency_retrieval_ms, r.latency_answer_ms, r.created_at
FROM public.chat_router_decisions AS r
JOIN public.chat_sessions AS s ON s.id = r.chat_session_id
JOIN app.learning_activities AS activity
  ON activity.id = s.id AND activity.modality = 'chat'
ON CONFLICT DO NOTHING;

INSERT INTO app.concepts (
    id, course_id, subject_slug, subject_display_name, slug, display_name, description,
    canonical_symbols, symbol_metadata, normalization_map,
    parser_prompt_template, solver_config, parser_config,
    forbidden_named_laws, created_at, updated_at
)
SELECT
    c.id,
    s.search_space_id::bigint,
    s.slug,
    s.display_name,
    c.slug,
    c.display_name,
    c.description,
    CASE
        WHEN jsonb_typeof(c.canonical_symbols) = 'array'
            THEN ARRAY(SELECT jsonb_array_elements_text(c.canonical_symbols))
        WHEN jsonb_typeof(c.canonical_symbols -> 'symbols') = 'array'
            THEN ARRAY(SELECT jsonb_array_elements_text(c.canonical_symbols -> 'symbols'))
        WHEN jsonb_typeof(c.canonical_symbols) = 'object'
            THEN ARRAY(SELECT key FROM jsonb_each(c.canonical_symbols) ORDER BY key)
        ELSE '{}'::text[]
    END,
    CASE WHEN jsonb_typeof(c.canonical_symbols) = 'object'
        THEN c.canonical_symbols - 'symbols' ELSE '{}'::jsonb END,
    COALESCE(c.normalization_map, '{}'::jsonb),
    c.parser_prompt_template,
    COALESCE(c.solver_hints, '{}'::jsonb),
    '{}'::jsonb,
    CASE
        WHEN jsonb_typeof(c.forbidden_named_laws) = 'array'
            THEN ARRAY(SELECT jsonb_array_elements_text(c.forbidden_named_laws))
        WHEN jsonb_typeof(c.forbidden_named_laws) = 'object'
            THEN ARRAY(SELECT key FROM jsonb_each(c.forbidden_named_laws) ORDER BY key)
        ELSE '{}'::text[]
    END,
    c.created_at,
    c.updated_at
FROM public.apollo_concepts AS c
JOIN public.apollo_subjects AS s ON s.id = c.subject_id
ON CONFLICT DO NOTHING;

DO $problem_shape$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.apollo_concept_problems
        WHERE jsonb_typeof(payload) <> 'object'
           OR NULLIF(payload ->> 'problem_text', '') IS NULL
           OR jsonb_typeof(payload -> 'reference_solution') <> 'object'
           OR jsonb_typeof(COALESCE(payload -> 'given_values', '{}'::jsonb)) <> 'object'
    ) THEN
        RAISE EXCEPTION 'problem payload cannot be promoted losslessly';
    END IF;
END
$problem_shape$;

INSERT INTO app.problems (
    id, course_id, concept_id, problem_code, difficulty, problem_text,
    given_values, target_unknown, reference_solution, payload_extra, tier,
    solution_source, provenance, quarantined_at, created_at, updated_at
)
SELECT
    p.id,
    COALESCE(p.search_space_id, s.search_space_id)::bigint,
    p.concept_id,
    p.problem_code,
    p.difficulty,
    p.payload ->> 'problem_text',
    COALESCE(p.payload -> 'given_values', '{}'::jsonb),
    COALESCE(p.payload ->> 'target_unknown', ''),
    p.payload -> 'reference_solution',
    p.payload - ARRAY['problem_text', 'given_values', 'target_unknown', 'reference_solution'],
    p.tier,
    p.solution_source,
    COALESCE(p.provenance, '{}'::jsonb),
    p.quarantined_at,
    p.created_at,
    p.created_at
FROM public.apollo_concept_problems AS p
JOIN public.apollo_concepts AS c ON c.id = p.concept_id
JOIN public.apollo_subjects AS s ON s.id = c.subject_id
ON CONFLICT DO NOTHING;

INSERT INTO pg_temp.db05_copy_quarantine (source_table, source_id, reason, source_row)
SELECT 'apollo_sessions', s.id::text, 'missing course, concept, or current problem', to_jsonb(s)
FROM public.apollo_sessions AS s
LEFT JOIN app.courses AS course ON course.id = s.search_space_id
LEFT JOIN app.concepts AS concept ON concept.id = s.concept_id
LEFT JOIN app.problems AS problem
  ON problem.course_id = s.search_space_id
 AND problem.problem_code = s.current_problem_id
 AND (s.concept_id IS NULL OR problem.concept_id = s.concept_id)
WHERE course.id IS NULL
   OR (s.concept_id IS NOT NULL AND concept.id IS NULL)
   OR (s.current_problem_id IS NOT NULL AND problem.id IS NULL)
ON CONFLICT DO NOTHING;

INSERT INTO pg_temp.db05_copy_quarantine (source_table, source_id, reason, source_row)
SELECT 'apollo_problem_attempts', a.id::text, 'unresolved parent activity or problem', to_jsonb(a)
FROM public.apollo_problem_attempts AS a
JOIN public.apollo_sessions AS s ON s.id = a.session_id
LEFT JOIN app.problems AS p
  ON p.course_id = s.search_space_id
 AND p.problem_code = a.problem_id
 AND (s.concept_id IS NULL OR p.concept_id = s.concept_id)
LEFT JOIN pg_temp.db05_copy_quarantine AS quarantined_session
  ON quarantined_session.source_table = 'apollo_sessions'
 AND quarantined_session.source_id = s.id::text
WHERE p.id IS NULL OR quarantined_session.source_id IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO app.learning_activities (
    id, modality, external_id, user_id, course_id, concept_id, status, phase, current_problem_id,
    pending_intent, history_summary, history_summary_up_to_turn, created_at,
    updated_at, last_touched_at
)
SELECT
    s.id + 1000000, 'tutoring', NULL, s.user_id, s.search_space_id::bigint,
    s.concept_id, s.status, s.phase,
    p.id, s.pending_intent, s.history_summary, s.history_summary_up_to_turn,
    s.created_at, s.last_touched_at, s.last_touched_at
FROM public.apollo_sessions AS s
LEFT JOIN app.problems AS p
  ON p.course_id = s.search_space_id
 AND p.problem_code = s.current_problem_id
 AND (s.concept_id IS NULL OR p.concept_id = s.concept_id)
LEFT JOIN pg_temp.db05_copy_quarantine AS quarantined
  ON quarantined.source_table = 'apollo_sessions'
 AND quarantined.source_id = s.id::text
WHERE quarantined.source_id IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO app.problem_attempts (
    id, course_id, user_id, learning_activity_id, problem_id, difficulty, result,
    solver_trace, diagnostic_report, learner_update_pending,
    learner_update_attempts, learner_update_failed_at, learner_update_last_error,
    learner_update_next_attempt_at, created_at, updated_at
)
SELECT
    a.id, s.search_space_id::bigint, s.user_id, a.session_id + 1000000, p.id,
    a.difficulty, a.result, a.solver_trace, a.diagnostic_report,
    a.learner_update_pending, a.learner_update_attempts,
    a.learner_update_failed_at, a.learner_update_last_error,
    a.learner_update_next_attempt_at, a.created_at, a.created_at
FROM public.apollo_problem_attempts AS a
JOIN public.apollo_sessions AS s ON s.id = a.session_id
JOIN app.problems AS p
  ON p.course_id = s.search_space_id
 AND p.problem_code = a.problem_id
 AND (s.concept_id IS NULL OR p.concept_id = s.concept_id)
JOIN app.learning_activities AS activity
  ON activity.id = s.id + 1000000 AND activity.modality = 'tutoring'
ON CONFLICT DO NOTHING;

INSERT INTO app.tutoring_messages (
    id, course_id, learning_activity_id, attempt_id, role, content, turn_index,
    low_confidence_pattern, intent, metadata, created_at
)
SELECT
    m.id,
    s.search_space_id::bigint,
    m.session_id + 1000000,
    m.attempt_id,
    m.role,
    m.content,
    m.turn_index,
    CASE WHEN jsonb_typeof(m.metadata -> 'low_conf_pattern') = 'boolean'
        THEN (m.metadata ->> 'low_conf_pattern')::boolean ELSE false END,
    CASE WHEN jsonb_typeof(m.metadata -> 'intent') = 'string'
        THEN m.metadata ->> 'intent' END,
    COALESCE(m.metadata, '{}'::jsonb)
        - ARRAY['low_conf_pattern', 'intent', 'misconception', 'misconceptions', 'clarification_trace'],
    m.created_at
FROM public.apollo_messages AS m
JOIN public.apollo_sessions AS s ON s.id = m.session_id
JOIN app.learning_activities AS activity
  ON activity.id = s.id + 1000000 AND activity.modality = 'tutoring'
ON CONFLICT DO NOTHING;

INSERT INTO pg_temp.db05_copy_quarantine (source_table, source_id, reason, source_row)
SELECT 'apollo_messages', m.id::text, 'missing target tutoring activity', to_jsonb(m)
FROM public.apollo_messages AS m
LEFT JOIN app.learning_activities AS activity
  ON activity.id = m.session_id + 1000000 AND activity.modality = 'tutoring'
WHERE activity.id IS NULL
ON CONFLICT DO NOTHING;

-- A progress row must resolve to exactly one course from session or membership
-- evidence. Multiple candidates are a product decision and abort the copy.
DO $progress_attribution$
BEGIN
    IF EXISTS (
        WITH candidates AS (
            SELECT p.user_id, s.search_space_id::bigint AS course_id
            FROM public.apollo_student_progress AS p
            JOIN public.apollo_sessions AS s ON s.user_id = p.user_id
            UNION
            SELECT p.user_id, m.search_space_id::bigint
            FROM public.apollo_student_progress AS p
            JOIN public.course_memberships AS m ON m.user_id = p.user_id
        )
        SELECT p.user_id
        FROM public.apollo_student_progress AS p
        LEFT JOIN candidates AS c ON c.user_id = p.user_id
        GROUP BY p.user_id
        HAVING count(DISTINCT c.course_id) <> 1
    ) THEN
        RAISE EXCEPTION 'student progress course attribution is missing or ambiguous';
    END IF;
END
$progress_attribution$;

INSERT INTO app.student_progress (
    user_id, course_id, xp_total, level, last_level_up_at, created_at, updated_at
)
WITH candidates AS (
    SELECT p.user_id, s.search_space_id::bigint AS course_id
    FROM public.apollo_student_progress AS p
    JOIN public.apollo_sessions AS s ON s.user_id = p.user_id
    UNION
    SELECT p.user_id, m.search_space_id::bigint
    FROM public.apollo_student_progress AS p
    JOIN public.course_memberships AS m ON m.user_id = p.user_id
), attributed AS (
    SELECT user_id, min(course_id) AS course_id
    FROM candidates GROUP BY user_id
)
SELECT p.user_id, a.course_id, p.xp_total, p.level, p.last_level_up_at,
       COALESCE(p.last_level_up_at, now()), COALESCE(p.last_level_up_at, now())
FROM public.apollo_student_progress AS p
JOIN attributed AS a ON a.user_id = p.user_id
ON CONFLICT DO NOTHING;

INSERT INTO app.provisioning_runs (
    id, course_id, kind, set_index, problem_document_id, solution_document_id,
    status, result_summary, created_at, updated_at
)
SELECT authored.id, authored.search_space_id::bigint, 'authored_set', authored.set_index,
       authored.problem_document_id, authored.solution_document_id, authored.status,
       authored.result_summary, authored.created_at, authored.updated_at
FROM public.apollo_authored_sets AS authored
ON CONFLICT DO NOTHING;

-- Opportunity rows own the merged identity. Tally-only rows receive a target
-- identity while retaining their shared business key and are reconciled below.
INSERT INTO app.question_opportunities (
    id, course_id, learning_activity_id, attempt_id, reference_node_id, state, question,
    asked_turn, answered_turn, evidence, student_declined, times_asked,
    last_asked_turn, created_at, updated_at
)
SELECT
    o.id, s.search_space_id::bigint, o.session_id + 1000000, o.attempt_id,
    o.reference_node_id, COALESCE(t.status, o.state), o.question, o.asked_turn,
    o.answered_turn, COALESCE(t.evidence, '[]'::jsonb),
    COALESCE(t.student_declined, false), COALESCE(t.times_asked, 0),
    t.last_asked_turn, o.created_at, COALESCE(t.updated_at, o.created_at)
FROM public.apollo_reference_question_opportunities AS o
JOIN public.apollo_problem_attempts AS a ON a.id = o.attempt_id
JOIN public.apollo_sessions AS s ON s.id = a.session_id
JOIN app.learning_activities AS activity
  ON activity.id = s.id + 1000000 AND activity.modality = 'tutoring'
LEFT JOIN public.apollo_question_tally AS t
  ON t.attempt_id = o.attempt_id AND t.reference_node_id = o.reference_node_id
ON CONFLICT DO NOTHING;

INSERT INTO app.question_opportunities (
    course_id, learning_activity_id, attempt_id, reference_node_id, state, question,
    asked_turn, answered_turn, evidence, student_declined, times_asked,
    last_asked_turn, created_at, updated_at
)
SELECT
    s.search_space_id::bigint, a.session_id + 1000000, t.attempt_id, t.reference_node_id,
    t.status, '', NULL, NULL, t.evidence, t.student_declined, t.times_asked,
    t.last_asked_turn, t.updated_at, t.updated_at
FROM public.apollo_question_tally AS t
JOIN public.apollo_problem_attempts AS a ON a.id = t.attempt_id
JOIN public.apollo_sessions AS s ON s.id = a.session_id
JOIN app.learning_activities AS activity
  ON activity.id = s.id + 1000000 AND activity.modality = 'tutoring'
LEFT JOIN public.apollo_reference_question_opportunities AS o
  ON o.attempt_id = t.attempt_id AND o.reference_node_id = t.reference_node_id
WHERE o.id IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO app.learner_entities (
    id, course_id, concept_id, canonical_key, kind, display_name, payload,
    aliases, scope_summary, created_at, updated_at
)
SELECT
    e.id, s.search_space_id::bigint, e.concept_id, e.canonical_key, e.kind,
    e.display_name, e.payload,
    CASE WHEN jsonb_typeof(e.aliases) = 'array'
        THEN ARRAY(SELECT jsonb_array_elements_text(e.aliases)) ELSE '{}'::text[] END,
    e.scope_summary, e.created_at, e.updated_at
FROM public.apollo_kg_entities AS e
JOIN public.apollo_concepts AS c ON c.id = e.concept_id
JOIN public.apollo_subjects AS s ON s.id = c.subject_id
WHERE e.kind <> 'misconception'
ON CONFLICT DO NOTHING;

INSERT INTO internal.entity_prerequisites (
    course_id, from_entity_id, to_entity_id, created_at
)
SELECT f.course_id, p.from_entity_id, p.to_entity_id, now()
FROM public.apollo_entity_prereqs AS p
JOIN app.learner_entities AS f ON f.id = p.from_entity_id
JOIN app.learner_entities AS t ON t.id = p.to_entity_id AND t.course_id = f.course_id
ON CONFLICT DO NOTHING;

INSERT INTO app.learner_state (
    user_id, course_id, entity_id, belief, mastery, confidence, evidence_count,
    last_evidence_at, created_at, updated_at
)
SELECT ls.user_id, ls.search_space_id::bigint, ls.entity_id, ls.belief, ls.mastery,
       ls.confidence, ls.evidence_count, ls.last_evidence_at,
       COALESCE(ls.last_evidence_at, ls.updated_at), ls.updated_at
FROM public.apollo_learner_state AS ls
JOIN app.learner_entities AS copied_entity
  ON copied_entity.id = ls.entity_id
ON CONFLICT DO NOTHING;

INSERT INTO app.mastery_events (
    id, user_id, course_id, entity_id, attempt_id, concept_problem_id,
    event_kind, score, parser_confidence, grader_confidence, negotiation_move,
    reference_step_id, prior_belief, posterior_belief, mastery_after,
    dt_days_since_last, evidence_node_ids, created_at
)
SELECT
    me.id, me.user_id, me.search_space_id::bigint, me.entity_id, me.attempt_id,
    me.concept_problem_id, me.event_kind, me.score, me.parser_confidence,
    me.grader_confidence, me.negotiation_move, me.reference_step_id,
    me.prior_belief, me.posterior_belief, me.mastery_after, me.dt_days_since_last,
    CASE WHEN jsonb_typeof(me.evidence_node_ids) = 'array'
        THEN ARRAY(SELECT jsonb_array_elements_text(me.evidence_node_ids))
        ELSE '{}'::text[] END,
    me.created_at
FROM public.apollo_mastery_events AS me
JOIN app.learner_entities AS copied_entity
  ON copied_entity.id = me.entity_id
ON CONFLICT DO NOTHING;

INSERT INTO internal.content_ingest_runs (
    id, course_id, document_id, content_hash, status, n_questions_scraped,
    n_promoted, n_rejected, n_dedup_merged, n_pages, llm_calls, llm_tokens_in,
    llm_tokens_out, llm_cost_usd, dedup_total_candidates, dedup_exact_merges,
    dedup_embedding_merges, dedup_embedding_distinct,
    dedup_embedding_merge_ratio, dedup_details, started_at, finished_at, created_at
)
SELECT
    id, search_space_id::bigint, document_id, content_hash, status,
    n_questions_scraped, n_promoted, n_rejected, n_dedup_merged, n_pages,
    llm_calls, llm_tokens_in, llm_tokens_out, llm_cost_usd,
    CASE WHEN jsonb_typeof(dedup_pressure -> 'total_candidates') = 'number'
        THEN (dedup_pressure ->> 'total_candidates')::integer ELSE 0 END,
    CASE WHEN jsonb_typeof(dedup_pressure -> 'exact_merges') = 'number'
        THEN (dedup_pressure ->> 'exact_merges')::integer ELSE 0 END,
    CASE WHEN jsonb_typeof(dedup_pressure -> 'embedding_merges') = 'number'
        THEN (dedup_pressure ->> 'embedding_merges')::integer ELSE 0 END,
    CASE WHEN jsonb_typeof(dedup_pressure -> 'embedding_distinct') = 'number'
        THEN (dedup_pressure ->> 'embedding_distinct')::integer ELSE 0 END,
    CASE WHEN jsonb_typeof(dedup_pressure -> 'embedding_merge_ratio') = 'number'
        THEN (dedup_pressure ->> 'embedding_merge_ratio')::real ELSE 0 END,
    CASE WHEN jsonb_typeof(dedup_pressure -> 'per_concept') = 'object'
        THEN dedup_pressure -> 'per_concept' ELSE '{}'::jsonb END,
    started_at, finished_at, created_at
FROM public.apollo_ingest_runs
ON CONFLICT DO NOTHING;

INSERT INTO app.provisioning_runs (
    id, course_id, kind, concept_id, status, result_summary, ingest_run_id,
    created_at, updated_at
)
-- The second union leg uses the same documented +1000000 collision boundary
-- so both independent legacy identity sequences remain reversible.
SELECT generation.id + 1000000, generation.search_space_id::bigint, 'generation',
       generation.concept_id, generation.status, generation.result_summary,
       generation.ingest_run_id, generation.created_at, generation.created_at
FROM public.apollo_generation_runs AS generation
ON CONFLICT DO NOTHING;

INSERT INTO internal.content_ingest_errors (
    id, course_id, ingest_run_id, stage, error_class, context, created_at
)
SELECT id, search_space_id::bigint, ingest_run_id, stage, error_class, context, created_at
FROM public.apollo_ingest_errors
ON CONFLICT DO NOTHING;

INSERT INTO internal.ingest_page_evidence (
    id, course_id, ingest_run_id, document_id, role, page_number, ocr_text,
    ocr_confidence, extraction_mode, verify_path_fired, created_at
)
SELECT id, search_space_id::bigint, ingest_run_id, document_id, role,
       page_number, ocr_text, ocr_confidence, extraction_mode,
       verify_path_fired, created_at
FROM public.apollo_ingest_page_evidence
ON CONFLICT DO NOTHING;

INSERT INTO internal.dedup_decisions (
    id, course_id, ingest_run_id, concept_id, matched_entity_id,
    candidate_key, method, similarity, verdict, created_at
)
SELECT d.id, d.search_space_id::bigint, d.ingest_run_id, d.concept_id,
       copied_entity.id, d.candidate_key, d.method, d.similarity, d.verdict, d.created_at
FROM public.apollo_dedup_decisions AS d
LEFT JOIN app.learner_entities AS copied_entity ON copied_entity.id = d.matched_entity_id
ON CONFLICT DO NOTHING;

-- Preserve the full legacy artifact payload for the first release while
-- promoting stable version and score fields to typed columns.
INSERT INTO internal.grading_runs (
    id, course_id, user_id, attempt_id, concept_id, problem_id, role,
    grader_used, grader_version, reference_graph_hash, weights_version,
    status, grading_latency_ms, composite_score, node_coverage_score,
    edge_coverage_score, abstained, version_details,
    node_ledger, edge_ledger, score_details, abstention_details,
    grader_payload, created_at
)
SELECT
    g.id, g.search_space_id, g.user_id, g.attempt_id, g.concept_id, p.id,
    g.role, g.grader_used, COALESCE(g.versions ->> 'grader', 'legacy'),
    g.versions ->> 'reference_graph_hash', g.versions ->> 'weights_version',
    'final', g.grading_latency_ms,
    CASE WHEN jsonb_typeof(g.scores -> 'composite') = 'number'
        THEN (g.scores ->> 'composite')::real END,
    CASE WHEN jsonb_typeof(g.scores -> 'node_coverage') = 'number'
        THEN (g.scores ->> 'node_coverage')::real END,
    CASE WHEN jsonb_typeof(g.scores -> 'edge_coverage') = 'number'
        THEN (g.scores ->> 'edge_coverage')::real END,
    g.abstention IS NOT NULL,
    g.versions,
    g.node_ledger,
    g.edge_ledger,
    g.scores,
    g.abstention,
    jsonb_build_object('source', 'apollo_grading_artifacts', 'source_id', g.id),
    g.created_at
FROM public.apollo_grading_artifacts AS g
JOIN app.problems AS p
  ON p.course_id = g.search_space_id
 AND p.problem_code = g.problem_id
 AND (g.concept_id IS NULL OR p.concept_id = g.concept_id)
ON CONFLICT DO NOTHING;

INSERT INTO pg_temp.db05_copy_quarantine (source_table, source_id, reason, source_row)
SELECT 'ai_use_reports', r.id::text, 'missing target chat activity', to_jsonb(r)
FROM public.ai_use_reports AS r
LEFT JOIN app.learning_activities AS s
  ON s.external_id = r.chat_id AND s.modality = 'chat'
WHERE s.id IS NULL
ON CONFLICT DO NOTHING;

INSERT INTO app.ai_usage_reports (
    id, user_id, course_id, chat_id, style, length, markdown, jsonld,
    model_fingerprint, tool_calls, prompt_hashes, created_at
)
SELECT
    r.id, s.user_id, s.course_id, r.chat_id, r.style, r.length, r.markdown,
    r.jsonld, r.model_fingerprint, r.tool_calls,
    CASE WHEN jsonb_typeof(r.prompt_hashes) = 'array'
        THEN ARRAY(SELECT jsonb_array_elements_text(r.prompt_hashes))
        ELSE '{}'::text[] END,
    r.created_at
FROM public.ai_use_reports AS r
JOIN app.learning_activities AS s ON s.external_id = r.chat_id AND s.modality = 'chat'
ON CONFLICT DO NOTHING;

-- Advance every identity sequence in app/internal, including target-only ids
-- allocated for remapped tutoring activities and generation runs.
DO $advance_sequences$
DECLARE
    identity_column record;
    sequence_name text;
    maximum_id bigint;
BEGIN
    FOR identity_column IN
        SELECT table_schema, table_name, column_name
        FROM information_schema.columns
        WHERE table_schema IN ('app', 'internal')
          AND is_identity = 'YES'
    LOOP
        sequence_name := pg_get_serial_sequence(
            format('%I.%I', identity_column.table_schema, identity_column.table_name),
            identity_column.column_name
        );
        EXECUTE format(
            'SELECT max(%I)::bigint FROM %I.%I',
            identity_column.column_name,
            identity_column.table_schema,
            identity_column.table_name
        ) INTO maximum_id;
        IF maximum_id IS NULL THEN
            PERFORM setval(sequence_name::regclass, 1, false);
        ELSE
            PERFORM setval(sequence_name::regclass, maximum_id, true);
        END IF;
    END LOOP;
END
$advance_sequences$;

COMMIT;
