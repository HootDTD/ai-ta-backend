"""Apollo Neo4j degraded mode — `apollo/provisioning/authored_sets/api.py`.

Authored-set provisioning is teacher-facing and Neo4j-native (no meaningful
Postgres-only fallback), so every direct `get_neo4j_client()` call site in
this module routes through the local `_require_neo` guard: a `None` client
(construction failed / Aura unreachable) raises `KGUnavailableError`, which
`apollo.api`'s registered exception handler surfaces as a structured 503
`kg_unavailable`.

`get_neo4j_client` here stays a plain SYNC function (not `async def`) —
existing tests across `test_authored_api.py` / `test_ingest_observability.py`
monkeypatch it directly with a sync lambda
(`monkeypatch.setattr(aapi, "get_neo4j_client", lambda: "neo")` /
`lambda: None`), so the wrapper must remain a synchronous, directly callable
+ patchable seam.

NOTE: `_run_set_background` and the `delete_authored_set` / `approve_held_
problem` route bodies that CALL `_require_neo(get_neo4j_client(), ...)` are
exercised end-to-end by the Docker-backed `test_authored_api.py` /
`test_ingest_observability.py` suites (real Postgres via `db_session`) —
those files already monkeypatch `get_neo4j_client` with both a healthy
sentinel and `None` (see e.g. `test_approve_gate_rejection_rolls_back_mint`,
which passes `lambda: None` while the code path never actually reaches a
`get_neo4j_client()` call). This file covers `_require_neo` itself in pure
isolation (no DB, no Docker needed).
"""

from __future__ import annotations

import pytest

from apollo.errors import KGUnavailableError
from apollo.provisioning.authored_sets.api import _require_neo

pytestmark = pytest.mark.unit


def test_require_neo_raises_on_none():
    with pytest.raises(KGUnavailableError) as exc_info:
        _require_neo(None, stage="delete_authored_set")
    assert exc_info.value.stage == "delete_authored_set"
    assert exc_info.value.last_error == "client unavailable"


def test_require_neo_passes_through_healthy_client():
    sentinel = object()
    assert _require_neo(sentinel, stage="approve_held_problem") is sentinel
