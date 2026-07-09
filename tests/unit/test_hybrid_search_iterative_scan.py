"""Guards for the HNSW iterative-scan SET LOCALs on the semantic arm.

Two invariants:
1. The env-derived SET LOCAL statements are strictly validated — the mode
   comes from an allowlist and the numeric knobs are parsed/clamped ints, so
   env values can never inject SQL.
2. hybrid_search() emits the SET LOCALs on the *same session* (and therefore
   the same autobegun transaction) before the fused query, and emits none
   when HNSW_ITERATIVE_SCAN=off — the exact-scan kill switch.

Compile/fake-session only — no database required.
"""

from __future__ import annotations

import pytest

from retrieval.hybrid_search import (
    AITAHybridSearchRetriever,
    _iterative_scan_statements,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _iterative_scan_statements: env parsing + SQL safety
# ---------------------------------------------------------------------------


def test_default_is_relaxed_order_with_tuned_knobs(monkeypatch):
    monkeypatch.delenv("HNSW_ITERATIVE_SCAN", raising=False)
    monkeypatch.delenv("HNSW_EF_SEARCH", raising=False)
    monkeypatch.delenv("HNSW_MAX_SCAN_TUPLES", raising=False)
    stmts = _iterative_scan_statements()
    assert stmts == [
        "SET LOCAL hnsw.iterative_scan = relaxed_order",
        "SET LOCAL hnsw.ef_search = 300",
        "SET LOCAL hnsw.max_scan_tuples = 20000",
    ]


@pytest.mark.parametrize("off", ["off", "OFF", "0", "false", "disabled", "none", " "])
def test_kill_switch_disables_all_statements(monkeypatch, off):
    monkeypatch.setenv("HNSW_ITERATIVE_SCAN", off)
    assert _iterative_scan_statements() == []


def test_strict_order_is_accepted(monkeypatch):
    monkeypatch.setenv("HNSW_ITERATIVE_SCAN", "strict_order")
    assert _iterative_scan_statements()[0] == ("SET LOCAL hnsw.iterative_scan = strict_order")


def test_unknown_mode_disables_instead_of_injecting(monkeypatch):
    monkeypatch.setenv("HNSW_ITERATIVE_SCAN", "relaxed_order; DROP TABLE aita_chunks")
    assert _iterative_scan_statements() == []


def test_numeric_knobs_reject_non_integers(monkeypatch):
    monkeypatch.delenv("HNSW_ITERATIVE_SCAN", raising=False)
    monkeypatch.setenv("HNSW_EF_SEARCH", "100; DROP TABLE aita_chunks")
    monkeypatch.setenv("HNSW_MAX_SCAN_TUPLES", "not-a-number")
    stmts = _iterative_scan_statements()
    assert "SET LOCAL hnsw.ef_search = 300" in stmts  # fell back to default
    assert "SET LOCAL hnsw.max_scan_tuples = 20000" in stmts


def test_numeric_knobs_clamp_to_pgvector_bounds(monkeypatch):
    monkeypatch.delenv("HNSW_ITERATIVE_SCAN", raising=False)
    monkeypatch.setenv("HNSW_EF_SEARCH", "999999")
    monkeypatch.setenv("HNSW_MAX_SCAN_TUPLES", "1")
    stmts = _iterative_scan_statements()
    assert "SET LOCAL hnsw.ef_search = 1000" in stmts
    assert "SET LOCAL hnsw.max_scan_tuples = 1000" in stmts


# ---------------------------------------------------------------------------
# hybrid_search(): SET LOCALs precede the fused query on the same session
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Records every executed statement.

    The first SELECT is the visible-doc-id resolution — return a non-empty list
    so hybrid_search proceeds past its early-return to the SET LOCALs + fused
    query. Every later statement returns no rows.
    """

    def __init__(self):
        self.executed: list[str] = []
        self._select_count = 0

    async def execute(self, statement):
        sql = str(statement)
        self.executed.append(sql)
        if sql.strip().lower().startswith("select"):
            self._select_count += 1
            if self._select_count == 1:
                return _FakeResult([(1,), (2,)])  # visible doc-id resolution
        return _FakeResult([])


@pytest.fixture
def fake_embed(monkeypatch):
    from database.models import EMBEDDING_DIM

    monkeypatch.setattr("retrieval.hybrid_search.embed_text", lambda _q: [0.1] * EMBEDDING_DIM)


def _is_query(sql: str) -> bool:
    # The doc-id resolution is a plain SELECT; the fused query starts with WITH
    # (the semantic/keyword CTEs).
    return sql.strip().lower().startswith(("select", "with"))


async def test_set_locals_run_between_docid_and_fused_query(monkeypatch, fake_embed):
    monkeypatch.delenv("HNSW_ITERATIVE_SCAN", raising=False)
    session = _FakeSession()
    retriever = AITAHybridSearchRetriever(session, search_space_id=1)

    out = await retriever.hybrid_search("what is a normal shock")

    assert out == []
    executed = session.executed
    set_local_idx = [i for i, s in enumerate(executed) if s.startswith("SET LOCAL")]
    select_idx = [i for i, s in enumerate(executed) if _is_query(s)]
    assert len(set_local_idx) == 3
    assert len(select_idx) == 2  # doc-id resolution + fused query
    # Order: doc-id SELECT -> 3x SET LOCAL -> fused SELECT, all one transaction.
    assert select_idx[0] < min(set_local_idx), "doc-id query runs first"
    assert max(set_local_idx) < select_idx[1], (
        "SET LOCALs must precede the fused query so they share its transaction"
    )


async def test_kill_switch_emits_no_set_locals(monkeypatch, fake_embed):
    monkeypatch.setenv("HNSW_ITERATIVE_SCAN", "off")
    session = _FakeSession()
    retriever = AITAHybridSearchRetriever(session, search_space_id=1)

    await retriever.hybrid_search("what is a normal shock")

    assert not any(s.startswith("SET LOCAL") for s in session.executed)
    # Just the doc-id resolution + the fused query.
    assert len(session.executed) == 2
