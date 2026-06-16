"""WU-3C1 — pure-unit tests for the :Canon projection mapping core.

No Docker, no network, no Postgres, no Neo4j. These pin the pure
entity->property-bag mapping and the load-bearing surrogate-id keying
decision (the entity id, NOT a synthesized search_space:canonical_key,
is what prevents two concepts that mint the same canonical_key from
fusing into one :Canon node).
"""

from __future__ import annotations

import dataclasses
from contextlib import asynccontextmanager
from typing import Any

import pytest

from apollo.errors import CanonProjectionError, RetentionError
from apollo.knowledge_graph.canon_projection import (
    _CANON_MERGE_CYPHER,
    CanonNodeSpec,
    canon_spec_to_row,
    entity_to_canon_spec,
    load_entity_specs,
    merge_specs,
)


def test_entity_to_canon_spec_carries_all_properties():
    spec = entity_to_canon_spec(
        42,
        concept_id=7,
        search_space_id=3,
        canonical_key="eq.continuity",
        kind="equation",
        display_name="Continuity",
    )
    assert isinstance(spec, CanonNodeSpec)
    assert spec.key == 42
    assert spec.concept_id == 7
    assert spec.search_space_id == 3
    assert spec.canonical_key == "eq.continuity"
    assert spec.kind == "equation"
    assert spec.display_name == "Continuity"


def test_canon_spec_key_is_surrogate_entity_id():
    """The load-bearing decision: key == entity.id, NOT a synthesized
    f"{search_space_id}:{canonical_key}". Two concepts both minting
    eq.continuity get distinct keys (their distinct entity ids), so they
    cannot fuse."""
    spec = entity_to_canon_spec(
        99,
        concept_id=1,
        search_space_id=5,
        canonical_key="eq.continuity",
        kind="equation",
        display_name="Continuity",
    )
    assert spec.key == 99
    assert spec.key != "5:eq.continuity"
    # A second entity under a different concept with the SAME canonical_key
    # must yield a different key.
    other = entity_to_canon_spec(
        100,
        concept_id=2,
        search_space_id=5,
        canonical_key="eq.continuity",
        kind="equation",
        display_name="Continuity",
    )
    assert other.key == 100
    assert spec.key != other.key


def test_canon_spec_to_row_shape():
    spec = entity_to_canon_spec(
        1,
        concept_id=2,
        search_space_id=3,
        canonical_key="eq.bernoulli",
        kind="equation",
        display_name="Bernoulli",
    )
    row = canon_spec_to_row(spec)
    assert row == {
        "key": 1,
        "search_space_id": 3,
        "concept_id": 2,
        "canonical_key": "eq.bernoulli",
        "kind": "equation",
        "display_name": "Bernoulli",
    }
    # Exactly the 6 keys — no RESOLVES_TO / resolution fields leak in.
    assert set(row.keys()) == {
        "key",
        "search_space_id",
        "concept_id",
        "canonical_key",
        "kind",
        "display_name",
    }


def test_misconception_kind_maps_unchanged():
    spec = entity_to_canon_spec(
        17,
        concept_id=4,
        search_space_id=2,
        canonical_key="misc.density_ignored",
        kind="misconception",
        display_name="Density ignored",
    )
    row = canon_spec_to_row(spec)
    assert row["kind"] == "misconception"
    assert row["canonical_key"] == "misc.density_ignored"


def test_canon_node_spec_is_frozen():
    spec = entity_to_canon_spec(
        1,
        concept_id=1,
        search_space_id=1,
        canonical_key="eq.x",
        kind="equation",
        display_name="X",
    )
    # dataclasses.replace works on a frozen dataclass...
    replaced = dataclasses.replace(spec, display_name="Y")
    assert replaced.display_name == "Y"
    assert spec.display_name == "X"  # original untouched (immutability)
    # ...but in-place attribute assignment is forbidden.
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.display_name = "Z"  # type: ignore[misc]


def test_merge_cypher_uses_key_only_and_merge():
    assert "MERGE (c:Canon {key: row.key})" in _CANON_MERGE_CYPHER
    assert "count(c) AS merged" in _CANON_MERGE_CYPHER
    # Guard against an accidental CREATE that would duplicate on re-run.
    assert "CREATE (c:Canon" not in _CANON_MERGE_CYPHER


def test_canon_projection_error_message_and_base():
    err = CanonProjectionError(stage="merge_canon", last_error="boom")
    assert isinstance(err, Exception)
    assert err.stage == "merge_canon"
    assert err.last_error == "boom"
    assert "merge_canon" in str(err)
    assert "boom" in str(err)


def test_retention_error_message_and_base():
    err = RetentionError(attempt_id=-501, last_error="neo down")
    assert err.attempt_id == -501
    assert err.last_error == "neo down"
    assert "-501" in str(err)
    assert "neo down" in str(err)


# ---------------------------------------------------------------------------
# load_entity_specs / merge_specs failure + edge branches (no live infra:
# a fake async session / fake Neo4j client drive the wrap-and-raise paths).
# ---------------------------------------------------------------------------


class _FailingDb:
    """An AsyncSession stand-in whose execute() always raises — drives the
    load_entities NO-FALLBACK wrap in load_entity_specs."""

    async def execute(self, *a: Any, **kw: Any):
        raise RuntimeError("postgres unreachable")


@pytest.mark.asyncio
async def test_load_entity_specs_db_failure_raises_canon_projection_error():
    with pytest.raises(CanonProjectionError) as exc:
        await load_entity_specs(_FailingDb(), concept_id=7)  # type: ignore[arg-type]
    assert exc.value.stage == "load_entities"
    assert "postgres unreachable" in exc.value.last_error


@pytest.mark.asyncio
async def test_merge_specs_empty_returns_zero_without_touching_neo4j():
    """No specs -> short-circuit to 0; the Neo4j session is never opened."""

    class _ExplodingNeo:
        @asynccontextmanager
        async def session(self):  # pragma: no cover - must NOT be entered
            raise AssertionError("session opened for an empty merge")
            yield self

    assert await merge_specs(_ExplodingNeo(), []) == 0  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_merge_specs_reraises_canon_projection_error_unwrapped():
    """A CanonProjectionError raised inside the session is re-raised verbatim,
    NOT double-wrapped into a merge_canon error (the defensive re-raise branch)."""

    inner = CanonProjectionError(stage="load_entities", last_error="already named")

    class _NamedFailNeo:
        @asynccontextmanager
        async def session(self):
            yield self

        async def run(self, *a: Any, **kw: Any):
            raise inner

    spec = entity_to_canon_spec(
        1, concept_id=1, search_space_id=1,
        canonical_key="eq.x", kind="equation", display_name="X",
    )
    with pytest.raises(CanonProjectionError) as exc:
        await merge_specs(_NamedFailNeo(), [spec])  # type: ignore[arg-type]
    assert exc.value is inner  # re-raised, not re-wrapped
    assert exc.value.stage == "load_entities"
