-- DB-05 HUMAN-RUN reverse-delta artifact. LOCAL rehearsal by agents only.
--
-- Use only after pausing user and worker writes. The operator must set a
-- transaction/session setting to the instant the forward copy completed:
--
--   BEGIN;
--   SET LOCAL db05.rollback_watermark = '2026-07-17T04:30:00Z';
--   \ir scripts/db/rollback_reverse_copy.sql
--   COMMIT;
--   \ir scripts/db/reconcile_copy.sql
--
-- This script never deletes target rows. It upserts target rows created or
-- updated at/after the watermark into legacy public tables, then validates
-- representative checksums. Full reconcile_copy.sql is a mandatory final gate.

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET CONSTRAINTS ALL IMMEDIATE;

DO $watermark_required$
BEGIN
    IF current_setting('db05.rollback_watermark', true) IS NULL THEN
        RAISE EXCEPTION 'set db05.rollback_watermark before reverse copy';
    END IF;
    PERFORM current_setting('db05.rollback_watermark')::timestamptz;
END
$watermark_required$;

CREATE TEMP TABLE _db05_reverse_context ON COMMIT DROP AS
SELECT current_setting('db05.rollback_watermark')::timestamptz AS watermark;

INSERT INTO public.aita_search_spaces (
    id, name, slug, subject_name, weight_overrides, metadata, created_at, updated_at
)
SELECT id::integer, name, slug, subject_name, retrieval_weights, metadata,
       created_at, updated_at
FROM app.courses, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (id) DO UPDATE SET
    name=EXCLUDED.name, slug=EXCLUDED.slug, subject_name=EXCLUDED.subject_name,
    weight_overrides=EXCLUDED.weight_overrides, metadata=EXCLUDED.metadata,
    updated_at=EXCLUDED.updated_at;

INSERT INTO public.teacher_courses (
    search_space_id, current_week, weights, weight_bounds, created_at, updated_at
)
SELECT id::integer, current_week, retrieval_weights,
       jsonb_build_object('min',retrieval_weight_min,'max',retrieval_weight_max),
       created_at, updated_at
FROM app.courses, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (search_space_id) DO UPDATE SET
    current_week=EXCLUDED.current_week, weights=EXCLUDED.weights,
    weight_bounds=EXCLUDED.weight_bounds, updated_at=EXCLUDED.updated_at;

INSERT INTO public.course_memberships (
    user_id, search_space_id, role, created_at, updated_at
)
SELECT user_id, course_id::integer, role, created_at, updated_at
FROM app.course_memberships, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (user_id,search_space_id) DO UPDATE SET
    role=EXCLUDED.role, updated_at=EXCLUDED.updated_at;

INSERT INTO public.course_invite_links (
    id, code, search_space_id, role, created_by, is_active, max_uses, use_count,
    expires_at, created_at, updated_at
)
SELECT id, code, course_id::integer, role, created_by, is_active, max_uses,
       use_count, expires_at, created_at, updated_at
FROM app.course_invites, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (id) DO UPDATE SET
    code=EXCLUDED.code, role=EXCLUDED.role, is_active=EXCLUDED.is_active,
    max_uses=EXCLUDED.max_uses, use_count=EXCLUDED.use_count,
    expires_at=EXCLUDED.expires_at, updated_at=EXCLUDED.updated_at;

INSERT INTO public.aita_documents (
    id, title, document_type, material_kind, content, source_markdown,
    content_hash, unique_identifier_hash, embedding, document_metadata,
    page_count, week, status, search_space_id, created_at, updated_at
)
SELECT id::integer, title, document_type, material_kind, content, source_markdown,
       content_hash, unique_identifier_hash, embedding, metadata, page_count, week,
       jsonb_strip_nulls(jsonb_build_object('state',status,'reason',failure_reason)),
       course_id::integer, created_at, updated_at
FROM app.documents, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (id) DO UPDATE SET
    title=EXCLUDED.title, content=EXCLUDED.content, source_markdown=EXCLUDED.source_markdown,
    document_metadata=EXCLUDED.document_metadata, page_count=EXCLUDED.page_count,
    week=EXCLUDED.week, status=EXCLUDED.status, updated_at=EXCLUDED.updated_at;

INSERT INTO public.aita_chunks (
    id, content, embedding, page_number, section_path, chunk_type,
    figure_id, document_id, created_at
)
SELECT id::integer, content, embedding, page_number, section_path, chunk_type,
       figure_id, document_id::integer, created_at
FROM internal.document_chunks, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.teacher_uploads (
    id, search_space_id, week, kind, title, source_name, doc_id, page_count,
    is_latest, uploaded_by, metadata, uploaded_at, created_at, updated_at,
    status, storage_key, artifact_manifest, ocr_provider, ocr_summary,
    warning_count, error_message, started_at, completed_at, attempt_count
)
SELECT id, course_id::integer, week, kind, title, source_name, document_id::integer,
       page_count, is_latest, uploaded_by, metadata, uploaded_at, created_at,
       updated_at, status, storage_key, artifact_manifest, ocr_provider,
       ocr_details, warning_count, error_message, started_at, completed_at,
       attempt_count
FROM app.uploads, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (id) DO UPDATE SET
    is_latest=EXCLUDED.is_latest, metadata=EXCLUDED.metadata, status=EXCLUDED.status,
    artifact_manifest=EXCLUDED.artifact_manifest, ocr_summary=EXCLUDED.ocr_summary,
    warning_count=EXCLUDED.warning_count, error_message=EXCLUDED.error_message,
    completed_at=EXCLUDED.completed_at, attempt_count=EXCLUDED.attempt_count,
    updated_at=EXCLUDED.updated_at;

INSERT INTO public.teacher_upload_jobs (
    id, upload_id, state, lease_owner, lease_expires_at, attempt_count,
    last_error, created_at, updated_at
)
SELECT id, upload_id, state, lease_owner, lease_expires_at, attempt_count,
       last_error, created_at, updated_at
FROM internal.upload_jobs, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (id) DO UPDATE SET
    state=EXCLUDED.state, lease_owner=EXCLUDED.lease_owner,
    lease_expires_at=EXCLUDED.lease_expires_at, attempt_count=EXCLUDED.attempt_count,
    last_error=EXCLUDED.last_error, updated_at=EXCLUDED.updated_at;

INSERT INTO public.chat_sessions (
    id, chat_id, user_id, search_space_id, meta, memory_summary, created_at, updated_at
)
SELECT id, external_id, user_id, course_id::integer, metadata, memory_summary,
       created_at, updated_at
FROM app.learning_activities, _db05_reverse_context
WHERE modality='chat' AND (created_at >= watermark OR updated_at >= watermark)
ON CONFLICT (id) DO UPDATE SET
    meta=EXCLUDED.meta, memory_summary=EXCLUDED.memory_summary,
    updated_at=EXCLUDED.updated_at;

INSERT INTO public.chat_turns (
    id, chat_session_id, turn_index, turn_id, role, content, created_at, model,
    tool_name, tool_inputs, attachments, citations, keywords
)
SELECT id, learning_activity_id, turn_index, external_id, role, content, created_at,
       model, tool_name, tool_inputs, attachments, citations, to_jsonb(keywords)
FROM app.chat_messages, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.chat_session_snippets (
    chat_session_id, chunk_id, original_score, first_seen_turn, last_used_turn,
    snippet_payload, created_at
)
SELECT learning_activity_id, chunk_id, original_score, first_seen_turn, last_used_turn,
       snippet_payload, created_at
FROM internal.chat_session_snippets, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (chat_session_id,chunk_id) DO UPDATE SET
    original_score=EXCLUDED.original_score, last_used_turn=EXCLUDED.last_used_turn,
    snippet_payload=EXCLUDED.snippet_payload;

INSERT INTO public.chat_router_decisions (
    id, turn_id, chat_session_id, query, stage1_top1, stage1_margin,
    stage1_route, stage2_invoked, stage2_route, stage2_confidence,
    retrieval_mode, retrieval_top_score, final_route, was_clarified,
    clarify_cause, latency_router_ms, latency_retrieval_ms, latency_answer_ms,
    created_at
) OVERRIDING SYSTEM VALUE
SELECT id, turn_id, learning_activity_id, query, stage1_top1, stage1_margin,
       stage1_route, stage2_invoked, stage2_route, stage2_confidence,
       retrieval_mode, retrieval_top_score, final_route, was_clarified,
       clarify_cause, latency_router_ms, latency_retrieval_ms,
       latency_answer_ms, created_at
FROM internal.chat_routing_decisions, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO NOTHING;

DO $legacy_subject_slug_guard$
BEGIN
    IF EXISTS (
        SELECT subject_slug
        FROM app.concepts
        GROUP BY subject_slug
        HAVING count(DISTINCT course_id) > 1
    ) THEN
        RAISE EXCEPTION 'cannot reverse course-scoped duplicate subject slugs into legacy global-unique apollo_subjects';
    END IF;
END
$legacy_subject_slug_guard$;

INSERT INTO public.apollo_subjects (slug, display_name, created_at, search_space_id)
SELECT DISTINCT ON (subject_slug)
       subject_slug, subject_display_name, created_at, course_id::integer
FROM app.concepts, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ORDER BY subject_slug, updated_at DESC
ON CONFLICT (slug) DO UPDATE SET
    display_name=EXCLUDED.display_name, search_space_id=EXCLUDED.search_space_id;

INSERT INTO public.apollo_concepts (
    id, subject_id, slug, display_name, canonical_symbols, normalization_map,
    parser_prompt_template, solver_hints, forbidden_named_laws, concept_dag,
    created_at, updated_at, description
) OVERRIDING SYSTEM VALUE
SELECT c.id, s.id, c.slug, c.display_name,
       c.symbol_metadata || jsonb_build_object('symbols',to_jsonb(c.canonical_symbols)),
       c.normalization_map, c.parser_prompt_template, c.solver_config,
       to_jsonb(c.forbidden_named_laws), '{}'::jsonb, c.created_at, c.updated_at,
       c.description
FROM app.concepts c
JOIN public.apollo_subjects s
  ON s.slug=c.subject_slug AND s.search_space_id=c.course_id
CROSS JOIN _db05_reverse_context ctx
WHERE c.created_at >= ctx.watermark OR c.updated_at >= ctx.watermark
ON CONFLICT (id) DO UPDATE SET
    display_name=EXCLUDED.display_name, canonical_symbols=EXCLUDED.canonical_symbols,
    normalization_map=EXCLUDED.normalization_map, solver_hints=EXCLUDED.solver_hints,
    forbidden_named_laws=EXCLUDED.forbidden_named_laws,
    updated_at=EXCLUDED.updated_at, description=EXCLUDED.description;

INSERT INTO public.apollo_concept_problems (
    id, concept_id, problem_code, difficulty, payload, created_at, tier,
    solution_source, provenance, quarantined_at, search_space_id
) OVERRIDING SYSTEM VALUE
SELECT id, concept_id, problem_code, difficulty,
       payload_extra || jsonb_build_object(
           'problem_text',problem_text, 'given_values',given_values,
           'target_unknown',target_unknown, 'reference_solution',reference_solution),
       created_at, tier, solution_source, provenance, quarantined_at,
       course_id::integer
FROM app.problems, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (id) DO UPDATE SET
    difficulty=EXCLUDED.difficulty, payload=EXCLUDED.payload, tier=EXCLUDED.tier,
    solution_source=EXCLUDED.solution_source, provenance=EXCLUDED.provenance,
    quarantined_at=EXCLUDED.quarantined_at;

INSERT INTO public.apollo_sessions (
    id, status, phase, current_problem_id, created_at, last_touched_at,
    pending_intent, history_summary, history_summary_up_to_turn, concept_id,
    user_id, search_space_id
)
SELECT s.id-1000000, s.status, s.phase, p.problem_code, s.created_at, s.last_touched_at,
       s.pending_intent, s.history_summary, s.history_summary_up_to_turn,
       s.concept_id, s.user_id, s.course_id::integer
FROM app.learning_activities s
LEFT JOIN app.problems p ON p.id=s.current_problem_id
CROSS JOIN _db05_reverse_context ctx
WHERE s.modality='tutoring'
  AND (s.created_at >= ctx.watermark OR s.updated_at >= ctx.watermark)
ON CONFLICT (id) DO UPDATE SET
    status=EXCLUDED.status, phase=EXCLUDED.phase,
    current_problem_id=EXCLUDED.current_problem_id,
    last_touched_at=EXCLUDED.last_touched_at, pending_intent=EXCLUDED.pending_intent,
    history_summary=EXCLUDED.history_summary,
    history_summary_up_to_turn=EXCLUDED.history_summary_up_to_turn;

INSERT INTO public.apollo_problem_attempts (
    id, session_id, problem_id, difficulty, result, solver_trace,
    diagnostic_report, created_at, learner_update_pending,
    learner_update_attempts, learner_update_failed_at, learner_update_last_error,
    learner_update_next_attempt_at
)
SELECT a.id, a.learning_activity_id-1000000, p.problem_code, a.difficulty, a.result, a.solver_trace,
       a.diagnostic_report, a.created_at, a.learner_update_pending,
       a.learner_update_attempts, a.learner_update_failed_at,
       a.learner_update_last_error, a.learner_update_next_attempt_at
FROM app.problem_attempts a JOIN app.problems p ON p.id=a.problem_id
CROSS JOIN _db05_reverse_context ctx
WHERE a.created_at >= ctx.watermark OR a.updated_at >= ctx.watermark
ON CONFLICT (id) DO UPDATE SET
    result=EXCLUDED.result, solver_trace=EXCLUDED.solver_trace,
    diagnostic_report=EXCLUDED.diagnostic_report,
    learner_update_pending=EXCLUDED.learner_update_pending,
    learner_update_attempts=EXCLUDED.learner_update_attempts,
    learner_update_failed_at=EXCLUDED.learner_update_failed_at,
    learner_update_last_error=EXCLUDED.learner_update_last_error,
    learner_update_next_attempt_at=EXCLUDED.learner_update_next_attempt_at;

INSERT INTO public.apollo_messages (
    id, session_id, role, content, turn_index, created_at, attempt_id, metadata
)
SELECT id, learning_activity_id-1000000, role, content, turn_index, created_at, attempt_id,
       metadata || jsonb_strip_nulls(jsonb_build_object(
           'low_conf_pattern',low_confidence_pattern,'intent',intent))
FROM app.tutoring_messages, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO NOTHING;

DO $global_progress_guard$
BEGIN
    IF EXISTS (
        SELECT user_id FROM app.student_progress, _db05_reverse_context
        WHERE created_at >= watermark OR updated_at >= watermark
        GROUP BY user_id HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION 'cannot reverse multiple per-course progress rows into one legacy global row';
    END IF;
END
$global_progress_guard$;

INSERT INTO public.apollo_student_progress (user_id, xp_total, level, last_level_up_at)
SELECT user_id, xp_total, level, last_level_up_at
FROM app.student_progress, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (user_id) DO UPDATE SET
    xp_total=EXCLUDED.xp_total, level=EXCLUDED.level,
    last_level_up_at=EXCLUDED.last_level_up_at;

INSERT INTO public.apollo_authored_sets (
    id, search_space_id, set_index, problem_document_id, solution_document_id,
    status, result_summary, created_at, updated_at
)
SELECT run.id, run.course_id::integer, run.set_index, run.problem_document_id,
       run.solution_document_id, run.status, run.result_summary, run.created_at,
       run.updated_at
FROM app.provisioning_runs AS run CROSS JOIN _db05_reverse_context AS ctx
WHERE run.kind='authored_set'
  AND (run.created_at >= ctx.watermark OR run.updated_at >= ctx.watermark)
ON CONFLICT (id) DO UPDATE SET
    status=EXCLUDED.status, result_summary=EXCLUDED.result_summary,
    updated_at=EXCLUDED.updated_at;

INSERT INTO public.apollo_reference_question_opportunities (
    id, attempt_id, session_id, reference_node_id, state, question,
    asked_turn, answered_turn, created_at
)
SELECT id, attempt_id, learning_activity_id-1000000, reference_node_id,
       CASE WHEN state='answered' THEN 'answered' ELSE 'asked_waiting' END,
       question, COALESCE(asked_turn,0), answered_turn, created_at
FROM app.question_opportunities, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (attempt_id,reference_node_id) DO UPDATE SET
    state=EXCLUDED.state, question=EXCLUDED.question,
    answered_turn=EXCLUDED.answered_turn;

INSERT INTO public.apollo_question_tally (
    attempt_id, reference_node_id, status, evidence, student_declined,
    times_asked, last_asked_turn, updated_at
)
SELECT attempt_id, reference_node_id, state, evidence, student_declined,
       times_asked, last_asked_turn, updated_at
FROM app.question_opportunities, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (attempt_id,reference_node_id) DO UPDATE SET
    status=EXCLUDED.status, evidence=EXCLUDED.evidence,
    student_declined=EXCLUDED.student_declined, times_asked=EXCLUDED.times_asked,
    last_asked_turn=EXCLUDED.last_asked_turn, updated_at=EXCLUDED.updated_at;

INSERT INTO public.apollo_kg_entities (
    id, concept_id, canonical_key, kind, display_name, payload, aliases,
    created_at, updated_at, scope_summary
) OVERRIDING SYSTEM VALUE
SELECT id, concept_id, canonical_key, kind, display_name, payload,
       to_jsonb(aliases), created_at, updated_at, scope_summary
FROM app.learner_entities, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (id) DO UPDATE SET
    display_name=EXCLUDED.display_name, payload=EXCLUDED.payload,
    aliases=EXCLUDED.aliases, updated_at=EXCLUDED.updated_at,
    scope_summary=EXCLUDED.scope_summary;

INSERT INTO public.apollo_entity_prereqs (from_entity_id,to_entity_id)
SELECT from_entity_id,to_entity_id
FROM internal.entity_prerequisites, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT DO NOTHING;

INSERT INTO public.apollo_learner_state (
    user_id, search_space_id, entity_id, belief, mastery, confidence,
    evidence_count, last_evidence_at, updated_at
)
SELECT user_id, course_id::integer, entity_id, belief, mastery, confidence,
       evidence_count, last_evidence_at, updated_at
FROM app.learner_state, _db05_reverse_context
WHERE created_at >= watermark OR updated_at >= watermark
ON CONFLICT (user_id,search_space_id,entity_id) DO UPDATE SET
    belief=EXCLUDED.belief, mastery=EXCLUDED.mastery,
    confidence=EXCLUDED.confidence, evidence_count=EXCLUDED.evidence_count,
    last_evidence_at=EXCLUDED.last_evidence_at, updated_at=EXCLUDED.updated_at;

INSERT INTO public.apollo_mastery_events (
    id, user_id, search_space_id, entity_id, attempt_id, event_kind, score,
    parser_confidence, grader_confidence, negotiation_move, reference_step_id,
    prior_belief, posterior_belief, mastery_after, dt_days_since_last,
    evidence_node_ids, created_at, concept_problem_id
) OVERRIDING SYSTEM VALUE
SELECT id, user_id, course_id::integer, entity_id, attempt_id, event_kind, score,
       parser_confidence, grader_confidence, negotiation_move, reference_step_id,
       prior_belief, posterior_belief, mastery_after, dt_days_since_last,
       to_jsonb(evidence_node_ids), created_at, concept_problem_id
FROM app.mastery_events, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.apollo_ingest_runs (
    id, search_space_id, document_id, content_hash, status,
    n_questions_scraped, n_promoted, n_rejected, n_dedup_merged, llm_calls,
    llm_tokens_in, llm_tokens_out, llm_cost_usd, started_at, finished_at,
    created_at, n_pages, dedup_pressure
) OVERRIDING SYSTEM VALUE
SELECT id, course_id::integer, document_id, content_hash, status,
       n_questions_scraped, n_promoted, n_rejected, n_dedup_merged, llm_calls,
       llm_tokens_in, llm_tokens_out, llm_cost_usd, started_at, finished_at,
       created_at, n_pages, jsonb_build_object(
           'total_candidates',dedup_total_candidates,
           'exact_merges',dedup_exact_merges,
           'embedding_merges',dedup_embedding_merges,
           'embedding_distinct',dedup_embedding_distinct,
           'embedding_merge_ratio',dedup_embedding_merge_ratio,
           'per_concept',dedup_details)
FROM internal.content_ingest_runs, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO UPDATE SET
    status=EXCLUDED.status, n_questions_scraped=EXCLUDED.n_questions_scraped,
    n_promoted=EXCLUDED.n_promoted, n_rejected=EXCLUDED.n_rejected,
    n_dedup_merged=EXCLUDED.n_dedup_merged, n_pages=EXCLUDED.n_pages,
    dedup_pressure=EXCLUDED.dedup_pressure, finished_at=EXCLUDED.finished_at;

INSERT INTO public.apollo_generation_runs (
    id, search_space_id, concept_id, status, result_summary, ingest_run_id, created_at
)
SELECT run.id-1000000, run.course_id::integer, run.concept_id, run.status,
       run.result_summary, run.ingest_run_id, run.created_at
FROM app.provisioning_runs AS run CROSS JOIN _db05_reverse_context AS ctx
WHERE run.kind='generation'
  AND (run.created_at >= ctx.watermark OR run.updated_at >= ctx.watermark)
ON CONFLICT (id) DO UPDATE SET
    status=EXCLUDED.status, result_summary=EXCLUDED.result_summary,
    ingest_run_id=EXCLUDED.ingest_run_id;

INSERT INTO public.apollo_ingest_errors (
    id, ingest_run_id, search_space_id, stage, error_class, context, created_at
) OVERRIDING SYSTEM VALUE
SELECT id, ingest_run_id, course_id::integer, stage, error_class, context, created_at
FROM internal.content_ingest_errors, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.apollo_ingest_page_evidence (
    id, ingest_run_id, search_space_id, document_id, role, page_number,
    ocr_text, ocr_confidence, extraction_mode, verify_path_fired, created_at
) OVERRIDING SYSTEM VALUE
SELECT id, ingest_run_id, course_id::integer, document_id, role, page_number,
       ocr_text, ocr_confidence, extraction_mode, verify_path_fired, created_at
FROM internal.ingest_page_evidence, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.apollo_dedup_decisions (
    id, ingest_run_id, search_space_id, concept_id, candidate_key, method,
    similarity, verdict, matched_entity_id, created_at
) OVERRIDING SYSTEM VALUE
SELECT id, ingest_run_id, course_id::integer, concept_id, candidate_key, method,
       similarity, verdict, matched_entity_id, created_at
FROM internal.dedup_decisions, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.apollo_grading_artifacts (
    id, attempt_id, role, grader_used, user_id, search_space_id, concept_id,
    problem_id, versions, node_ledger, edge_ledger, scores, abstention,
    grading_latency_ms, created_at
)
SELECT r.id, r.attempt_id, r.role, r.grader_used, r.user_id, r.course_id,
       r.concept_id, p.problem_code, r.version_details, r.node_ledger,
       r.edge_ledger, r.score_details, r.abstention_details,
       r.grading_latency_ms, r.created_at
FROM internal.grading_runs r JOIN app.problems p ON p.id=r.problem_id
CROSS JOIN _db05_reverse_context ctx
WHERE r.role IN ('canonical','pair') AND r.created_at >= ctx.watermark
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.ai_use_reports (
    id, chat_id, created_at, style, length, markdown, jsonld,
    model_fingerprint, tool_calls, prompt_hashes
)
SELECT id, chat_id, created_at, style, length, markdown, jsonld,
       model_fingerprint, tool_calls, to_jsonb(prompt_hashes)
FROM app.ai_usage_reports, _db05_reverse_context
WHERE created_at >= watermark
ON CONFLICT (id) DO UPDATE SET
    style=EXCLUDED.style, length=EXCLUDED.length, markdown=EXCLUDED.markdown,
    jsonld=EXCLUDED.jsonld, model_fingerprint=EXCLUDED.model_fingerprint,
    tool_calls=EXCLUDED.tool_calls, prompt_hashes=EXCLUDED.prompt_hashes;

-- Advance every legacy public sequence touched by explicit identity values.
DO $advance_legacy_sequences$
DECLARE identity_column record; sequence_name text; maximum_id bigint;
BEGIN
    FOR identity_column IN
        SELECT table_schema,table_name,column_name
        FROM information_schema.columns
        WHERE table_schema='public'
          AND (is_identity='YES' OR column_default LIKE 'nextval(%')
    LOOP
        sequence_name := pg_get_serial_sequence(
            format('%I.%I',identity_column.table_schema,identity_column.table_name),
            identity_column.column_name);
        IF sequence_name IS NOT NULL THEN
            EXECUTE format('SELECT max(%I)::bigint FROM %I.%I',
                identity_column.column_name, identity_column.table_schema,
                identity_column.table_name) INTO maximum_id;
            IF maximum_id IS NULL THEN
                PERFORM setval(sequence_name::regclass,1,false);
            ELSE
                PERFORM setval(sequence_name::regclass,maximum_id,true);
            END IF;
        END IF;
    END LOOP;
END
$advance_legacy_sequences$;

-- Fail-loud reverse checksums for the principal ownership/FK spine. The full
-- 28-table checksum gate is scripts/db/reconcile_copy.sql, run immediately
-- after this transaction as shown above.
DO $reverse_checksum_assertions$
DECLARE source_hash text; target_hash text;
BEGIN
    SELECT md5(COALESCE(string_agg(md5(to_jsonb(x)::text),'' ORDER BY id),''))
      INTO target_hash
      FROM (SELECT id,slug,current_week FROM app.courses,_db05_reverse_context
            WHERE created_at>=watermark OR updated_at>=watermark) x;
    SELECT md5(COALESCE(string_agg(md5(to_jsonb(x)::text),'' ORDER BY id),''))
      INTO source_hash
      FROM (SELECT s.id,s.slug,tc.current_week FROM public.aita_search_spaces s
            JOIN public.teacher_courses tc ON tc.search_space_id=s.id
            JOIN app.courses a ON a.id=s.id CROSS JOIN _db05_reverse_context
            WHERE a.created_at>=watermark OR a.updated_at>=watermark) x;
    IF source_hash <> target_hash THEN RAISE EXCEPTION 'reverse checksum failed: courses'; END IF;

    SELECT md5(COALESCE(string_agg(md5(to_jsonb(x)::text),'' ORDER BY id),''))
      INTO target_hash
      FROM (SELECT id,content_hash,status FROM app.documents,_db05_reverse_context
            WHERE created_at>=watermark OR updated_at>=watermark) x;
    SELECT md5(COALESCE(string_agg(md5(to_jsonb(x)::text),'' ORDER BY id),''))
      INTO source_hash
      FROM (SELECT d.id,d.content_hash,d.status->>'state' AS status
            FROM public.aita_documents d JOIN app.documents a ON a.id=d.id
            CROSS JOIN _db05_reverse_context
            WHERE a.created_at>=watermark OR a.updated_at>=watermark) x;
    IF source_hash <> target_hash THEN RAISE EXCEPTION 'reverse checksum failed: documents'; END IF;

    SELECT md5(COALESCE(string_agg(md5(to_jsonb(x)::text),'' ORDER BY id),''))
      INTO target_hash
      FROM (SELECT id,external_id,user_id,course_id FROM app.learning_activities,_db05_reverse_context
            WHERE modality='chat' AND (created_at>=watermark OR updated_at>=watermark)) x;
    SELECT md5(COALESCE(string_agg(md5(to_jsonb(x)::text),'' ORDER BY id),''))
      INTO source_hash
      FROM (SELECT s.id,s.chat_id AS external_id,s.user_id,s.search_space_id::bigint AS course_id
             FROM public.chat_sessions s JOIN app.learning_activities a
               ON a.id=s.id AND a.modality='chat'
            CROSS JOIN _db05_reverse_context
            WHERE a.created_at>=watermark OR a.updated_at>=watermark) x;
    IF source_hash <> target_hash THEN RAISE EXCEPTION 'reverse checksum failed: learning_activities(chat)'; END IF;
END
$reverse_checksum_assertions$;

DO $reverse_complete$
BEGIN
    RAISE NOTICE 'DB-05 reverse delta copied and principal checksums passed; run reconcile_copy.sql before reopening writes';
END
$reverse_complete$;
