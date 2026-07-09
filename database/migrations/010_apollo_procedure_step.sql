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
