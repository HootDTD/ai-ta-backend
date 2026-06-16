"""WU-3C2 Step 10 — pure-unit tests for the resolution_store mapping core.

No Docker, no network. Pin the DB-free seams + Cypher constants:
- resolved_node_to_edge_spec -> None for unresolved / canon-keyless nodes,
  else the 5-field ResolvesToEdgeSpec;
- resolved_node_to_field_spec -> the 4-field spec for EVERY node (incl.
  unresolved);
- the RESOLVES_TO Cypher uses MERGE (not CREATE) for the edge (idempotency);
- the resolution-fields Cypher SETs the four props;
- None-omission for resolved_key;
- empty spec lists short-circuit without opening a Neo4j session;
- a Neo4j write failure surfaces as ResolutionUnavailableError.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from apollo.errors import ResolutionUnavailableError
from apollo.knowledge_graph.resolution_store import (
    _RESOLUTION_FIELDS_CYPHER,
    _RESOLVES_TO_MERGE_CYPHER,
    ResolutionFieldSpec,
    ResolvesToEdgeSpec,
    persist_resolution_fields,
    resolved_node_to_edge_spec,
    resolved_node_to_field_spec,
    write_resolves_to,
)
from apollo.resolution.result import ResolvedNode

_RESOLVED_AT = "2026-06-16T00:00:00+00:00"


def _rn(node_id, *, resolution, method, canon, key, conf):
    return ResolvedNode(
        node_id=node_id,
        resolution=resolution,
        resolved_key=key,
        resolved_canon_key=canon,
        method=method,
        confidence=conf,
    )


def test_resolved_node_to_edge_spec_skips_unresolved():
    rn = _rn("s1", resolution="unresolved", method="unresolved", canon=None, key=None, conf=0.0)
    assert resolved_node_to_edge_spec(rn, resolved_at=_RESOLVED_AT) is None


def test_resolved_node_to_edge_spec_skips_missing_canon_key():
    rn = _rn("s1", resolution="resolved", method="alias", canon=None, key="cond.x", conf=0.92)
    assert resolved_node_to_edge_spec(rn, resolved_at=_RESOLVED_AT) is None


def test_resolved_node_to_edge_spec_shape():
    rn = _rn("s1", resolution="resolved", method="exact", canon=42, key="eq.b", conf=1.0)
    spec = resolved_node_to_edge_spec(rn, resolved_at=_RESOLVED_AT)
    assert spec == ResolvesToEdgeSpec(
        node_id="s1", canon_key=42, method="exact", confidence=1.0, resolved_at=_RESOLVED_AT
    )


def test_resolved_node_to_field_spec_always_present():
    rn = _rn("s1", resolution="unresolved", method="unresolved", canon=None, key=None, conf=0.0)
    spec = resolved_node_to_field_spec(rn)
    assert spec == ResolutionFieldSpec(
        node_id="s1",
        resolution="unresolved",
        resolved_key=None,
        resolution_method="unresolved",
        resolution_confidence=0.0,
    )


def test_resolves_to_cypher_uses_merge_not_create():
    assert "MERGE" in _RESOLVES_TO_MERGE_CYPHER
    assert "RESOLVES_TO" in _RESOLVES_TO_MERGE_CYPHER
    # Guard against an accidental CREATE that would duplicate on re-run.
    assert "CREATE (" not in _RESOLVES_TO_MERGE_CYPHER


def test_resolution_fields_cypher_sets_four_props():
    for prop in (
        "n.resolution",
        "n.resolved_key",
        "n.resolution_method",
        "n.resolution_confidence",
    ):
        assert prop in _RESOLUTION_FIELDS_CYPHER


@pytest.mark.asyncio
async def test_write_resolves_to_empty_returns_zero_without_session():
    class _ExplodingNeo:
        @asynccontextmanager
        async def session(self):  # pragma: no cover - must NOT be entered
            raise AssertionError("session opened for an empty edge write")
            yield self

    assert await write_resolves_to(_ExplodingNeo(), 1, []) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_persist_fields_empty_returns_zero_without_session():
    class _ExplodingNeo:
        @asynccontextmanager
        async def session(self):  # pragma: no cover - must NOT be entered
            raise AssertionError("session opened for an empty field write")
            yield self

    assert await persist_resolution_fields(_ExplodingNeo(), 1, []) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_write_resolves_to_neo4j_failure_raises_unavailable():
    class _FailingNeo:
        @asynccontextmanager
        async def session(self):
            yield self

        async def run(self, *a: Any, **kw: Any):
            raise RuntimeError("neo4j unreachable")

    specs = [ResolvesToEdgeSpec("s1", 42, "exact", 1.0, _RESOLVED_AT)]
    with pytest.raises(ResolutionUnavailableError) as exc:
        await write_resolves_to(_FailingNeo(), 1, specs)  # type: ignore[arg-type]
    assert exc.value.stage == "write_resolves_to"


@pytest.mark.asyncio
async def test_persist_fields_neo4j_failure_raises_unavailable():
    class _FailingNeo:
        @asynccontextmanager
        async def session(self):
            yield self

        async def run(self, *a: Any, **kw: Any):
            raise RuntimeError("neo4j unreachable")

    specs = [ResolutionFieldSpec("s1", "resolved", "eq.b", "exact", 1.0)]
    with pytest.raises(ResolutionUnavailableError) as exc:
        await persist_resolution_fields(_FailingNeo(), 1, specs)  # type: ignore[arg-type]
    assert exc.value.stage == "persist_fields"


@pytest.mark.asyncio
async def test_write_resolves_to_reraises_named_error_unwrapped():
    """A ResolutionUnavailableError raised inside the session is re-raised
    verbatim, NOT double-wrapped (the defensive re-raise branch)."""
    inner = ResolutionUnavailableError(stage="write_resolves_to", last_error="already named")

    class _NamedFailNeo:
        @asynccontextmanager
        async def session(self):
            yield self

        async def run(self, *a: Any, **kw: Any):
            raise inner

    specs = [ResolvesToEdgeSpec("s1", 42, "exact", 1.0, _RESOLVED_AT)]
    with pytest.raises(ResolutionUnavailableError) as exc:
        await write_resolves_to(_NamedFailNeo(), 1, specs)  # type: ignore[arg-type]
    assert exc.value is inner


@pytest.mark.asyncio
async def test_persist_fields_reraises_named_error_unwrapped():
    inner = ResolutionUnavailableError(stage="persist_fields", last_error="already named")

    class _NamedFailNeo:
        @asynccontextmanager
        async def session(self):
            yield self

        async def run(self, *a: Any, **kw: Any):
            raise inner

    specs = [ResolutionFieldSpec("s1", "resolved", "eq.b", "exact", 1.0)]
    with pytest.raises(ResolutionUnavailableError) as exc:
        await persist_resolution_fields(_NamedFailNeo(), 1, specs)  # type: ignore[arg-type]
    assert exc.value is inner
