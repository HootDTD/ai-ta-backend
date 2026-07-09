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
