/*
 * DRAFT - DERIVED FROM THE FROZEN LEGACY MIGRATION CHAIN, NOT PRODUCTION.
 *
 * This file is a locally reconstructed fallback. The HUMAN-ONLY db-pull
 * snapshot in docs/_archive/handoffs/2026-07-16-db-history-repair-handoff.md
 * is authoritative and MUST replace this draft before any remote history
 * reconciliation. Known production drift includes the duplicate 023 pair and
 * untracked pre-043 history, so production may differ from this file.
 *
 * Reconstruction method: migration 001's public schema DDL at the production
 * embedding dimension (3072), followed by every frozen SQL migration 004..047
 * in lexicographic filename order (including both 023 files). Python migrations
 * 002 and 003 move data and are intentionally excluded from this schema draft.
 */

SET lock_timeout = '5s';
SET statement_timeout = '5min';


-- SOURCE: database/migrations/001_create_schema.py (DDL)
-- Enable pgvector if not already enabled
CREATE EXTENSION IF NOT EXISTS vector;

-- -----------------------------------------------------------------------
-- aita_search_spaces  (one row per class / course)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aita_search_spaces (
    id               SERIAL PRIMARY KEY,
    name             VARCHAR(200) NOT NULL,
    slug             VARCHAR(200) NOT NULL UNIQUE,
    subject_name     VARCHAR(200) NOT NULL,
    weight_overrides JSONB        NOT NULL DEFAULT '{}'::jsonb,
    metadata         JSONB,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aita_search_spaces_name ON aita_search_spaces (name);
CREATE INDEX IF NOT EXISTS idx_aita_search_spaces_slug ON aita_search_spaces (slug);

-- -----------------------------------------------------------------------
-- aita_documents  (one row per uploaded PDF / material)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aita_documents (
    id                      SERIAL PRIMARY KEY,
    title                   VARCHAR        NOT NULL,
    document_type           VARCHAR        NOT NULL DEFAULT 'EDUCATIONAL_FILE',
    material_kind           VARCHAR(50)    NOT NULL DEFAULT 'other',
    content                 TEXT           NOT NULL,
    source_markdown         TEXT,
    content_hash            VARCHAR        NOT NULL UNIQUE,
    unique_identifier_hash  VARCHAR        UNIQUE,
    embedding               vector(3072),
    document_metadata       JSONB,
    page_count              INTEGER,
    week                    INTEGER,
    status                  JSONB          NOT NULL DEFAULT '{"state": "ready"}'::jsonb,
    search_space_id         INTEGER        NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    created_at              TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aita_documents_search_space ON aita_documents (search_space_id);
CREATE INDEX IF NOT EXISTS idx_aita_documents_material_kind ON aita_documents (material_kind);
CREATE INDEX IF NOT EXISTS idx_aita_documents_status ON aita_documents USING gin (status);
CREATE INDEX IF NOT EXISTS idx_aita_documents_content_hash ON aita_documents (content_hash);

-- -----------------------------------------------------------------------
-- aita_chunks  (one row per layout-aware Item / paragraph / heading / figure)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aita_chunks (
    id           SERIAL PRIMARY KEY,
    content      TEXT           NOT NULL,
    embedding    vector(3072),
    page_number  INTEGER,
    section_path TEXT,
    chunk_type   VARCHAR(20)    NOT NULL DEFAULT 'body',
    figure_id    VARCHAR,
    document_id  INTEGER        NOT NULL REFERENCES aita_documents(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aita_chunks_document ON aita_chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_aita_chunks_page ON aita_chunks (page_number);

-- Full-text search index (GIN on tsvector of chunk content)
CREATE INDEX IF NOT EXISTS idx_aita_chunks_fts
    ON aita_chunks USING gin (to_tsvector('english', content));


-- SOURCE: database/migrations/004_teacher_features.sql
-- 004_teacher_features.sql
-- Supabase-first teacher weekly features keyed by aita_search_spaces.id.

BEGIN;

CREATE TABLE IF NOT EXISTS teacher_courses (
    id              BIGSERIAL PRIMARY KEY,
    search_space_id INTEGER NOT NULL UNIQUE
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    current_week    INTEGER NOT NULL DEFAULT 1 CHECK (current_week BETWEEN 1 AND 16),
    weights         JSONB   NOT NULL DEFAULT '{}'::jsonb,
    weight_bounds   JSONB   NOT NULL DEFAULT '{"min":0.0,"max":1.0}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_teacher_courses_search_space_id
    ON teacher_courses (search_space_id);

CREATE TABLE IF NOT EXISTS teacher_uploads (
    id              BIGSERIAL PRIMARY KEY,
    search_space_id INTEGER NOT NULL
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    week            INTEGER NOT NULL CHECK (week BETWEEN 1 AND 16),
    kind            TEXT    NOT NULL CHECK (kind IN ('notes', 'slides')),
    title           TEXT    NOT NULL,
    source_name     TEXT,
    doc_id          INTEGER
        REFERENCES aita_documents(id) ON DELETE SET NULL,
    page_count      INTEGER,
    is_latest       BOOLEAN NOT NULL DEFAULT TRUE,
    uploaded_by     UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    metadata        JSONB   NOT NULL DEFAULT '{}'::jsonb,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_teacher_uploads_lookup
    ON teacher_uploads (search_space_id, week, kind, uploaded_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_teacher_uploads_latest
    ON teacher_uploads (search_space_id, week, kind)
    WHERE is_latest = TRUE;

ALTER TABLE teacher_courses ENABLE ROW LEVEL SECURITY;
ALTER TABLE teacher_uploads ENABLE ROW LEVEL SECURITY;

COMMIT;


-- SOURCE: database/migrations/005_chat_memory.sql
-- 005_chat_memory.sql
-- Chat ownership + memory summary keyed by chat_id and user_id.

BEGIN;

-- If an existing table already exists in Supabase, this migration is intended
-- for fresh installs. Existing deployments should run a manual data migration.
CREATE TABLE IF NOT EXISTS chat_sessions (
    id               BIGSERIAL PRIMARY KEY,
    chat_id          TEXT    NOT NULL UNIQUE,
    user_id          UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id  INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    meta             JSONB   NOT NULL DEFAULT '{}'::jsonb,
    memory_summary   TEXT    NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chat_turns (
    id               BIGSERIAL PRIMARY KEY,
    chat_session_id  BIGINT  NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    turn_index       INTEGER NOT NULL,
    turn_id          TEXT    NOT NULL,
    role             TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content          TEXT    NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model            TEXT,
    tool_name        TEXT,
    tool_inputs      JSONB,
    attachments      JSONB   NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_chat_turns_session_turn_index
    ON chat_turns (chat_session_id, turn_index);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_updated
    ON chat_sessions (user_id, updated_at DESC);

ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_turns ENABLE ROW LEVEL SECURITY;

COMMIT;


-- SOURCE: database/migrations/006_membership_auth.sql
-- 006_membership_auth.sql
-- Course memberships + RLS policies for secure multi-user isolation.

BEGIN;

CREATE TABLE IF NOT EXISTS course_memberships (
    user_id          UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id  INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    role             TEXT    NOT NULL CHECK (role IN ('student', 'teacher')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, search_space_id)
);

CREATE INDEX IF NOT EXISTS idx_course_memberships_search_space
    ON course_memberships (search_space_id, role);

ALTER TABLE course_memberships ENABLE ROW LEVEL SECURITY;

-- RLS policies
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'course_memberships'
          AND policyname = 'course_memberships_self_read'
    ) THEN
        CREATE POLICY course_memberships_self_read
            ON course_memberships FOR SELECT
            USING (user_id = auth.uid());
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'chat_sessions'
          AND policyname = 'chat_sessions_owner_rw'
    ) THEN
        CREATE POLICY chat_sessions_owner_rw
            ON chat_sessions FOR ALL
            USING (user_id = auth.uid())
            WITH CHECK (user_id = auth.uid());
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'chat_turns'
          AND policyname = 'chat_turns_owner_rw'
    ) THEN
        CREATE POLICY chat_turns_owner_rw
            ON chat_turns FOR ALL
            USING (
                EXISTS (
                    SELECT 1
                    FROM chat_sessions cs
                    WHERE cs.id = chat_turns.chat_session_id
                      AND cs.user_id = auth.uid()
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM chat_sessions cs
                    WHERE cs.id = chat_turns.chat_session_id
                      AND cs.user_id = auth.uid()
                )
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'teacher_courses'
          AND policyname = 'teacher_courses_teacher_rw'
    ) THEN
        CREATE POLICY teacher_courses_teacher_rw
            ON teacher_courses FOR ALL
            USING (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = teacher_courses.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = teacher_courses.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            );
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'teacher_uploads'
          AND policyname = 'teacher_uploads_teacher_rw'
    ) THEN
        CREATE POLICY teacher_uploads_teacher_rw
            ON teacher_uploads FOR ALL
            USING (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = teacher_uploads.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = teacher_uploads.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            );
    END IF;
END $$;

COMMIT;


-- SOURCE: database/migrations/007_teacher_upload_async.sql
-- 007_teacher_upload_async.sql
-- Expand teacher uploads for async ingestion and add durable worker jobs.

BEGIN;

ALTER TABLE teacher_uploads
    ADD COLUMN IF NOT EXISTS status TEXT,
    ADD COLUMN IF NOT EXISTS storage_key TEXT,
    ADD COLUMN IF NOT EXISTS artifact_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS ocr_provider TEXT,
    ADD COLUMN IF NOT EXISTS ocr_summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS warning_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS error_message TEXT,
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;

UPDATE teacher_uploads
SET status = CASE
    WHEN COALESCE(is_latest, FALSE) THEN 'ready'
    ELSE 'superseded'
END
WHERE status IS NULL;

ALTER TABLE teacher_uploads
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN status SET DEFAULT 'ready';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'teacher_uploads_status_check'
    ) THEN
        ALTER TABLE teacher_uploads
            ADD CONSTRAINT teacher_uploads_status_check
            CHECK (status IN ('queued', 'processing', 'ready', 'failed', 'superseded'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_teacher_uploads_status
    ON teacher_uploads (status, uploaded_at DESC);

CREATE INDEX IF NOT EXISTS idx_teacher_uploads_storage_key
    ON teacher_uploads (storage_key);

CREATE TABLE IF NOT EXISTS teacher_upload_jobs (
    id               BIGSERIAL PRIMARY KEY,
    upload_id        BIGINT NOT NULL
        REFERENCES teacher_uploads(id) ON DELETE CASCADE,
    state            TEXT NOT NULL DEFAULT 'queued',
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'teacher_upload_jobs_state_check'
    ) THEN
        ALTER TABLE teacher_upload_jobs
            ADD CONSTRAINT teacher_upload_jobs_state_check
            CHECK (state IN ('queued', 'processing', 'completed', 'failed'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_teacher_upload_jobs_queue
    ON teacher_upload_jobs (state, lease_expires_at, created_at);

CREATE INDEX IF NOT EXISTS idx_teacher_upload_jobs_upload_id
    ON teacher_upload_jobs (upload_id);

COMMIT;


-- SOURCE: database/migrations/008_invite_links.sql
-- 008_invite_links.sql
-- Shareable invite links for course enrollment (student + teacher roles).

BEGIN;

CREATE TABLE IF NOT EXISTS course_invite_links (
    id              BIGSERIAL   PRIMARY KEY,
    code            TEXT        NOT NULL UNIQUE,
    search_space_id INTEGER     NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    role            TEXT        NOT NULL CHECK (role IN ('student', 'teacher')),
    created_by      UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    max_uses        INTEGER     DEFAULT NULL,
    use_count       INTEGER     NOT NULL DEFAULT 0,
    expires_at      TIMESTAMPTZ DEFAULT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_invite_links_code
    ON course_invite_links (code) WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_invite_links_space
    ON course_invite_links (search_space_id);

ALTER TABLE course_invite_links ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'course_invite_links'
          AND policyname = 'invite_links_teacher_rw'
    ) THEN
        CREATE POLICY invite_links_teacher_rw
            ON course_invite_links FOR ALL
            USING (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = course_invite_links.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1
                    FROM course_memberships cm
                    WHERE cm.search_space_id = course_invite_links.search_space_id
                      AND cm.user_id = auth.uid()
                      AND cm.role = 'teacher'
                )
            );
    END IF;
END $$;

COMMIT;


-- SOURCE: database/migrations/009_apollo_slice0.sql
-- 009_apollo_slice0.sql
-- Apollo v2 Slice 0 persistence tables: sessions, KG entries, messages, problem attempts.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_sessions (
    id                   BIGSERIAL PRIMARY KEY,
    student_id           TEXT       NOT NULL,
    concept_cluster_id   TEXT       NOT NULL,
    status               TEXT       NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'paused', 'ended')),
    phase                TEXT       NOT NULL DEFAULT 'INIT'
                         CHECK (phase IN ('INIT','TEACHING','PROBLEM_REVEAL','SOLVING','REPORT','BETWEEN')),
    current_problem_id   TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_touched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_apollo_sessions_student ON apollo_sessions(student_id);

-- Enforce one active session per student at a time.
CREATE UNIQUE INDEX IF NOT EXISTS ix_apollo_sessions_unique_active_per_student
    ON apollo_sessions(student_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS apollo_kg_entries (
    id           BIGSERIAL PRIMARY KEY,
    session_id   BIGINT      NOT NULL REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    type         TEXT        NOT NULL
                 CHECK (type IN ('equation','definition','condition','simplification','variable_mapping')),
    content      JSONB       NOT NULL,
    source       TEXT        NOT NULL DEFAULT 'parser'
                 CHECK (source IN ('parser', 'student_edit')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_apollo_kg_entries_session ON apollo_kg_entries(session_id);

CREATE TABLE IF NOT EXISTS apollo_messages (
    id           BIGSERIAL PRIMARY KEY,
    session_id   BIGINT      NOT NULL REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    role         TEXT        NOT NULL CHECK (role IN ('student', 'apollo', 'system')),
    content      TEXT        NOT NULL,
    turn_index   INTEGER     NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_apollo_messages_session ON apollo_messages(session_id);

CREATE TABLE IF NOT EXISTS apollo_problem_attempts (
    id                   BIGSERIAL PRIMARY KEY,
    session_id           BIGINT      NOT NULL REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    problem_id           TEXT        NOT NULL,
    difficulty           TEXT        NOT NULL CHECK (difficulty IN ('intro', 'standard', 'hard')),
    result               TEXT
                         CHECK (result IS NULL OR result IN ('solved', 'stuck', 'skipped', 'returned_to_hoot')),
    solver_trace         JSONB,
    diagnostic_report    JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_apollo_problem_attempts_session ON apollo_problem_attempts(session_id);

COMMIT;


-- SOURCE: database/migrations/010_apollo_procedure_step.sql
-- 010_apollo_procedure_step.sql
-- Widen apollo_kg_entries.type CHECK constraint to include 'procedure_step'.
-- Added for Apollo teaching-rigor phase 1 (2026-04-21): the parser now emits
-- procedure_step entries (order/action/uses_equations/purpose), but migration
-- 009 only allowed the original five entry types, causing CheckViolationError
-- on every KG insert batch containing a procedure_step.

BEGIN;

ALTER TABLE apollo_kg_entries
    DROP CONSTRAINT IF EXISTS apollo_kg_entries_type_check;

ALTER TABLE apollo_kg_entries
    ADD CONSTRAINT apollo_kg_entries_type_check
    CHECK (type IN (
        'equation',
        'definition',
        'condition',
        'simplification',
        'variable_mapping',
        'procedure_step'
    ));

COMMIT;


-- SOURCE: database/migrations/011_chat_turn_citations.sql
-- 011_chat_turn_citations.sql
-- Persist citation badges per assistant turn so old chats can re-render them.
-- Prior to this migration, citations were computed at request time, returned
-- to the client in the /ask response, and then discarded — reloading a chat
-- lost every badge.

BEGIN;

ALTER TABLE chat_turns
    ADD COLUMN IF NOT EXISTS citations JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;


-- SOURCE: database/migrations/012_ai_use_reports.sql
-- 012_ai_use_reports.sql
-- Create the ai_use_reports table used by reports/ai_use/models.py. The
-- table was previously created by hand in the old Supabase project and was
-- never backed by a migration; provisioning a fresh Supabase project left
-- /reports/ai-use endpoints 4xx/5xx until now.
--
-- Ownership is enforced at the application layer (see
-- reports/ai_use/routes.py::_require_owned_report). The backend uses the
-- Supabase anon key to talk to PostgREST here, so we leave RLS disabled on
-- this table rather than synthesising auth.uid() context from a service
-- role. All reads/writes funnel through the FastAPI routes which already
-- check chat ownership.

BEGIN;

CREATE TABLE IF NOT EXISTS ai_use_reports (
    id                 UUID        PRIMARY KEY,
    chat_id            TEXT        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    style              TEXT,
    length             TEXT,
    markdown           TEXT,
    jsonld             JSONB,
    model_fingerprint  TEXT,
    tool_calls         JSONB,
    prompt_hashes      JSONB
);

CREATE INDEX IF NOT EXISTS idx_ai_use_reports_chat_created
    ON ai_use_reports (chat_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ai_use_reports_created
    ON ai_use_reports (created_at DESC);

COMMIT;


-- SOURCE: database/migrations/013_apollo_student_progress.sql
-- 013_apollo_student_progress.sql
-- Apollo teaching-rigor phase 2 (gamification): persistent XP + level per
-- student, keyed by the same student_id string stored on apollo_sessions.
-- Kept in a side table so gamification state is isolated from identity /
-- auth state and can be reset without touching core student records.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_student_progress (
    student_id        TEXT        PRIMARY KEY,
    xp_total          INTEGER     NOT NULL DEFAULT 0,
    level             INTEGER     NOT NULL DEFAULT 1,
    last_level_up_at  TIMESTAMPTZ NULL
);

COMMIT;


-- SOURCE: database/migrations/014_apollo_attempt_id.sql
-- 014_apollo_attempt_id.sql
-- Migrate apollo_kg_entries and apollo_messages from session-scoped to
-- attempt-scoped. Adds nullable attempt_id columns with FK + cascade to
-- apollo_problem_attempts, backfills from the single existing attempt per
-- session (true for all current data because sessions have been
-- single-problem to date), and indexes the new columns.

BEGIN;

ALTER TABLE apollo_kg_entries
    ADD COLUMN IF NOT EXISTS attempt_id BIGINT
    REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE;

ALTER TABLE apollo_messages
    ADD COLUMN IF NOT EXISTS attempt_id BIGINT
    REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE;

UPDATE apollo_kg_entries
SET attempt_id = (
    SELECT id FROM apollo_problem_attempts
    WHERE session_id = apollo_kg_entries.session_id
    ORDER BY id DESC
    LIMIT 1
)
WHERE attempt_id IS NULL;

UPDATE apollo_messages
SET attempt_id = (
    SELECT id FROM apollo_problem_attempts
    WHERE session_id = apollo_messages.session_id
    ORDER BY id DESC
    LIMIT 1
)
WHERE attempt_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_apollo_kg_entries_attempt_id
    ON apollo_kg_entries(attempt_id);

CREATE INDEX IF NOT EXISTS ix_apollo_messages_attempt_id
    ON apollo_messages(attempt_id);

COMMIT;


-- SOURCE: database/migrations/015_rag_orchestrator.sql
-- 015_rag_orchestrator.sql
-- Adds RAG orchestrator persistence: cached snippets per chat session,
-- topic centroid for follow-up detection, and router decision telemetry.

BEGIN;

ALTER TABLE chat_sessions
  ADD COLUMN IF NOT EXISTS topic_centroid_vector vector(3072) NULL;

CREATE TABLE IF NOT EXISTS chat_session_snippets (
  chat_session_id   BIGINT      NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  chunk_id          BIGINT      NOT NULL REFERENCES aita_chunks(id)   ON DELETE CASCADE,
  original_score    REAL        NOT NULL,
  first_seen_turn   INTEGER     NOT NULL,
  last_used_turn    INTEGER     NOT NULL,
  snippet_payload   JSONB       NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (chat_session_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS ix_css_session_lru
  ON chat_session_snippets (chat_session_id, last_used_turn);

ALTER TABLE chat_session_snippets ENABLE ROW LEVEL SECURITY;
CREATE POLICY css_owner_only ON chat_session_snippets
  USING (
    chat_session_id IN (
      SELECT id FROM chat_sessions WHERE user_id = auth.uid()
    )
  );

CREATE TABLE IF NOT EXISTS chat_router_decisions (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  turn_id               TEXT       NOT NULL,
  chat_session_id       BIGINT     NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
  query                 TEXT       NOT NULL,
  stage1_top1           REAL,
  stage1_margin         REAL,
  stage1_route          TEXT,
  stage2_invoked        BOOLEAN    NOT NULL DEFAULT FALSE,
  stage2_route          TEXT,
  stage2_confidence     REAL,
  retrieval_mode        TEXT       NOT NULL,
  retrieval_top_score   REAL,
  final_route           TEXT       NOT NULL,
  was_clarified         BOOLEAN    NOT NULL DEFAULT FALSE,
  clarify_cause         TEXT,
  latency_router_ms     INTEGER,
  latency_retrieval_ms  INTEGER,
  latency_answer_ms     INTEGER,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_router_decisions_session_ts
  ON chat_router_decisions (chat_session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_router_decisions_route_ts
  ON chat_router_decisions (final_route, created_at DESC);

ALTER TABLE chat_router_decisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY crd_owner_only ON chat_router_decisions
  USING (
    chat_session_id IN (
      SELECT id FROM chat_sessions WHERE user_id = auth.uid()
    )
  );

COMMIT;


-- SOURCE: database/migrations/016_apollo_pending_intent.sql
-- 016_apollo_pending_intent.sql
-- Item #5: add `pending_intent` column to apollo_sessions so the chat
-- handler can stash a one-turn confirmation ("you sounded like you were
-- done teaching — confirm?") and resolve it on the next student turn.
--
-- Nullable. Cleared when the next turn arrives (whether the student
-- confirmed or not). Values are the string forms of the Intent literal
-- in `apollo/handlers/intent.py` — application-side enforced, not a
-- DB enum (additive intents shouldn't require migrations).

BEGIN;

ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS pending_intent TEXT;

COMMIT;


-- SOURCE: database/migrations/017_apollo_history_summary.sql
-- 017_apollo_history_summary.sql
-- Item #2: rolling history summary so Apollo's chat context stays bounded.
--
-- `history_summary` holds a short LLM-generated digest of conversation
-- turns older than the windowing cutoff. `history_summary_up_to_turn`
-- records the highest turn_index covered by that summary so we know
-- when to refresh it (every K new turns) without reading every message.

BEGIN;

ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS history_summary TEXT;

ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS history_summary_up_to_turn INTEGER;

COMMIT;


-- SOURCE: database/migrations/018_apollo_concept_registry.sql
-- 018_apollo_concept_registry.sql
-- Class 2 Phase 2: move the per-subject / per-concept registry from the
-- filesystem (apollo/subjects/<subject>/concepts/<concept>/*.json) into
-- Postgres so curriculum content is data, not code.
--
-- Rationale: the first real class will not be fluid mechanics. Hardcoding
-- subject/concept strings into Python (cluster_to_concept maps,
-- load_concept(subject_id, concept_id) signatures) couples the runtime to
-- the curriculum and forces a code deploy for every new class. After
-- this migration, adding a class is one INSERT per subject + N INSERTs
-- per concept, runnable from the teacher UI or a seeder script.
--
-- New tables:
--   apollo_subjects             — top-level curriculum domain
--   apollo_concepts             — one row per teachable concept; the per-
--                                 concept artifacts (canonical_symbols,
--                                 forbidden_named_laws, parser_prompt_template,
--                                 solver_hints, normalization_map) live as
--                                 JSONB / TEXT columns
--   apollo_concept_problems     — per-concept problem bank
--
-- Modified tables:
--   apollo_sessions             — concept_cluster_id (TEXT) replaced by
--                                 concept_id (FK BIGINT)

BEGIN;

-- -----------------------------------------------------------------------------
-- apollo_subjects
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS apollo_subjects (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE apollo_subjects IS
    'Top-level curriculum domain (e.g. fluid_mechanics, thermodynamics, organic_chemistry).';
COMMENT ON COLUMN apollo_subjects.slug IS
    'Stable, machine-readable identifier. Matches the legacy filesystem dir name during migration.';

-- -----------------------------------------------------------------------------
-- apollo_concepts
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS apollo_concepts (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_id              BIGINT NOT NULL REFERENCES apollo_subjects(id) ON DELETE CASCADE,
    slug                    TEXT NOT NULL,
    display_name            TEXT NOT NULL,
    -- Concept-shaped curriculum payloads — verbatim from the registry's
    -- per-concept JSON / Markdown files. Pydantic models continue to
    -- validate these on load; storing them as JSONB / TEXT keeps the
    -- migration additive and lossless.
    canonical_symbols       JSONB NOT NULL DEFAULT '{}'::jsonb,
    normalization_map       JSONB NOT NULL DEFAULT '{}'::jsonb,
    parser_prompt_template  TEXT  NOT NULL DEFAULT '',
    solver_hints            JSONB NOT NULL DEFAULT '{}'::jsonb,
    forbidden_named_laws    JSONB NOT NULL DEFAULT '{}'::jsonb,
    concept_dag             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (subject_id, slug)
);

CREATE INDEX IF NOT EXISTS apollo_concepts_subject_idx
    ON apollo_concepts(subject_id);

COMMENT ON TABLE apollo_concepts IS
    'One row per teachable concept. Replaces the filesystem layout '
    'apollo/subjects/<subject>/concepts/<concept>/*.json. Curriculum '
    'authors edit rows here (not code) to add or revise content.';

-- -----------------------------------------------------------------------------
-- apollo_concept_problems
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS apollo_concept_problems (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_id    BIGINT NOT NULL REFERENCES apollo_concepts(id) ON DELETE CASCADE,
    -- Author-facing identifier — stable across edits, used by tests, logs,
    -- and ProblemAttempt.problem_id.
    problem_code  TEXT NOT NULL,
    difficulty    TEXT NOT NULL,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (concept_id, problem_code)
);

CREATE INDEX IF NOT EXISTS apollo_concept_problems_concept_difficulty_idx
    ON apollo_concept_problems(concept_id, difficulty);

COMMENT ON TABLE apollo_concept_problems IS
    'Per-concept problem bank. Replaces apollo/subjects/.../problems/*.json. '
    'payload is the full Problem schema (problem_text, given_values, '
    'target_unknown, reference_solution).';

-- -----------------------------------------------------------------------------
-- apollo_sessions: concept_cluster_id (TEXT) -> concept_id (FK BIGINT)
-- -----------------------------------------------------------------------------
ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS concept_id BIGINT
        REFERENCES apollo_concepts(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS apollo_sessions_concept_id_idx
    ON apollo_sessions(concept_id);

COMMENT ON COLUMN apollo_sessions.concept_id IS
    'FK into apollo_concepts. Replaces concept_cluster_id (TEXT). '
    'Backfill happens in the seeder once concept rows exist; '
    'concept_cluster_id stays during the cutover and is dropped in 022.';

COMMIT;


-- SOURCE: database/migrations/019_apollo_misconceptions.sql
-- 019_apollo_misconceptions.sql
-- Class 2 Phase 2: per-concept misconception bank backing the
-- misconception-inference channel (apollo/overseer/misconception.py).
--
-- The pipeline (per Macina 2024 + MISTAKE 2510.11502 + G-R-R 2602.02414):
--   1. LLM generates a candidate misconception from the student utterance.
--   2. Embed the candidate; nearest-neighbor over `description_embedding`
--      restricted to the session's concept_id finds attested matches.
--   3. Macina-style verifier confirms the match.
--   4. On verified hit, the row's probe_question / rt_steps drive Apollo's
--      Socratic-debugging persona shift.
--
-- Storage strategy per CLAUDE.md:
--   - Store as `vector(3072)` (full float32 precision).
--   - HNSW expression index casts to `halfvec(3072)` at index time
--     (HNSW limit is 4000 dims for halfvec, 2000 for vector). 50% less
--     memory; precision loss is negligible for cosine on text embeddings.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_misconceptions (
    id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_id            BIGINT NOT NULL
        REFERENCES apollo_concepts(id) ON DELETE CASCADE,
    -- Author-facing stable identifier (e.g. 'no_density', 'pressure_for_velocity').
    -- Lets curriculum authors reference a misconception in tests + analytics
    -- without depending on the surrogate id.
    code                  TEXT NOT NULL,
    description           TEXT NOT NULL,
    description_embedding vector(3072),
    -- Optional structured pair encoding the conceptual confusion
    -- (e.g. confusion_pair_a='mass_flow', confusion_pair_b='volumetric_flow').
    -- Used for analytics; the inference pipeline retrieves on the embedding,
    -- not the pair.
    confusion_pair_a      TEXT,
    confusion_pair_b      TEXT,
    -- Verbatim student phrasings that hint at this misconception. Used by
    -- the optional fast-path matcher and as authoring guidance.
    trigger_phrases       JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Apollo-voiced probing question fed into the persona shift on a
    -- TAU_PROBE-band detection. Must read in confused-tutee voice.
    probe_question        TEXT NOT NULL,
    -- Reasoning Trajectory steps (per arXiv 2511.00371) — the sequence of
    -- diagnostic moves Apollo walks the student through on a TAU_FIRE-band
    -- detection. Shorter is better (paper finding: validity is inversely
    -- correlated with trajectory length).
    rt_steps              JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (concept_id, code)
);

CREATE INDEX IF NOT EXISTS apollo_misconceptions_concept_idx
    ON apollo_misconceptions(concept_id);

-- HNSW expression index — halfvec, cosine distance.
-- See CLAUDE.md "Critical Stack Decision: HNSW Indexing Strategy for 3072
-- Dimensions" for the rationale.
CREATE INDEX IF NOT EXISTS apollo_misconceptions_embedding_hnsw_idx
    ON apollo_misconceptions
    USING hnsw ((description_embedding::halfvec(3072)) halfvec_cosine_ops);

COMMENT ON TABLE apollo_misconceptions IS
    'Per-concept authored misconception bank for the misconception-inference '
    'channel (Apollo Gap B). Curriculum authors INSERT rows here when a '
    'class is added; runtime queries by concept_id (set on apollo_sessions).';

COMMIT;


-- SOURCE: database/migrations/020_apollo_message_metadata.sql
-- 020_apollo_message_metadata.sql
-- Class 2 Phase 2 (P2.7): per-turn metadata channel on apollo_messages.
--
-- Used today by the misconception-inference pipeline to persist the
-- per-turn `MisconceptionSignal` for offline eval and for the
-- PROBE-then-confirm gate (which needs the previous turn's signal).
--
-- Schema choice: JSONB for forward-compat. Other per-turn signals
-- (sufficiency, intent, parser_confidence aggregates) may join the
-- same column when they are useful to retain across turns. The
-- response envelope keeps the canonical shape; this column is the
-- audit trail.

BEGIN;

ALTER TABLE apollo_messages
    ADD COLUMN IF NOT EXISTS metadata JSONB;

COMMENT ON COLUMN apollo_messages.metadata IS
    'Per-turn signals (misconception, sufficiency, etc.) for offline '
    'eval and short-history readback (e.g. PROBE-then-confirm gate). '
    'Schema-less by design — readers must tolerate missing keys.';

COMMIT;


-- SOURCE: database/migrations/021_apollo_kg_negotiations.sql
-- 021_apollo_kg_negotiations.sql
-- Class 2 Phase 3 (P3.1): Negotiable Open Learner Model — audit log for the
-- three negotiation moves (CHALLENGE / SUPPLY-PARAPHRASE / SKIP) the student
-- can perform on a parser-authored KG entry.
--
-- Research anchors:
--   STyLE-OLM (Dimitrova 2003)        — interactive open learner modelling
--                                       with structured negotiation moves
--   Mr Collins (Bull & Pain 1995)     — dual-belief schema; "justify",
--                                       "accept-system", "accept-compromise"
--   CALMsystem (Kerly & Bull 2007)    — chat-based negotiation; ~40%
--                                       reduction in self-assessment
--                                       discrepancies (n=30)
--
-- Per-attempt rows are source-of-truth for the audit trail. Neo4j carries
-- the *current* status/student_belief on each node (live state); this table
-- is the *history* (what moved, when, with what payload).
--
-- The 3 moves and their payload shape (validated at the handler):
--   challenge   { reason: TEXT }                — flag for grader review
--   paraphrase  { surface_form: TEXT }          — student's wording
--   skip        { }                             — pass through w/o edits
--
-- The Done-gate (P3.6) is satisfied iff every flagged entry has at least
-- one row in this table for the active attempt.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_kg_negotiations (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id      BIGINT NOT NULL
                    REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    -- Neo4j node_id — TEXT to match the per-attempt subgraph node identifier.
    -- Not a Postgres FK because the canonical store is Neo4j; orphan rows
    -- are tolerated (the attempt_id FK + ON DELETE CASCADE handles cleanup
    -- when the attempt row goes away).
    entry_id        TEXT   NOT NULL,
    actor           TEXT   NOT NULL CHECK (actor IN ('student','parser','system')),
    move            TEXT   NOT NULL CHECK (move IN ('challenge','paraphrase','skip')),
    payload         JSONB  NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS apollo_kg_negotiations_attempt_id_idx
    ON apollo_kg_negotiations(attempt_id);

CREATE INDEX IF NOT EXISTS apollo_kg_negotiations_attempt_entry_idx
    ON apollo_kg_negotiations(attempt_id, entry_id);

COMMENT ON TABLE apollo_kg_negotiations IS
    'Audit log for the three Negotiable OLM moves (challenge / paraphrase / '
    'skip). One row per move; the latest row per (attempt_id, entry_id) is '
    'the operative move when the Done-gate evaluates whether a flagged '
    'entry has been touched.';

COMMENT ON COLUMN apollo_kg_negotiations.entry_id IS
    'Neo4j node_id (TEXT) of the negotiated KG entry within the per-attempt '
    'subgraph. Not a Postgres FK — Neo4j is the canonical store.';

COMMENT ON COLUMN apollo_kg_negotiations.payload IS
    'Move-specific data. challenge -> {reason:str}; '
    'paraphrase -> {surface_form:str}; skip -> {}.';

COMMIT;


-- SOURCE: database/migrations/022_enable_rls_stopgap.sql
-- 022: RLS stopgap — enable (not FORCE) row level security on all public tables.
--
-- Applied to test (hjevtxdtrkxjcaaexdxt) and prod (uduxdniieeqbljtwocxy) on 2026-06-10
-- via Supabase MCP migration `enable_rls_stopgap_all_public_tables`.
--
-- Safety basis:
--   * The backend connects as the table owner (postgres); owners are exempt from
--     RLS unless FORCE ROW LEVEL SECURITY is set — so backend behavior is unchanged.
--   * Neither UI queries these tables via supabase-js/PostgREST (auth only).
--   * Net effect: the public anon key can no longer read/write these tables.
--
-- Follow-up (tracked in docs/WORKFLOW-OPTIMIZATION-HANDOFF.md item 6): per-table
-- policies with student/teacher/membership scoping + pgTAP assertions in CI.

ALTER TABLE IF EXISTS public.ai_use_reports          ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.aita_chunks             ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.aita_documents          ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.aita_search_spaces      ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_concept_problems ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_concepts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_kg_entries       ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_kg_negotiations  ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_messages         ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_misconceptions   ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_problem_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_sessions         ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_student_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.apollo_subjects         ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.chat_sessions           ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.chat_turns              ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.teacher_upload_jobs     ENABLE ROW LEVEL SECURITY;

-- ai_use_reports is the ONE table the backend reads/writes via the anon-key REST
-- client (reports/ai_use/models.py), so it needs policies once RLS is on. These
-- existed on prod only (created ad-hoc, never in a migration file) — codified here
-- and mirrored to the test project on 2026-06-10. Intentionally permissive for now;
-- tightening is part of the full RLS policy work.
DROP POLICY IF EXISTS aur_insert ON public.ai_use_reports;
DROP POLICY IF EXISTS aur_select ON public.ai_use_reports;
CREATE POLICY aur_insert ON public.ai_use_reports FOR INSERT TO anon WITH CHECK (true);
CREATE POLICY aur_select ON public.ai_use_reports FOR SELECT TO anon USING (true);


-- SOURCE: database/migrations/023_apollo_auth_scoping.sql
-- 023_apollo_auth_scoping.sql
-- Learner-model Phase 1 (spec: 2026-06-10-apollo-kg-learner-model-architecture-decision.md §8).
-- Closes the security.md "Known gaps" item: apollo_* tables keyed by loose
-- student_id TEXT with no course scoping. Re-keys apollo_sessions by
-- (user_id UUID -> auth.users, search_space_id -> aita_search_spaces) and
-- apollo_student_progress by user_id UUID (XP stays global per student).
--
-- DESTRUCTIVE: rows whose student_id is not a real auth.users UUID, or whose
-- user has no course membership to attribute a search_space_id from, are
-- dev/test artifacts and are deleted. Verified near-empty in prod before
-- applying (plan Task 1 Step 1). Rehearse on the test project first.

BEGIN;

-- ---------------------------------------------------------------------------
-- apollo_sessions: identity + course scoping
-- ---------------------------------------------------------------------------
ALTER TABLE apollo_sessions
    ADD COLUMN IF NOT EXISTS user_id UUID,
    ADD COLUMN IF NOT EXISTS search_space_id INTEGER;

-- The student UI has always sent the Supabase auth user id as student_id.
UPDATE apollo_sessions
SET user_id = student_id::uuid
WHERE user_id IS NULL
  AND student_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

-- Best-effort course attribution from the user's membership (legacy rows
-- predate course scoping; pilot users belong to one course).
UPDATE apollo_sessions s
SET search_space_id = cm.search_space_id
FROM course_memberships cm
WHERE s.search_space_id IS NULL
  AND s.user_id IS NOT NULL
  AND cm.user_id = s.user_id;

DELETE FROM apollo_sessions WHERE user_id IS NULL OR search_space_id IS NULL;

ALTER TABLE apollo_sessions
    ALTER COLUMN user_id SET NOT NULL,
    ALTER COLUMN search_space_id SET NOT NULL,
    ADD CONSTRAINT apollo_sessions_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE,
    ADD CONSTRAINT apollo_sessions_search_space_id_fkey
        FOREIGN KEY (search_space_id) REFERENCES aita_search_spaces(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS ix_apollo_sessions_user_id
    ON apollo_sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_apollo_sessions_search_space_id
    ON apollo_sessions(search_space_id);

-- One active session per student (same semantics as before, new key).
DROP INDEX IF EXISTS ix_apollo_sessions_unique_active_per_student;
CREATE UNIQUE INDEX ix_apollo_sessions_unique_active_per_user
    ON apollo_sessions(user_id) WHERE status = 'active';

DROP INDEX IF EXISTS ix_apollo_sessions_student;
ALTER TABLE apollo_sessions DROP COLUMN student_id;

-- ---------------------------------------------------------------------------
-- apollo_student_progress: re-key by auth UUID (XP stays global per student)
-- ---------------------------------------------------------------------------
ALTER TABLE apollo_student_progress
    ADD COLUMN IF NOT EXISTS user_id UUID;

UPDATE apollo_student_progress
SET user_id = student_id::uuid
WHERE user_id IS NULL
  AND student_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$';

DELETE FROM apollo_student_progress WHERE user_id IS NULL;

ALTER TABLE apollo_student_progress
    ALTER COLUMN user_id SET NOT NULL,
    DROP CONSTRAINT apollo_student_progress_pkey,
    ADD PRIMARY KEY (user_id),
    DROP COLUMN student_id;

COMMIT;


-- SOURCE: database/migrations/023_chunks_halfvec_hnsw.sql
-- 023_chunks_halfvec_hnsw.sql
-- (renumbered from 022 after merging staging, which claimed 022 for
-- 022_enable_rls_stopgap.sql)
-- RetrievalV2: make the chunk-level vector index reproducible from migrations.
--
-- 001_create_schema.py skips HNSW entirely for EMBEDDING_DIM > 2000 (it
-- predates halfvec). The production index idx_aita_chunks_embedding_hnsw was
-- created by hand; this migration codifies it so fresh environments match.
--
-- Storage strategy (same as 019_apollo_misconceptions.sql):
--   - Column stays vector(3072) (full float32 precision).
--   - HNSW expression index casts to halfvec(3072) at index time
--     (HNSW limit is 4000 dims for halfvec, 2000 for vector). 50% less
--     memory; precision loss negligible for cosine on text embeddings.
--   - Queries MUST cast BOTH operands to halfvec(3072) to match this
--     expression (see retrieval/hybrid_search.py::_halfvec_cosine_distance).
--
-- NOTE: CREATE INDEX takes a write lock on aita_chunks for the build
-- duration. Indexing is batch/offline in this system, so that is acceptable.
-- If applying to a busy production DB, run the CREATE INDEX CONCURRENTLY
-- variant outside a transaction instead.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_aita_chunks_embedding_hnsw
    ON aita_chunks
    USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

ANALYZE aita_chunks;

COMMIT;


-- SOURCE: database/migrations/024_teacher_textbook.sql
-- 024_teacher_textbook.sql
-- Allow course-wide textbook uploads in teacher_uploads.
--
-- The textbook upload path writes kind='textbook' with the COURSE_WIDE_WEEK
-- sentinel (week=0, see knowledge/teacher_weekly.py). The original 004 checks
-- only admit weekly materials (kind notes/slides, week 1..16), so the INSERT
-- raises check_violation on any database where they are live. Relax both.
--
-- MUST be applied before the textbook backend code is deployed (same
-- migration-before-code ordering as 023). Idempotent: drop-if-exists + add.
--
-- The partial unique index uniq_teacher_uploads_latest
-- (search_space_id, week, kind) WHERE is_latest stays correct with week=0
-- fixed: exactly one latest textbook per course.

BEGIN;

ALTER TABLE teacher_uploads
    DROP CONSTRAINT IF EXISTS teacher_uploads_week_check;
ALTER TABLE teacher_uploads
    ADD CONSTRAINT teacher_uploads_week_check
    CHECK (week BETWEEN 0 AND 16);

ALTER TABLE teacher_uploads
    DROP CONSTRAINT IF EXISTS teacher_uploads_kind_check;
ALTER TABLE teacher_uploads
    ADD CONSTRAINT teacher_uploads_kind_check
    CHECK (kind IN ('notes', 'slides', 'textbook'));

COMMIT;


-- SOURCE: database/migrations/025_apollo_attempt_result_values.sql
-- 025_apollo_attempt_result_values.sql
-- Widen the apollo_problem_attempts.result CHECK constraint.
--
-- Root cause (staging 500 on Apollo "I'm finished teaching", 2026-06-15):
-- migration 009 created
--     CHECK (result IS NULL OR result IN
--            ('solved','stuck','skipped','returned_to_hoot'))
-- but the code has since drifted to write two values that were never added:
--   * handle_done  -> result='graded'    (commit 21b42e1 dropped the SymPy
--                                          solver; diff+rubric is now the grade)
--   * handle_next  -> result='abandoned' (prior attempt on a mid-problem switch)
-- Every Done (and every mid-problem switch) therefore raised
-- asyncpg.CheckViolationError -> HTTP 500 after the grade had been computed.
--
-- The new allowlist is a strict SUPERSET of the old one, so it validates
-- against all existing rows with no data backfill. The set here MUST match
-- apollo.persistence.models.ATTEMPT_RESULTS (single source of truth, asserted
-- by apollo/persistence/tests/test_attempt_result_constraint.py).
--
-- MUST be applied before this code is deployed to a given environment
-- (migration-before-code ordering, same as 023/024). Idempotent:
-- drop-if-exists + add.

BEGIN;

ALTER TABLE apollo_problem_attempts
    DROP CONSTRAINT IF EXISTS apollo_problem_attempts_result_check;

ALTER TABLE apollo_problem_attempts
    ADD CONSTRAINT apollo_problem_attempts_result_check
    CHECK (
        result IS NULL
        OR result IN (
            'solved',
            'stuck',
            'skipped',
            'returned_to_hoot',
            'abandoned',
            'graded'
        )
    );

COMMIT;


-- SOURCE: database/migrations/026_apollo_learner_model.sql
-- 026_apollo_learner_model.sql
-- Apollo KG learner model (spec: docs/superpowers/specs/
--   2026-06-10-apollo-kg-learner-model-architecture-decision.md §1.4/§2/§3).
-- WU-3A: schema + ORM only. No resolver (:Canon/RESOLVES_TO — WU-3C), no seed
-- (WU-3B), no §8A runtime cutover (WU-3D).
--
-- Adds: apollo_subjects.search_space_id (course-scoping, isolation invariant §1.4);
--       Layer 1 (apollo_kg_entities, apollo_entity_prereqs);
--       Layer 3 (apollo_learner_state, apollo_mastery_events);
--       grading-core audit (apollo_graph_comparison_runs, _findings);
--       apollo_problem_attempts.learner_update_pending (§6/§7 retry flag).
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 025; 026 is the next free number.
--   * 023 is a TWO-FILE collision (023_apollo_auth_scoping.sql +
--     023_chunks_halfvec_hnsw.sql), resolved on supabase-test by distinct version
--     timestamps. Verify the target project's applied set before applying 026.
--   * 024 (teacher_textbook) + 025 (attempt result values) are NOT applied on
--     supabase-test as of 2026-06-15. PROD applied-state is UNVERIFIED — do not query
--     prod. A human/CI step rehearses on test, then prod (see plan "Deploy handoff").
--   * This file is applied to LOCAL Docker Postgres only by feller agents.
--
-- BACKFILL LANDMINE (spec §2): apollo_subjects.search_space_id lands NOT NULL on a
-- table that may already hold global rows. The migration adds it NULLABLE, backfills
-- every existing subject to a course, THEN tightens to NOT NULL + FK. A bare NOT NULL
-- ADD would fail on any populated environment.

BEGIN;

-- ===========================================================================
-- 1. apollo_subjects.search_space_id (course-scoping, two-step safe backfill)
-- ===========================================================================

-- Phase A: add nullable so the column can be populated before the constraint exists.
ALTER TABLE apollo_subjects
    ADD COLUMN IF NOT EXISTS search_space_id INTEGER;

-- Phase B: backfill existing global subjects to a course.
-- Deterministic rule: attribute each orphan subject to the LOWEST search_space id
-- that exists (the bootstrap/pilot course). This is the seam WU-3B's seeder overrides
-- with the real per-course mapping; here it guarantees NO row is left NULL so Phase C
-- can tighten. If aita_search_spaces is empty (fresh test DB with no course), the
-- UPDATE is a no-op and the (also-empty) apollo_subjects table tightens cleanly.
UPDATE apollo_subjects s
SET search_space_id = (SELECT MIN(id) FROM aita_search_spaces)
WHERE s.search_space_id IS NULL
  AND EXISTS (SELECT 1 FROM aita_search_spaces);

-- Phase C: any subject still NULL has no course to attribute to (a populated
-- apollo_subjects with an empty aita_search_spaces — an inconsistent state). Fail loud
-- rather than silently dropping curriculum: a bare SET NOT NULL below would error, but
-- this explicit guard gives a readable message.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM apollo_subjects WHERE search_space_id IS NULL) THEN
        RAISE EXCEPTION
            'apollo_subjects has rows that cannot be attributed to a course '
            '(no aita_search_spaces rows). Seed a course before applying 026.';
    END IF;
END $$;

-- Phase D: tighten + FK (isolation invariant §1.4 — ON DELETE CASCADE so deleting a
-- course removes its curriculum chain).
ALTER TABLE apollo_subjects
    ALTER COLUMN search_space_id SET NOT NULL,
    ADD CONSTRAINT apollo_subjects_search_space_id_fkey
        FOREIGN KEY (search_space_id)
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS apollo_subjects_search_space_idx
    ON apollo_subjects(search_space_id);

COMMENT ON COLUMN apollo_subjects.search_space_id IS
    'Course owning this subject (isolation invariant §1.4). Concepts/entities/'
    'problems inherit course ownership through existing FKs. Backfilled in 026; '
    'WU-3B seeder sets the real per-course mapping.';

-- ===========================================================================
-- 2. Layer 1 — apollo_kg_entities + apollo_entity_prereqs
-- ===========================================================================

CREATE TABLE IF NOT EXISTS apollo_kg_entities (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    concept_id    BIGINT NOT NULL REFERENCES apollo_concepts(id) ON DELETE CASCADE,
    canonical_key TEXT   NOT NULL,           -- 'bernoulli_principle/eq.bernoulli_full'
    kind          TEXT   NOT NULL
        CHECK (kind IN ('concept','equation','condition','definition','procedure','variable','misconception')),
    display_name  TEXT   NOT NULL,
    payload       JSONB  NOT NULL DEFAULT '{}'::jsonb,  -- symbolic form, applies_when,
                                                        -- opposes_entity_id (misconceptions)
    aliases       JSONB  NOT NULL DEFAULT '[]'::jsonb,  -- grows from the resolution log
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- PER-CONCEPT uniqueness, NOT global (§1.4): the same concept name in two courses
    -- is two distinct concepts -> two distinct entity rows. Never UNIQUE(canonical_key).
    UNIQUE (concept_id, canonical_key)
);

CREATE INDEX IF NOT EXISTS apollo_kg_entities_concept_idx
    ON apollo_kg_entities(concept_id);

COMMENT ON TABLE apollo_kg_entities IS
    'Layer-1 course-scoped skill inventory (spec §2). Course ownership inherited via '
    'concept_id -> apollo_concepts -> apollo_subjects.search_space_id. canonical_key is '
    'unique PER CONCEPT, not global (§1.4).';

CREATE TABLE IF NOT EXISTS apollo_entity_prereqs (  -- normalizes concept_dag.json
    from_entity_id BIGINT NOT NULL REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
    to_entity_id   BIGINT NOT NULL REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
    PRIMARY KEY (from_entity_id, to_entity_id)
);

COMMENT ON TABLE apollo_entity_prereqs IS
    'Prerequisite DAG edges between Layer-1 entities (spec §2). from depends on to.';

-- ===========================================================================
-- 3. Layer 3 — apollo_learner_state (current snapshot)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS apollo_learner_state (
    user_id            UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id    INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    entity_id          BIGINT  NOT NULL REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
    belief             REAL[]  NOT NULL CHECK (array_length(belief, 1) = 3),
                                          -- [p_misc, p_shaky, p_mastered]; sums-to-1 app-side
    mastery            REAL    NOT NULL CHECK (mastery >= 0 AND mastery <= 1),
    confidence         REAL    NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    misconception_code TEXT    NULL,
    evidence_count     INTEGER NOT NULL DEFAULT 0,
    last_evidence_at   TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, search_space_id, entity_id)
);

COMMENT ON TABLE apollo_learner_state IS
    'Layer-3 current per-(user,course,entity) belief snapshot, updated in place at Done '
    '(spec §3). PK enforces one row per learner per entity per course.';

-- ===========================================================================
-- 4. Layer 3 — apollo_mastery_events (append-only log)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS apollo_mastery_events (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id            UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id    INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    entity_id          BIGINT  NOT NULL REFERENCES apollo_kg_entities(id) ON DELETE CASCADE,
    attempt_id         BIGINT  REFERENCES apollo_problem_attempts(id) ON DELETE SET NULL,
    event_kind         TEXT    NOT NULL,   -- covered|missing|partial|misconception|corrected
                                           -- open enum; 'chat_question' reserved (RQ5)
    score              REAL    NULL CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
    misconception_code TEXT    NULL,
    parser_confidence  REAL    NULL CHECK (parser_confidence IS NULL OR (parser_confidence >= 0 AND parser_confidence <= 1)),
    grader_confidence  REAL    NULL CHECK (grader_confidence IS NULL OR (grader_confidence >= 0 AND grader_confidence <= 1)),
    negotiation_move   TEXT    NULL,       -- challenge|paraphrase|skip|null
    reference_step_id  TEXT    NULL,
    prior_belief       REAL[]  NOT NULL CHECK (array_length(prior_belief, 1) = 3),
    posterior_belief   REAL[]  NOT NULL CHECK (array_length(posterior_belief, 1) = 3),
    mastery_after      REAL    NOT NULL CHECK (mastery_after >= 0 AND mastery_after <= 1),
    dt_days_since_last REAL    NULL,
    evidence_node_ids  JSONB   NOT NULL DEFAULT '[]'::jsonb,  -- Neo4j bridge
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULLS NOT DISTINCT: a plain UNIQUE never conflicts on NULL attempt_id (deleted
    -- attempts; reserved chat_question events), so retries could double-insert. This
    -- treats NULL attempt_id as equal, blocking the duplicate. Belt-and-braces — the
    -- real guarantee is transactional (§3). Requires Postgres 15+ (pg16 here).
    CONSTRAINT apollo_mastery_events_attempt_entity_kind_uniq
        UNIQUE NULLS NOT DISTINCT (attempt_id, entity_id, event_kind)
);

CREATE INDEX IF NOT EXISTS apollo_mastery_events_user_entity_created_idx
    ON apollo_mastery_events (user_id, entity_id, created_at);  -- Q3 trend
CREATE INDEX IF NOT EXISTS apollo_mastery_events_entity_created_idx
    ON apollo_mastery_events (entity_id, created_at);           -- Q2 class aggregates

COMMENT ON TABLE apollo_mastery_events IS
    'Layer-3 append-only longitudinal evidence log AND refit corpus (spec §2/§3). '
    'mastery_after is the direct Q3 time series.';

-- ===========================================================================
-- 5. Grading-core audit — apollo_graph_comparison_runs + _findings
-- ===========================================================================

CREATE TABLE IF NOT EXISTS apollo_graph_comparison_runs (
    id                       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id               BIGINT  NOT NULL REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    user_id                  UUID    NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id          INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    coverage_score           REAL NOT NULL,
    soundness_score          REAL NOT NULL,
    bisimilarity_score       REAL NOT NULL,
    node_coverage_score      REAL,
    edge_coverage_score      REAL,
    scoping_score            REAL,
    usage_score              REAL,
    procedure_order_score    REAL,
    dependency_score         REAL,
    contradiction_score      REAL,
    normalization_confidence REAL NOT NULL,
    abstained                BOOLEAN NOT NULL DEFAULT false,
    abstention_reasons       JSONB   NOT NULL DEFAULT '[]'::jsonb,
    comparison_version       TEXT NOT NULL,   -- algorithm version, for replays
    reference_graph_hash     TEXT NOT NULL,   -- the reference graph AS GRADED
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Re-run at the same version SUPERSEDES (delete prior run+findings+events, re-insert
    -- in one txn, §2). Constraint guards accidental double-execution; must not crash a
    -- legitimate retry (retry deletes first).
    CONSTRAINT apollo_graph_comparison_runs_attempt_version_uniq
        UNIQUE (attempt_id, comparison_version)
);

CREATE INDEX IF NOT EXISTS apollo_graph_comparison_runs_attempt_idx
    ON apollo_graph_comparison_runs(attempt_id);

COMMENT ON TABLE apollo_graph_comparison_runs IS
    'One row per Done-time comparison; makes the grader auditable (spec §2/§6).';

CREATE TABLE IF NOT EXISTS apollo_graph_comparison_findings (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES apollo_graph_comparison_runs(id) ON DELETE CASCADE,
    entity_id          BIGINT REFERENCES apollo_kg_entities(id) ON DELETE SET NULL,
    finding_kind       TEXT NOT NULL,  -- covered_node|missing_node|matched_edge|missing_edge
                                       -- |unsupported_extra|contradiction|unresolved|alternative_path
    score              REAL,
    confidence         REAL,
    student_node_ids   JSONB NOT NULL DEFAULT '[]'::jsonb,
    reference_node_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    student_edge_ids   JSONB NOT NULL DEFAULT '[]'::jsonb,
    reference_edge_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_spans     JSONB NOT NULL DEFAULT '[]'::jsonb,  -- quoted transcript spans
    message            TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS apollo_graph_comparison_findings_run_idx
    ON apollo_graph_comparison_findings(run_id);

COMMENT ON TABLE apollo_graph_comparison_findings IS
    'Structured evidence behind every comparison score; diagnostics read THIS, not the '
    'transcript (spec §2/§6). entity_id ON DELETE SET NULL: a pruned entity must not '
    'delete the audit finding.';

-- ===========================================================================
-- 6. apollo_problem_attempts.learner_update_pending
-- ===========================================================================

-- §6/§7 cross-store retry flag: set true when the learner update is deferred (resolution
-- infra failure) so a janitor/next-session retry re-runs from resolution idempotently.
ALTER TABLE apollo_problem_attempts
    ADD COLUMN IF NOT EXISTS learner_update_pending BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN apollo_problem_attempts.learner_update_pending IS
    'True iff a Done-time learner-model update is pending retry (spec §5/§6/§7). The '
    'grade is never voided by grading-pipeline failure; this flag covers the cross-store '
    'window.';

-- ===========================================================================
-- 7. RLS stopgap (mirror migration 022): default-deny to PostgREST on the 6 new
--    public tables. NO policies — the owner-connection backend is exempt; the
--    anon/public key cannot read/write. App-layer tenant scoping (auth.py +
--    search_space_id predicate) is the enforcement point.
-- ===========================================================================

ALTER TABLE apollo_kg_entities               ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_entity_prereqs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_learner_state             ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_mastery_events            ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_graph_comparison_runs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_graph_comparison_findings ENABLE ROW LEVEL SECURITY;

COMMIT;

-- Rollback (run manually; never auto-applied):
-- BEGIN;
-- DROP TABLE IF EXISTS apollo_graph_comparison_findings;
-- DROP TABLE IF EXISTS apollo_graph_comparison_runs;
-- DROP TABLE IF EXISTS apollo_mastery_events;
-- DROP TABLE IF EXISTS apollo_learner_state;
-- DROP TABLE IF EXISTS apollo_entity_prereqs;
-- DROP TABLE IF EXISTS apollo_kg_entities;
-- ALTER TABLE apollo_problem_attempts DROP COLUMN IF EXISTS learner_update_pending;
-- ALTER TABLE apollo_subjects
--     DROP CONSTRAINT IF EXISTS apollo_subjects_search_space_id_fkey;
-- DROP INDEX IF EXISTS apollo_subjects_search_space_idx;
-- ALTER TABLE apollo_subjects DROP COLUMN IF EXISTS search_space_id;
-- COMMIT;


-- SOURCE: database/migrations/027_apollo_drop_concept_cluster_id.sql
-- 027: drop apollo_sessions.concept_cluster_id (WU-3D §8A runtime cutover).
--
-- The Apollo runtime is now fully DB-resolved + course-scoped: session_init
-- builds candidate concepts from apollo_concepts (scoped via
-- apollo_subjects.search_space_id), populates apollo_sessions.concept_id (the
-- 018 FK), and the handlers resolve concept/problems by concept_id. Nothing
-- reads or writes the legacy concept_cluster_id column anymore — Tasks 4 and 6
-- of WU-3D prove this (session_init no longer writes it; the grep guard asserts
-- the cluster map / _AVAILABLE_CLUSTERS / filesystem reads are deleted). So the
-- column is safe to drop.
--
-- Numbering caution: 026 is the highest on-disk migration; 027 is the next free
-- number. (The historical 023 two-file collision — 023_apollo_auth_scoping.sql
-- and 023_chunks_halfvec_hnsw.sql — is unrelated and does not affect 027.)
--
-- LOCAL-DOCKER-ONLY: this file is applied to local Docker Postgres by the
-- feller migration tests. It is NEVER applied to any remote Supabase project by
-- an agent — deploying (test rehearsal, then prod) is a human/CI step.
--
-- DROP COLUMN IF EXISTS makes this idempotent: re-applying it on a DB where the
-- column is already gone is a no-op.

BEGIN;

ALTER TABLE apollo_sessions DROP COLUMN IF EXISTS concept_cluster_id;

COMMIT;


-- SOURCE: database/migrations/028_apollo_learner_janitor.sql
-- 028_apollo_learner_janitor.sql  (next_free = 028; on-disk tops at 027)
-- WU-5B3a — dead-letter + exponential-backoff columns for the learner_update
-- retry janitor. NO graded_at column: the original done_ts is durably on every
-- frozen Neo4j node (store.stamp_graded_at), read back via read_node_graded_at.
-- The janitor's claim/recompute state machine (WU-5B3a-1) CONSUMES these columns;
-- WU-5B3a-0 ships the DDL + ORM mapping only (nothing reads them yet).
--
-- LOCAL-DOCKER-ONLY: applied to local Docker Postgres by the feller migration
-- tests. NEVER applied to any remote Supabase project by an agent — deploying
-- (test rehearsal, then prod) is a human/CI step (see "Deploy handoff").
--
-- Idempotent: IF NOT EXISTS guards make a re-apply on a partially-migrated local
-- DB a no-op (the chain test re-runs the whole chain into a fresh DB each session).

BEGIN;

ALTER TABLE apollo_problem_attempts
    ADD COLUMN IF NOT EXISTS learner_update_attempts           INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS learner_update_failed_at          TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS learner_update_last_error         TEXT        NULL,
    ADD COLUMN IF NOT EXISTS learner_update_next_attempt_at    TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS learner_update_failed_permanently BOOLEAN     NOT NULL DEFAULT false;

-- Cheap drain scan: a PARTIAL index over only the pending, not-dead rows. The
-- predicate uses ONLY the two boolean columns — `next_attempt_at <= now()` is NOT
-- in the predicate (now() is non-immutable / not index-predicate-legal) and is
-- applied as a RUNTIME query filter by the janitor (WU-5B3a-1).
CREATE INDEX IF NOT EXISTS apollo_problem_attempts_pending_idx
    ON apollo_problem_attempts (created_at)
    WHERE learner_update_pending AND NOT learner_update_failed_permanently;

COMMIT;

-- Rollback (LOCAL ONLY — never run against a remote DB):
-- DROP INDEX IF EXISTS apollo_problem_attempts_pending_idx;
-- ALTER TABLE apollo_problem_attempts
--     DROP COLUMN IF EXISTS learner_update_attempts,
--     DROP COLUMN IF EXISTS learner_update_failed_at,
--     DROP COLUMN IF EXISTS learner_update_last_error,
--     DROP COLUMN IF EXISTS learner_update_next_attempt_at,
--     DROP COLUMN IF EXISTS learner_update_failed_permanently;


-- SOURCE: database/migrations/029_chat_turn_keywords.sql
-- 029_chat_turn_keywords.sql
-- §10 RQ5 hedge (spec docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-
--   architecture-decision.md:1453-1466; §12 phase-5 L1534).
-- Persist the per-/ask extract_and_filter_keywords output (<=8 concept terms)
-- as ONE write-only JSONB column on chat_turns. Until now these keywords were
-- computed at request time as retrieval hints and then DISCARDED. Persisting
-- them lets months of chat history be backfilled offline as a CLASS-LEVEL
-- signal (aggregate concept coverage across a course) -- never a hard
-- per-student negative, and with NO read/consumer path in v1 (write-only).
--
-- Safe / zero-downtime: ADD COLUMN with a constant server default is a
-- metadata-only catalog change on Postgres 11+ (no full-table rewrite). NOT NULL
-- is safe because a non-volatile DEFAULT is supplied in the same statement.
-- IF NOT EXISTS makes re-application a no-op (idempotent).

BEGIN;

ALTER TABLE chat_turns
    ADD COLUMN IF NOT EXISTS keywords JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;

-- Rollback for 029_chat_turn_keywords.sql:
-- BEGIN;
-- ALTER TABLE chat_turns DROP COLUMN IF EXISTS keywords;
-- COMMIT;


-- SOURCE: database/migrations/030_apollo_autoprovisioning.sql
-- 030_apollo_autoprovisioning.sql
-- §8B materials -> Apollo auto-provisioning substrate (WU-3B2a).
-- Spec: docs/superpowers/specs/2026-06-10-apollo-kg-learner-model-architecture-decision.md §8B.
-- Adjudicated DDL: docs/superpowers/plans/2026-06-19-apollo-kg-wu3b2-split-proposal.md
--   '### WU-3B2a' + 'ORCHESTRATOR ADJUDICATION (2026-06-19)' decisions #3/#6/#11.
--
-- This unit ships SCHEMA + ORM + the Tier-2 selection gate ONLY. No pipeline/LLM logic.
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 029_chat_turn_keywords.sql; 030 is the next free number.
--   * This file is applied to LOCAL Docker Postgres ONLY by feller agents. Rehearsal on the
--     TEST Supabase project then prod is a human/CI step (see plan "Deploy handoff").
--
-- TWO BACKFILLS (both inside this migration):
--   (a) denormalize search_space_id onto apollo_concept_problems via the concept->subject join.
--   (b) CRITICAL — flip every existing (seeded) tier=1 row to tier=2/'authored' so the live
--       bernoulli pool stays teachable the instant the tier filter ships. Highest blast radius.
--
-- ROLLBACK CAVEAT (see foot of file): dropping `tier` loses the tier=1/tier=2 distinction;
-- backfill (b) is NOT separately reversible — after rollback every problem is teachable again
-- (the pre-030 behavior), which is the safe direction. The search_space_id denormalization is a
-- copy and is dropped cleanly (the authoritative scope is still reachable via concept->subject).

BEGIN;
-- 1. apollo_concept_problems: tier/solution_source/provenance/quarantine + denormalized scope.
ALTER TABLE apollo_concept_problems
  ADD COLUMN IF NOT EXISTS tier            SMALLINT NOT NULL DEFAULT 1
      CHECK (tier IN (1, 2)),                              -- 1=inventory (not teachable), 2=teachable
  ADD COLUMN IF NOT EXISTS solution_source TEXT
      CHECK (solution_source IS NULL OR solution_source IN ('extracted','generated','authored')),
  ADD COLUMN IF NOT EXISTS provenance      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {document_id,page,chunk_content_hash}
  ADD COLUMN IF NOT EXISTS quarantined_at  TIMESTAMPTZ,                          -- §8B.3c anomaly quarantine (NULL = live); FILTER added in WU-3B2h, NOT here
  ADD COLUMN IF NOT EXISTS search_space_id INTEGER;                             -- denormalized course scope (ADJUDICATION #3: NULLABLE here)
-- backfill (a): denormalize the course scope from the concept->subject chain.
UPDATE apollo_concept_problems p SET search_space_id = sub.search_space_id
  FROM apollo_concepts c JOIN apollo_subjects sub ON sub.id = c.subject_id
  WHERE p.concept_id = c.id AND p.search_space_id IS NULL;
-- backfill (b) CRITICAL: existing §8-seeded rows ARE teachable -> tier=2, solution_source='authored'.
UPDATE apollo_concept_problems SET tier = 2, solution_source = 'authored' WHERE tier = 1;
CREATE INDEX IF NOT EXISTS apollo_concept_problems_concept_tier_idx
  ON apollo_concept_problems(concept_id, tier);

-- 1b. apollo_kg_entities: scope_summary text (the dedup embedding SOURCE, embedded on the fly; 3B2c).
ALTER TABLE apollo_kg_entities
  ADD COLUMN IF NOT EXISTS scope_summary TEXT;  -- nullable; authored at mint; NO persisted vector (no pgvector migration)

-- 2. apollo_ingest_runs — per-doc counts + LLM call/token/cost aggregates + content_hash + status.
CREATE TABLE IF NOT EXISTS apollo_ingest_runs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT  NOT NULL,
  content_hash      TEXT,                 -- document content hash; an unchanged re-upload short-circuits (3B2g)
  status            TEXT    NOT NULL DEFAULT 'queued'
      CHECK (status IN ('queued','running','succeeded','failed')),
  n_questions_scraped INTEGER NOT NULL DEFAULT 0,
  n_promoted        INTEGER NOT NULL DEFAULT 0,
  n_rejected        INTEGER NOT NULL DEFAULT 0,
  n_dedup_merged    INTEGER NOT NULL DEFAULT 0,
  llm_calls         INTEGER NOT NULL DEFAULT 0,
  llm_tokens_in     BIGINT  NOT NULL DEFAULT 0,
  llm_tokens_out    BIGINT  NOT NULL DEFAULT 0,
  llm_cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_runs_space_doc_idx ON apollo_ingest_runs(search_space_id, document_id);
-- a succeeded run per (document_id, content_hash) lets enqueue short-circuit an unchanged re-upload:
CREATE INDEX IF NOT EXISTS apollo_ingest_runs_doc_hash_idx ON apollo_ingest_runs(document_id, content_hash);

-- 3. apollo_provisioning_jobs — the SKIP LOCKED work queue (mirrors TeacherUploadJob lease shape).
CREATE TABLE IF NOT EXISTS apollo_provisioning_jobs (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT  NOT NULL,
  state             TEXT    NOT NULL DEFAULT 'pending'
      CHECK (state IN ('pending','running','completed','failed')),
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE SET NULL,
  lease_owner       TEXT,
  lease_expires_at  TIMESTAMPTZ,
  attempt_count     INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- idempotent enqueue: at most one OPEN job per document (partial-unique-index idiom).
-- JOB-level dedup; the (document_id, chunk_content_hash) no-op is a SEPARATE intra-job guarantee (3B2g).
CREATE UNIQUE INDEX IF NOT EXISTS apollo_provisioning_jobs_open_uniq
  ON apollo_provisioning_jobs(document_id) WHERE state IN ('pending','running');

-- 4. apollo_rejected_problems — a gate failure + diagnostic + the rejected payload.
CREATE TABLE IF NOT EXISTS apollo_rejected_problems (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  concept_id        BIGINT  REFERENCES apollo_concepts(id) ON DELETE SET NULL,  -- NULL if rejected pre-tag
  failed_gate       SMALLINT,            -- 1..8 (NULL if rejected at the pairing gate, not a lint gate)
  rejected_stage    TEXT    NOT NULL,    -- 'pairing_gate' | 'promotion_lint'
  diagnostic        TEXT    NOT NULL,
  payload           JSONB   NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_rejected_problems_run_idx ON apollo_rejected_problems(ingest_run_id);

-- 5. apollo_dedup_decisions — method + similarity + verdict for every dedup resolution.
CREATE TABLE IF NOT EXISTS apollo_dedup_decisions (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  concept_id        BIGINT  REFERENCES apollo_concepts(id) ON DELETE SET NULL,
  candidate_key     TEXT    NOT NULL,
  method            TEXT    NOT NULL CHECK (method IN ('slug','embedding','llm_judge')),
  similarity        REAL,                -- NULL for the slug exact-match tier
  verdict           TEXT    NOT NULL CHECK (verdict IN ('merged','distinct')),
  matched_entity_id BIGINT  REFERENCES apollo_kg_entities(id) ON DELETE SET NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_dedup_decisions_run_idx ON apollo_dedup_decisions(ingest_run_id);

-- 6. apollo_ingest_errors — stage + class + context for any non-terminal pipeline error.
CREATE TABLE IF NOT EXISTS apollo_ingest_errors (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  stage             TEXT    NOT NULL,    -- scrape|find_or_generate|pairing|tag_mint|dedup|promotion
  error_class       TEXT    NOT NULL,
  context           JSONB   NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_errors_run_idx ON apollo_ingest_errors(ingest_run_id);

-- 7. RLS stopgap (mirror 026 §7 / migration 022): default-deny to PostgREST on the 5 new tables.
ALTER TABLE apollo_ingest_runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_provisioning_jobs  ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_rejected_problems  ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_dedup_decisions    ENABLE ROW LEVEL SECURITY;
ALTER TABLE apollo_ingest_errors      ENABLE ROW LEVEL SECURITY;
COMMIT;
-- Rollback (run manually; never auto-applied):
-- BEGIN; DROP TABLE IF EXISTS apollo_ingest_errors, apollo_dedup_decisions, apollo_rejected_problems,
--   apollo_provisioning_jobs, apollo_ingest_runs;
-- ALTER TABLE apollo_kg_entities DROP COLUMN IF EXISTS scope_summary;
-- DROP INDEX IF EXISTS apollo_concept_problems_concept_tier_idx;
-- ALTER TABLE apollo_concept_problems DROP COLUMN IF EXISTS search_space_id, DROP COLUMN IF EXISTS quarantined_at,
--   DROP COLUMN IF EXISTS provenance, DROP COLUMN IF EXISTS solution_source, DROP COLUMN IF EXISTS tier; COMMIT;


-- SOURCE: database/migrations/031_apollo_soundness_applicable.sql
-- 031_apollo_soundness_applicable.sql
-- D5/D6 — soundness N/A on an empty misconception bank.
-- Design: docs/design/2026-06-23-apollo-soundness-na-sentinel.md
--
-- Adds a single authoritative flag distinguishing a VERIFIED-sound score from a
-- never-checked one (the bank was empty/absent for the concept). When false:
--   * soundness_score / bisimilarity_score hold the COVERAGE-ONLY fallback value
--     (NOT NULL kept; the harmonic mean renormalizes to coverage, never 0.0/1.0);
--   * contradiction_score is NULL (already nullable; no change needed).
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 030_apollo_autoprovisioning.sql; 031 is next free.
--   * Applied to LOCAL Docker Postgres ONLY by agents. Rehearsal on the TEST
--     Supabase project then prod is a human/CI step.
--
-- ROLLBACK: dropping soundness_applicable loses the verified-vs-N/A distinction;
-- the numeric columns are unaffected (they always held an in-range scalar). Safe
-- direction: after rollback every row reads as if applicable (pre-031 behavior).

BEGIN;

ALTER TABLE apollo_graph_comparison_runs
    ADD COLUMN IF NOT EXISTS soundness_applicable BOOLEAN NOT NULL DEFAULT true;

COMMENT ON COLUMN apollo_graph_comparison_runs.soundness_applicable IS
    'D5/D6: false iff the misconception bank was empty/absent for the concept; '
    'then soundness_score/bisimilarity_score are the coverage-only fallback and '
    'contradiction_score is NULL. Readers must check this before trusting soundness.';

COMMIT;


-- SOURCE: database/migrations/032_apollo_authored_sets.sql
-- 032_apollo_authored_sets.sql — paired authored problem/solution sets (WU-AAS).
BEGIN;

CREATE TABLE IF NOT EXISTS apollo_authored_sets (
    id                    BIGSERIAL PRIMARY KEY,
    search_space_id       INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    set_index             INTEGER NOT NULL,
    problem_document_id   BIGINT,
    solution_document_id  BIGINT,
    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','indexing','provisioning','done','failed')),
    result_summary        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (search_space_id, set_index)
);

CREATE INDEX IF NOT EXISTS apollo_authored_sets_space_idx
    ON apollo_authored_sets(search_space_id);

-- RLS parity with every sibling Apollo table: enable the deny-all stopgap so the
-- table is not exposed via the public PostgREST API. No policies are added — the
-- backend connects through the service/postgres role, which bypasses RLS.
ALTER TABLE apollo_authored_sets ENABLE ROW LEVEL SECURITY;

COMMIT;


-- SOURCE: database/migrations/033_apollo_clarifications.sql
-- 033_apollo_clarifications.sql
-- Apollo clarification loop (G2): one row per ambiguous student idea Apollo
-- probed with an answer-blind follow-up. State machine:
--   asked_waiting -> {confirmed | refuted | vague}  (terminal; one probe per idea)
-- A `confirmed` row resolves the node at grading via the `clarification` method
-- (cap 0.90). A `refuted` row is misconception evidence (no credit). RLS follows
-- the Apollo default-deny stopgap (mirror 022/026): ENABLE, no policies, the
-- owner-connection backend is exempt; app-layer scoping is search_space_id.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_clarifications (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id         BIGINT NOT NULL
        REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    session_id         BIGINT NOT NULL
        REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    user_id            UUID NOT NULL
        REFERENCES auth.users(id) ON DELETE CASCADE,
    search_space_id    INTEGER NOT NULL
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    concept_id         BIGINT
        REFERENCES apollo_concepts(id) ON DELETE SET NULL,
    node_id            TEXT NOT NULL,
    candidate_key      TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'asked_waiting'
        CHECK (state IN ('asked_waiting', 'confirmed', 'refuted', 'vague')),
    probe_question     TEXT NOT NULL,
    original_statement TEXT NOT NULL,
    clarification_text TEXT,
    asked_turn         INTEGER NOT NULL,
    answered_turn      INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One follow-up per idea per attempt (spec §10 state machine).
    CONSTRAINT apollo_clarifications_attempt_node_uniq UNIQUE (attempt_id, node_id)
);

CREATE INDEX IF NOT EXISTS apollo_clarifications_attempt_state_idx
    ON apollo_clarifications(attempt_id, state);

CREATE INDEX IF NOT EXISTS apollo_clarifications_user_concept_idx
    ON apollo_clarifications(user_id, concept_id);

-- RLS stopgap (mirror migrations 022/026): default-deny to PostgREST, no
-- policies. The owner-connection backend is exempt; the anon/public key cannot
-- read/write. App-layer tenant scoping (auth.py + search_space_id) enforces.
ALTER TABLE apollo_clarifications ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_clarifications IS
    'Apollo clarification loop (G2): one row per ambiguous student idea probed '
    'with an answer-blind follow-up; confirmed rows resolve at grading via the '
    'clarification method (0.90), refuted rows are misconception evidence.';

COMMIT;


-- SOURCE: database/migrations/034_apollo_grading_artifacts.sql
-- 034_apollo_grading_artifacts.sql
-- Canonical grading artifact (spec 2026-07-01 §1): ONE immutable record per
-- Done-click per grader role. role='canonical' is the record of the grade the
-- student was served; role='pair' is the other grader's artifact captured on
-- the same input (paired-artifact contract, spec §5). Append-only: no UPDATE
-- path exists in code; retuning weights affects future artifacts only.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_grading_artifacts (
    id BIGSERIAL PRIMARY KEY,
    attempt_id BIGINT NOT NULL
        REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('canonical', 'pair')),
    grader_used TEXT NOT NULL CHECK (grader_used IN ('graph', 'llm_fallback')),
    user_id UUID NOT NULL,
    search_space_id BIGINT NOT NULL
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    concept_id BIGINT
        REFERENCES apollo_concepts(id) ON DELETE SET NULL,
    problem_id TEXT NOT NULL,
    versions JSONB NOT NULL,            -- {grader, reference_graph_hash, nli_model, weights_version}
    node_ledger JSONB NOT NULL,         -- [{key, status, method, confidence, evidence_span}]
    edge_ledger JSONB NOT NULL,         -- [{key, edge_type, status, method, confidence, evidence_span}]
    misconceptions JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{key, utterance, confidence}]
    clarification_trace JSONB NOT NULL DEFAULT '[]'::jsonb,
    scores JSONB NOT NULL,              -- {node_coverage, edge_coverage, misconception_penalty,
                                        --  composite, weights:{w_n,w_e,p}}
    abstention JSONB,                   -- null OR {reasons:[...], llm_fallback_grade,
                                        --  graph_failure: str|null}
    grading_latency_ms INTEGER,         -- Done-click grading latency (ops gate input)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_grading_artifact_attempt_role UNIQUE (attempt_id, role)
);

CREATE INDEX IF NOT EXISTS ix_grading_artifacts_space_concept_time
    ON apollo_grading_artifacts (search_space_id, concept_id, created_at);
CREATE INDEX IF NOT EXISTS ix_grading_artifacts_user
    ON apollo_grading_artifacts (user_id, created_at);

-- RLS stopgap (mirror migrations 022/026/033): default-deny to PostgREST, no
-- policies. The owner-connection backend is exempt; the anon/public key cannot
-- read/write. App-layer tenant scoping (auth.py + search_space_id) enforces.
ALTER TABLE apollo_grading_artifacts ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_grading_artifacts IS
    'Canonical grading artifact (spec 2026-07-01 §1): one immutable row per '
    'Done-click per grader role. role=canonical is what the student was '
    'served; role=pair is the other graders artifact captured on the same '
    'input (paired-artifact contract, spec §5). Append-only.';

COMMIT;


-- SOURCE: database/migrations/035_apollo_learner_state_space_idx.sql
-- 035_apollo_learner_state_space_idx.sql
-- Campaign-plan Task B3: the classroom mastery-heatmap projection scans every
-- apollo_learner_state row for a course (search_space_id), then joins to
-- apollo_kg_entities for the concept grouping. apollo_learner_state's PRIMARY
-- KEY is (user_id, search_space_id, entity_id) -- user_id leads, so a
-- course-wide scan cannot use the PK index. Mirrors migration 034's
-- ix_grading_artifacts_space_concept_time shape for apollo_grading_artifacts.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

CREATE INDEX IF NOT EXISTS ix_apollo_learner_state_space_entity
    ON apollo_learner_state (search_space_id, entity_id);

COMMIT;


-- SOURCE: database/migrations/036_apollo_ingest_page_evidence.sql
-- 036_apollo_ingest_page_evidence.sql
-- WU-AAS ingestion observability (lane B2.4 / G4.4).
-- The authored-sets ingestion path never wrote apollo_ingest_runs /
-- apollo_ingest_errors (migration 030) and persisted NO per-page OCR text, so the
-- S2 ingestion audit ran on thin/absent inputs. This unit adds:
--   (a) apollo_ingest_runs.n_pages — the page count for a run (runs are now
--       written per authored-set ingestion, not only by the queue worker);
--   (b) apollo_ingest_page_evidence — one row per source page per run carrying the
--       recognized OCR text + self-reported confidence + extraction mode +
--       verify-path flag, the audit-facing projection of the transient OCR pass.
--
-- DEPLOY-TIME RECONCILIATION (read before applying anywhere — DO NOT auto-apply):
--   * On-disk migrations top out at 035_apollo_learner_state_space_idx.sql; 036 is
--     the next free number.
--   * Applied to LOCAL Docker Postgres ONLY by agents. Rehearsal on the TEST
--     Supabase project then prod is a human/CI step.
--
-- Guarded DDL (CREATE ... IF NOT EXISTS / ADD COLUMN IF NOT EXISTS) so a re-apply
-- is a no-op, mirroring every migration in this tree.

BEGIN;

-- 1. apollo_ingest_runs: page count for a run (authored-set ingestion writes it).
ALTER TABLE apollo_ingest_runs
  ADD COLUMN IF NOT EXISTS n_pages INTEGER NOT NULL DEFAULT 0;

-- 1b. apollo_ingest_runs.document_id → NULLABLE. The authored-set path opens the
--     run BEFORE indexing so an OCR/indexing failure (bad PDF, no chunks produced)
--     still leaves a run row + error, instead of both observability tables staying
--     empty. At run-open no document has been minted yet, so the column must allow
--     NULL until problem indexing succeeds. The queue worker path always sets it.
--     Idempotent: DROP NOT NULL is a no-op if the column is already nullable.
ALTER TABLE apollo_ingest_runs
  ALTER COLUMN document_id DROP NOT NULL;

-- 2. apollo_ingest_page_evidence — per-page OCR text + confidence + verify flag.
--    document_id is NULLABLE: a page captured before the failure path minted a
--    document (a page-bearing-but-chunkless PDF) still gets its evidence persisted.
CREATE TABLE IF NOT EXISTS apollo_ingest_page_evidence (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ingest_run_id     BIGINT  NOT NULL REFERENCES apollo_ingest_runs(id) ON DELETE CASCADE,
  search_space_id   INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
  document_id       BIGINT,                    -- hidden authored doc (NULL if indexing failed pre-mint)
  role              TEXT    NOT NULL,          -- 'problem' | 'solution' (open enum)
  page_number       INTEGER,                   -- source page (NULL if the page carried none)
  ocr_text          TEXT    NOT NULL DEFAULT '',  -- recognized text/LaTeX for the page
  ocr_confidence    REAL,                      -- self-reported OCR confidence [0,1] (NULL if native)
  extraction_mode   TEXT,                      -- 'native' | 'ocr' | vendor-specific
  verify_path_fired BOOLEAN NOT NULL DEFAULT false,  -- page tripped the low-confidence threshold
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS apollo_ingest_page_evidence_run_idx
  ON apollo_ingest_page_evidence(ingest_run_id);
CREATE INDEX IF NOT EXISTS apollo_ingest_page_evidence_doc_idx
  ON apollo_ingest_page_evidence(document_id);

-- 3. RLS stopgap (mirror migration 030 §7): default-deny to PostgREST.
ALTER TABLE apollo_ingest_page_evidence ENABLE ROW LEVEL SECURITY;

COMMIT;
-- Rollback (run manually; never auto-applied):
-- BEGIN; DROP TABLE IF EXISTS apollo_ingest_page_evidence;
-- ALTER TABLE apollo_ingest_runs DROP COLUMN IF EXISTS n_pages;
-- -- Re-tighten document_id ONLY if no run row has a NULL document_id:
-- -- ALTER TABLE apollo_ingest_runs ALTER COLUMN document_id SET NOT NULL;
-- COMMIT;


-- SOURCE: database/migrations/037_apollo_misconception_observations.sql
-- 037_apollo_misconception_observations.sql
-- Emergent per-class misconception store (design memo 2026-07-05, increment 1;
-- lane B3b/D1). Append-only observation ledger: ONE row per
-- (attempt_id, signature) derived from a role='canonical' grading artifact's
-- asserted misconceptions, written only when the APOLLO_EMERGENT_MISCONCEPTIONS
-- flag is ON. The ledger is the source of truth; trust is derived on read
-- (apollo/emergent/store.py), never materialized here (increment 2 defers the
-- rolled-up bank + teacher-curation columns).
--
-- Numbering: on-disk max was 035; 036 is claimed by a sibling lane's PR, so
-- this store takes 037 (the memo proposed 036 pre-collision).
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.

BEGIN;

CREATE TABLE IF NOT EXISTS apollo_misconception_observations (
    id BIGSERIAL PRIMARY KEY,
    search_space_id BIGINT NOT NULL
        REFERENCES aita_search_spaces(id) ON DELETE CASCADE,   -- P5 per-class isolation
    concept_id BIGINT
        REFERENCES apollo_concepts(id) ON DELETE SET NULL,
    signature TEXT NOT NULL,            -- canonical_key OR 'unkeyed:<concept_id>' bucket
    user_id UUID NOT NULL,              -- distinct-student counting (trust support term)
    attempt_id BIGINT
        REFERENCES apollo_problem_attempts(id) ON DELETE SET NULL,  -- log outlives the attempt
    confidence REAL,                    -- resolver confidence of the asserting finding
    opposes TEXT,                       -- reference node the misconception contradicts (or NULL)
    evidence_span TEXT,                 -- representative student utterance
    source TEXT NOT NULL DEFAULT 'grading_artifact',  -- future: 'clarification_refuted'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- P6 idempotency: a re-grade / retry of the same attempt cannot double-count.
    -- Mirrors apollo_mastery_events' (attempt_id, ...) UNIQUE. Only role=canonical
    -- artifacts feed this table, so (attempt_id, signature) is the per-attempt key.
    CONSTRAINT uq_misconception_observation_attempt_signature
        UNIQUE (attempt_id, signature)
);

-- Read path scans one class+concept and GROUPs BY signature; the PK leads with
-- id so it cannot serve that scan. Mirrors ix_grading_artifacts_space_concept_time.
CREATE INDEX IF NOT EXISTS ix_apollo_misconception_obs_space_concept
    ON apollo_misconception_observations (search_space_id, concept_id);

-- RLS stopgap (mirror migrations 022/026/033/034): default-deny to PostgREST,
-- no policies. The owner-connection backend is exempt; the anon/public key
-- cannot read/write. App-layer tenant scoping (auth.py + search_space_id)
-- enforces tenancy.
ALTER TABLE apollo_misconception_observations ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_misconception_observations IS
    'Emergent per-class misconception store (memo 2026-07-05, increment 1): '
    'append-only observation ledger, one row per (attempt_id, signature) from '
    'role=canonical grading artifacts. Written only when '
    'APOLLO_EMERGENT_MISCONCEPTIONS is ON. Trust is derived on read, not stored. '
    'Append-only; the source of truth for a rebuildable increment-2 bank.';

COMMIT;


-- SOURCE: database/migrations/038_apollo_concept_description.sql
-- 038: reversed-provisioning concept matching needs a human-readable
-- description per registered concept (the closed-list matcher prompt is
-- "slug — display_name: description"; protocol measured at 95-96.7% in
-- apollo/provisioning/corpora/calc2/authored/results_authored.md).
-- Additive; default '' keeps every existing row and seeder valid.
ALTER TABLE apollo_concepts
    ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';


-- SOURCE: database/migrations/039_apollo_misconception_opposes.sql
-- 039_apollo_misconception_opposes.sql
-- F-struct (structural co-key): add the per-entry `opposes` link to the
-- RUNTIME misconception bank (apollo_misconceptions, migration 019). The link
-- already exists in the on-disk misconceptions.json source and in the emergent
-- store (apollo_misconception_observations.opposes, migration 037) + the
-- apollo_kg_entities opposes_entity_key payload — only the detector's bank
-- lacked it. Nullable: most banks have no opposes; NULL means "no structural
-- scope" and the structural co-key gate path never fires for that entry.
--
-- Numbering: on-disk max was 038 (038_apollo_concept_description.sql on staging); this takes 039.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.
--
-- ROLLBACK: dropping opposes loses the structural co-key link; the bank's
-- existing embedding-match path is unaffected (opposes is read only by the
-- F-struct gate path). Safe direction: after rollback every entry reads as
-- "no structural scope" (pre-039 behavior).

BEGIN;

ALTER TABLE apollo_misconceptions
    ADD COLUMN IF NOT EXISTS opposes TEXT;

COMMENT ON COLUMN apollo_misconceptions.opposes IS
    'F-struct: canonical entity_key of the reference node this misconception '
    'contradicts (e.g. def.real_basis), or NULL. Seeded from misconceptions.json '
    '"opposes". Read by the structural co-key gate path to NAME a misconception '
    'the judge only localized.';

COMMIT;


-- SOURCE: database/migrations/040_apollo_misconception_obs_source_domain.sql
-- 040_apollo_misconception_obs_source_domain.sql
-- Emergent misconception map (design 2026-07-10, §5.3/§5.4): enforce the
-- `source` domain on apollo_misconception_observations (migration 037).
-- `source` was plain TEXT DEFAULT 'grading_artifact' with no CHECK; this adds
-- one enumerating the three write-path values: the pre-existing
-- 'grading_artifact' (role=canonical grading-artifact ledger feed, unchanged)
-- plus the two new emergent-map capture seams' sources, 'detector_unkeyed'
-- (judge confidently wrong at a keyed reference node it cannot name — the
-- birth signal) and 'clarification_refuted' (a clarification rescore that
-- confirms the misconception — the upgrade signal). No behavior change to
-- any existing write: 'grading_artifact' is already the only value ever
-- written on staging/prod.
--
-- Numbering: on-disk max was 039 (039_apollo_misconception_opposes.sql); this
-- takes 040.
--
-- DEPLOY-TIME RECONCILIATION: numbered migration; agents apply to LOCAL Docker
-- Postgres only. Test-project rehearsal then prod is a human/CI step. DO NOT
-- auto-apply to any remote Supabase project.
--
-- ROLLBACK: dropping the constraint (ck_misconception_obs_source) restores the
-- unconstrained TEXT column; no data is lost or rewritten either direction.

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_misconception_obs_source'
    ) THEN
        ALTER TABLE apollo_misconception_observations
            ADD CONSTRAINT ck_misconception_obs_source
            CHECK (source IN ('grading_artifact', 'detector_unkeyed', 'clarification_refuted'));
    END IF;
END $$;

COMMENT ON COLUMN apollo_misconception_observations.source IS
    'Observation origin: grading_artifact (role=canonical Done-grade ledger '
    'feed, migration 037, unchanged) | detector_unkeyed (emergent-map birth '
    'signal — judge confidently wrong at a keyed reference node it cannot '
    'name) | clarification_refuted (emergent-map upgrade signal — a '
    'clarification rescore confirms the misconception). Enumerated by '
    'ck_misconception_obs_source (migration 040).';

COMMIT;


-- SOURCE: database/migrations/041_apollo_grading_artifact_llm_transcript.sql
-- Part 1 transcript grader: make the future canonical artifact value legal.
-- File-only migration; artifact writing remains on graph/llm_fallback until calibration passes.
BEGIN;
ALTER TABLE apollo_grading_artifacts
    DROP CONSTRAINT IF EXISTS apollo_grading_artifacts_grader_used_check;
ALTER TABLE apollo_grading_artifacts
    ADD CONSTRAINT apollo_grading_artifacts_grader_used_check
    CHECK (grader_used IN ('graph', 'llm_fallback', 'llm_transcript'));
COMMIT;


-- SOURCE: database/migrations/042_apollo_reference_question_opportunities.sql
BEGIN;

CREATE TABLE IF NOT EXISTS apollo_reference_question_opportunities (
    id BIGSERIAL PRIMARY KEY,
    attempt_id BIGINT NOT NULL REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    session_id BIGINT NOT NULL REFERENCES apollo_sessions(id) ON DELETE CASCADE,
    reference_node_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'asked_waiting'
        CHECK (state IN ('asked_waiting', 'answered')),
    question TEXT NOT NULL,
    asked_turn INTEGER NOT NULL,
    answered_turn INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT apollo_reference_question_opportunities_attempt_node_uniq
        UNIQUE (attempt_id, reference_node_id)
);

CREATE INDEX IF NOT EXISTS apollo_reference_question_opportunities_attempt_state_idx
    ON apollo_reference_question_opportunities(attempt_id, state);

ALTER TABLE apollo_reference_question_opportunities ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_reference_question_opportunities IS
    'One answer-blind Apollo question opportunity per authored reference node and attempt.';

COMMIT;


-- SOURCE: database/migrations/043_apollo_dedup_pressure.sql
-- DAG-1: persist the mint-time dedup-pressure gauge on each ingestion run.
-- Idempotent additive DDL; remote application remains a human/CI step.
-- Default is the empty object (mirrors the ORM server_default); readers
-- .get() every key, and ORM inserts populate the zeroed gauge shape.

ALTER TABLE apollo_ingest_runs
    ADD COLUMN IF NOT EXISTS dedup_pressure JSONB NOT NULL DEFAULT '{}'::jsonb;


-- SOURCE: database/migrations/044_apollo_generation_runs.sql
-- GEN-4: teacher-initiated problem-generation batch tracking.
BEGIN;

CREATE TABLE IF NOT EXISTS apollo_generation_runs (
    id                  BIGSERIAL PRIMARY KEY,
    search_space_id     INTEGER NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    concept_id          BIGINT NOT NULL REFERENCES apollo_concepts(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','succeeded','failed')),
    result_summary      JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingest_run_id       BIGINT REFERENCES apollo_ingest_runs(id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS apollo_generation_runs_space_idx
    ON apollo_generation_runs(search_space_id);

CREATE INDEX IF NOT EXISTS apollo_generation_runs_concept_idx
    ON apollo_generation_runs(concept_id);

ALTER TABLE apollo_generation_runs ENABLE ROW LEVEL SECURITY;

COMMIT;


-- SOURCE: database/migrations/045_apollo_mastery_event_problem_link.sql
BEGIN;

ALTER TABLE apollo_mastery_events
    ADD COLUMN IF NOT EXISTS concept_problem_id BIGINT
        REFERENCES apollo_concept_problems(id) ON DELETE SET NULL;

COMMENT ON COLUMN apollo_mastery_events.concept_problem_id IS
    'Problem-bank row this outcome came from (resolved at write time from '
    '(session.concept_id, attempt.problem_code)); NULL when unresolvable. '
    'The per-item difficulty-calibration key (GEN-5).';

CREATE INDEX IF NOT EXISTS apollo_mastery_events_problem_created_idx
    ON apollo_mastery_events (concept_problem_id, created_at)
    WHERE concept_problem_id IS NOT NULL;

COMMIT;


-- SOURCE: database/migrations/046_apollo_solution_source_llm_paired.sql
-- Authored-set structure pairing: preserve LLM-paired solution provenance.
--
-- Migration 030 introduced the solution_source domain as an inline CHECK,
-- which PostgreSQL named apollo_concept_problems_solution_source_check.
-- Structure-paired references pass the same verification and pairing gates as
-- extracted references, but retain their distinct origin through promotion.
--
-- Guarded on column existence: the migration-036 test harness replays every
-- migration that touches apollo_concept_problems BEFORE 030 (which mints the
-- column), so this must be a no-op until 030 has run. Real chains apply in
-- numeric order and always take the guarded ALTER path.

BEGIN;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'apollo_concept_problems'
          AND column_name = 'solution_source'
    ) THEN
        ALTER TABLE apollo_concept_problems
            DROP CONSTRAINT IF EXISTS apollo_concept_problems_solution_source_check;

        ALTER TABLE apollo_concept_problems
            ADD CONSTRAINT apollo_concept_problems_solution_source_check
            CHECK (
                solution_source IS NULL
                OR solution_source IN ('extracted', 'generated', 'authored', 'llm_paired')
            );
    END IF;
END $$;

COMMIT;


-- SOURCE: database/migrations/047_apollo_question_tally.sql
BEGIN;

CREATE TABLE IF NOT EXISTS apollo_question_tally (
    id BIGSERIAL PRIMARY KEY,
    attempt_id BIGINT NOT NULL REFERENCES apollo_problem_attempts(id) ON DELETE CASCADE,
    reference_node_id TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '[]',
    student_declined BOOLEAN NOT NULL DEFAULT FALSE,
    times_asked INTEGER NOT NULL DEFAULT 0,
    last_asked_turn INTEGER,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, reference_node_id)
);

CREATE INDEX IF NOT EXISTS ix_apollo_question_tally_attempt
    ON apollo_question_tally (attempt_id);

ALTER TABLE apollo_question_tally ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE apollo_question_tally IS
    'Durable per-attempt memory for Apollo reference-node questioning judgments.';

COMMIT;
