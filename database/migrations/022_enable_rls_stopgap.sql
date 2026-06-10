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
