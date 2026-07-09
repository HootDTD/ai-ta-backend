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
