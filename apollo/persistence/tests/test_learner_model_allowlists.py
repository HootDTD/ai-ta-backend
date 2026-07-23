"""Drift guard: app-layer allowlists agree with migration 026 (frozen legacy
chain — file-level DDL sanity only, per DB-03), plus ENTITY_KINDS agreement
with the DB-13 target DDL. migration 026 is FROZEN (database/migrations/
README.md); it is no longer the schema authority for ENTITY_KINDS — see
supabase/migrations/20260717035041_create_app_schema_v1.sql.

Mirrors ``test_attempt_result_values.py`` (migration<->model agreement, no DB
needed). ``ENTITY_KINDS`` is the one tuple tied to a SQL CHECK (on
``app.learner_entities.kind`` in the target DDL — migration 026's frozen
``apollo_kg_entities.kind`` CHECK still admits 'misconception', which DB-13/A6
intentionally dropped from the target) and is asserted equal to the target
migration's set. ``MASTERY_EVENT_KINDS`` / ``FINDING_KINDS`` are OPEN enums (no
SQL CHECK) — documentation tuples, NOT asserted against the SQL (asserting an
open enum would be wrong).
"""

from __future__ import annotations

import re
from pathlib import Path

from apollo.persistence.models import (
    ENTITY_KINDS,
    FINDING_KINDS,
    MASTERY_EVENT_KINDS,
)

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "database" / "migrations"
_MIGRATION_026 = _MIGRATIONS_DIR / "026_apollo_learner_model.sql"

_SUPABASE_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "supabase" / "migrations"
_APP_SCHEMA_V1 = _SUPABASE_MIGRATIONS_DIR / "20260717035041_create_app_schema_v1.sql"

_NEW_TABLES = (
    "apollo_kg_entities",
    "apollo_entity_prereqs",
    "apollo_learner_state",
    "apollo_mastery_events",
    "apollo_graph_comparison_runs",
    "apollo_graph_comparison_findings",
)


def _migration_text() -> str:
    return _MIGRATION_026.read_text(encoding="utf-8")


def _migration_sql_only() -> str:
    """Migration text with SQL line comments (``-- ...``) stripped, so assertions
    about DDL don't false-match on prose in comments (e.g. the comment that warns
    'Never UNIQUE(canonical_key)')."""
    lines = []
    for line in _migration_text().splitlines():
        idx = line.find("--")
        lines.append(line if idx == -1 else line[:idx])
    return "\n".join(lines)


def _target_entity_kind_allowlist() -> set[str]:
    """Values inside app.learner_entities' ``kind IN ( ... )`` CHECK in the
    DB-13 target DDL. Anchored on the constraint name
    (``learner_entities__kind__check``) since create_app_schema_v1.sql has
    several unrelated ``kind IN (...)`` CHECKs (uploads, chat_messages,
    provisioning_runs)."""
    sql = _APP_SCHEMA_V1.read_text(encoding="utf-8")
    match = re.search(
        r"learner_entities__kind__check\s*CHECK\s*\(kind\s+IN\s*\(([^)]*)\)\)",
        sql,
        re.IGNORECASE,
    )
    assert match, "could not find learner_entities__kind__check in create_app_schema_v1.sql"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_migration_026_file_exists():
    assert _MIGRATION_026.exists(), f"missing migration file: {_MIGRATION_026}"


def test_entity_kinds_match_target_ddl_check():
    assert _target_entity_kind_allowlist() == set(ENTITY_KINDS), (
        "create_app_schema_v1.sql learner_entities__kind__check and "
        "models.ENTITY_KINDS disagree: "
        f"migration={sorted(_target_entity_kind_allowlist())} model={sorted(ENTITY_KINDS)}"
    )


def test_migration_creates_all_eight_tables():
    body = _migration_text()
    for table in _NEW_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in body, f"{table} not created"


def test_migration_uses_nulls_not_distinct():
    body = _migration_text()
    assert "UNIQUE NULLS NOT DISTINCT (attempt_id, entity_id, event_kind)" in body


def test_migration_kg_entities_unique_is_per_concept():
    body = _migration_text()
    assert "UNIQUE (concept_id, canonical_key)" in body
    # A bare global UNIQUE (canonical_key) would be the §1.4 regression. Check the
    # DDL only (comments mention the anti-pattern as a warning).
    assert not re.search(r"UNIQUE\s*\(\s*canonical_key\s*\)", _migration_sql_only())


def test_migration_subjects_backfill_present():
    """The backfill UPDATE must appear BEFORE the SET NOT NULL — guards against a
    bare-NOT-NULL regression that would break a populated DB."""
    body = _migration_text()
    update_match = re.search(
        r"UPDATE\s+apollo_subjects\b.*?SET\s+search_space_id", body, re.IGNORECASE | re.DOTALL
    )
    notnull_match = re.search(
        r"ALTER\s+COLUMN\s+search_space_id\s+SET\s+NOT\s+NULL", body, re.IGNORECASE
    )
    assert update_match, "no backfill UPDATE on apollo_subjects.search_space_id"
    assert notnull_match, "no SET NOT NULL on apollo_subjects.search_space_id"
    assert update_match.start() < notnull_match.start(), "backfill UPDATE must precede SET NOT NULL"


def test_migration_header_notes_reconciliation():
    body = _migration_text()
    assert "023" in body  # the 023 collision note
    assert "024" in body and "025" in body  # unapplied-on-test note
    assert "LOCAL" in body  # local-only / do-not-auto-apply
    assert "DO NOT" in body.upper() or "do not" in body  # do-not-apply-remote


def test_migration_enables_rls_on_new_tables():
    body = _migration_text()
    public_tables = [t for t in _NEW_TABLES]
    for table in public_tables:
        assert f"ALTER TABLE {table}" in body
    # RLS enabled for each of the 6 public tables.
    assert body.count("ENABLE ROW LEVEL SECURITY") >= len(public_tables)


def test_mastery_event_and_finding_kinds_documented():
    """Documentation guard for the open-enum tuples (not asserted vs SQL)."""
    assert isinstance(MASTERY_EVENT_KINDS, tuple) and MASTERY_EVENT_KINDS
    assert isinstance(FINDING_KINDS, tuple) and FINDING_KINDS
    # The spec §2 mastery-event set and §6.3 finding set.
    assert set(MASTERY_EVENT_KINDS) == {
        "covered",
        "missing",
        "partial",
        "misconception",
        "corrected",
    }
    assert set(FINDING_KINDS) == {
        "covered_node",
        "missing_node",
        "matched_edge",
        "missing_edge",
        "unsupported_extra",
        "contradiction",
        "unresolved",
        "alternative_path",
        "covered_by_contraction",
        "not_demonstrated",
    }
