"""entity_key plumbing on the runtime node (F-struct Task 1)."""

from __future__ import annotations

import pytest

from apollo.ontology.nodes import DefinitionNode, build_node

pytestmark = pytest.mark.unit


def test_node_base_defaults_entity_key_none() -> None:
    node = build_node(
        node_type="definition",
        node_id="real_basis",
        attempt_id=1,
        source="reference",
        content={"concept": "real GDP", "meaning": "inflation-adjusted"},
    )
    assert node.entity_key is None


def test_build_node_threads_entity_key() -> None:
    node = build_node(
        node_type="definition",
        node_id="real_basis",
        attempt_id=1,
        source="reference",
        content={"concept": "real GDP", "meaning": "inflation-adjusted"},
        entity_key="def.real_basis",
    )
    assert isinstance(node, DefinitionNode)
    assert node.entity_key == "def.real_basis"
    # Round-trips through pydantic serialization unchanged.
    assert node.model_dump()["entity_key"] == "def.real_basis"
