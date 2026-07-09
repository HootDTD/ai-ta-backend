"""WU-3C1 — neo4j_schema.cypher declares the :Canon constraint + user_id index.

Pure text assertions over the schema file (the prod/Aura authority). The
Testcontainers fixture does NOT auto-apply this file, so these guard that the
declarations EXIST and are idempotent (`IF NOT EXISTS`).
"""

from __future__ import annotations

from pathlib import Path

_SCHEMA = Path(__file__).resolve().parents[2] / "persistence" / "neo4j_schema.cypher"


def _text() -> str:
    return _SCHEMA.read_text(encoding="utf-8")


def test_schema_declares_canon_key_constraint():
    text = _text()
    assert (
        "CREATE CONSTRAINT canon_key_unique IF NOT EXISTS FOR (c:Canon) REQUIRE c.key IS UNIQUE"
    ) in text


def test_schema_declares_user_id_index():
    text = _text()
    assert "CREATE INDEX kgnode_user_id IF NOT EXISTS FOR (n:_KGNode) ON (n.user_id)" in text
    # The existing attempt_id index is preserved (binding constraint).
    assert "CREATE INDEX kgnode_attempt_id IF NOT EXISTS FOR (n:_KGNode) ON (n.attempt_id)" in text


def test_schema_declares_canon_search_space_index():
    text = _text()
    assert (
        "CREATE INDEX canon_search_space_id IF NOT EXISTS FOR (c:Canon) ON (c.search_space_id)"
    ) in text


def test_schema_statements_idempotent():
    """Every CREATE statement uses IF NOT EXISTS (safe re-run on Aura)."""
    for line in _text().splitlines():
        stripped = line.strip()
        if stripped.startswith("CREATE CONSTRAINT") or stripped.startswith("CREATE INDEX"):
            assert "IF NOT EXISTS" in stripped, f"non-idempotent statement: {stripped!r}"
