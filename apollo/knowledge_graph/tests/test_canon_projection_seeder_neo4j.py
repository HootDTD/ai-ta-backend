"""WU-3C1 — real-Neo4j integration tests for the :Canon seeder (write half).

Uses a LOCAL Testcontainers Neo4j fixture (`tc_neo4j`, from this dir's
conftest), NOT the live-Aura `neo4j_client` from apollo/conftest.py — the
binding constraint forbids touching remote Aura, and these must run green (not
skip) against a container.

The Postgres source is mocked deterministically: tests build in-memory
`CanonNodeSpec` lists and either call `merge_specs` directly or call
`project_canon` with a stubbed `db` whose `load_entity_specs` is monkeypatched.
Each test wipes the graph (MATCH (n) DETACH DELETE n) for isolation.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from apollo.errors import CanonProjectionError
from apollo.knowledge_graph import canon_projection
from apollo.knowledge_graph.canon_projection import (
    CanonNodeSpec,
    merge_specs,
    project_canon,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _spec(
    key: int,
    *,
    canonical_key: str,
    kind: str,
    display_name: str,
    concept_id: int = 1,
    search_space_id: int = 1,
) -> CanonNodeSpec:
    return CanonNodeSpec(
        key=key,
        search_space_id=search_space_id,
        concept_id=concept_id,
        canonical_key=canonical_key,
        kind=kind,
        display_name=display_name,
    )


async def _count_canon(neo) -> int:
    async with neo.session() as s:
        rec = await (await s.run("MATCH (c:Canon) RETURN count(c) AS n")).single()
        return int(rec["n"])


async def _read_canon(neo, key: int) -> dict[str, Any]:
    async with neo.session() as s:
        rec = await (await s.run("MATCH (c:Canon {key: $key}) RETURN c AS c", key=key)).single()
        return dict(rec["c"]) if rec else {}


class _StubDb:
    """Stand-in Postgres session — never executed (load is monkeypatched)."""


async def test_project_merges_one_canon_node_per_entity(tc_neo4j, monkeypatch):
    specs = [
        _spec(1, canonical_key="eq.continuity", kind="equation", display_name="Continuity"),
        _spec(2, canonical_key="cond.steady", kind="condition", display_name="Steady"),
        _spec(
            3,
            canonical_key="misc.density_ignored",
            kind="misconception",
            display_name="Density ignored",
        ),
    ]

    async def _fake_load(db, **kw):
        return specs

    monkeypatch.setattr(canon_projection, "load_entity_specs", _fake_load)

    result = await project_canon(_StubDb(), tc_neo4j, concept_id=1)
    assert await _count_canon(tc_neo4j) == 3
    assert result.entity_count == 3

    n1 = await _read_canon(tc_neo4j, 1)
    assert n1["canonical_key"] == "eq.continuity"
    assert n1["kind"] == "equation"
    assert n1["display_name"] == "Continuity"
    assert n1["search_space_id"] == 1
    assert n1["concept_id"] == 1


async def test_project_is_idempotent_same_count_second_run(tc_neo4j):
    specs = [
        _spec(10, canonical_key="eq.x", kind="equation", display_name="X"),
        _spec(11, canonical_key="eq.y", kind="equation", display_name="Y"),
    ]
    first = await merge_specs(tc_neo4j, specs)
    after_first = await _count_canon(tc_neo4j)
    second = await merge_specs(tc_neo4j, specs)
    after_second = await _count_canon(tc_neo4j)

    assert after_first == 2
    assert after_second == 2  # no duplicates on re-run
    assert first == 2
    assert second == 2  # MERGE re-touches the same 2 nodes


async def test_misconception_entity_projected_as_canon(tc_neo4j):
    await merge_specs(
        tc_neo4j,
        [
            _spec(
                20,
                canonical_key="misc.bernoulli_speed",
                kind="misconception",
                display_name="Speed up => pressure up",
            ),
        ],
    )
    node = await _read_canon(tc_neo4j, 20)
    assert node["kind"] == "misconception"
    assert node["canonical_key"] == "misc.bernoulli_speed"


async def test_key_property_is_int_equal_to_entity_id(tc_neo4j):
    await merge_specs(
        tc_neo4j,
        [
            _spec(42, canonical_key="eq.continuity", kind="equation", display_name="Continuity"),
        ],
    )
    node = await _read_canon(tc_neo4j, 42)
    assert node["key"] == 42
    assert isinstance(node["key"], int)


async def test_project_neo4j_failure_raises_canon_projection_error(monkeypatch):
    """A Neo4jClient whose session.run raises -> CanonProjectionError(merge_canon)."""

    class _FailingNeo:
        @asynccontextmanager
        async def session(self):
            yield self

        async def run(self, *a, **kw):
            raise RuntimeError("neo4j unreachable")

    specs = [_spec(1, canonical_key="eq.x", kind="equation", display_name="X")]

    async def _fake_load(db, **kw):
        return specs

    monkeypatch.setattr(canon_projection, "load_entity_specs", _fake_load)

    with pytest.raises(CanonProjectionError) as exc:
        await project_canon(_StubDb(), _FailingNeo(), concept_id=1)
    assert exc.value.stage == "merge_canon"
    assert "neo4j unreachable" in exc.value.last_error
