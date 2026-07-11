"""Pure node_id->bank_code resolver for the structural co-key (F-struct Task 7)."""

from __future__ import annotations

import pytest

from apollo.ontology import KGGraph
from apollo.ontology.nodes import build_node
from apollo.overseer.misconception_bank import MisconceptionEntry
from apollo.overseer.misconception_detector.opposes_index import build_opposes_index

pytestmark = pytest.mark.unit


def _entry(code: str, opposes: str | None) -> MisconceptionEntry:
    return MisconceptionEntry(
        id=1,
        concept_id=1,
        code=code,
        description="d",
        confusion_pair=None,
        trigger_phrases=(),
        probe_question="p",
        rt_steps=(),
        opposes=opposes,
    )


def _ref_graph() -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="real_basis",
                attempt_id=1,
                source="reference",
                content={"concept": "real GDP", "meaning": "m"},
                entity_key="def.real_basis",
            ),
            build_node(
                node_type="equation",
                node_id="growth_rate",
                attempt_id=1,
                source="reference",
                content={"symbolic": "a-b"},
                entity_key="eq.growth_rate",
            ),
        ],
        edges=[],
    )


def test_maps_node_id_to_opposing_bank_code() -> None:
    idx = build_opposes_index(_ref_graph(), (_entry("nominal_for_real", "def.real_basis"),))
    assert idx == {"real_basis": "nominal_for_real"}


def test_entry_without_opposes_ignored() -> None:
    assert build_opposes_index(_ref_graph(), (_entry("x", None),)) == {}


def test_opposes_no_matching_node_ignored() -> None:
    assert build_opposes_index(_ref_graph(), (_entry("x", "def.absent"),)) == {}


def test_node_without_entity_key_never_matched() -> None:
    g = KGGraph(
        nodes=[
            build_node(
                node_type="equation",
                node_id="n",
                attempt_id=1,
                source="reference",
                content={"symbolic": "a"},
            )
        ],
        edges=[],
    )
    assert build_opposes_index(g, (_entry("x", None),)) == {}


def test_multi_opposes_same_node_lowest_code_wins() -> None:
    entries = (_entry("zeta", "def.real_basis"), _entry("alpha", "def.real_basis"))
    assert build_opposes_index(_ref_graph(), entries) == {"real_basis": "alpha"}
