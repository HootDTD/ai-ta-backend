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
