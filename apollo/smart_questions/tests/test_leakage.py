"""The private-atom leakage belt, extracted from `unified` so neither file
exceeds the repo's 800-line ceiling.

These cases moved verbatim from `test_unified.py` — the belt's behaviour is
unchanged, only its home is.
"""

from __future__ import annotations

import pytest

from apollo.ontology import Edge, EdgeType, KGGraph, build_node
from apollo.smart_questions import leakage


def _graph(concept: str = "pressure", meaning: str = "force divided by area") -> KGGraph:
    return KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="a",
                attempt_id=1,
                source="reference",
                content={"concept": concept, "meaning": meaning},
            )
        ],
        edges=[],
    )


@pytest.mark.parametrize(
    ("reply", "flagged"),
    [
        ("Did it begin in 1970?", True),
        ("What did Toffler say?", True),
        ("How does change over time happen?", True),
        ("Does this change anything about how people live day to day?", False),
    ],
)
def test_belt_three_atom_classes_and_ordinary_english(reply: str, flagged: bool) -> None:
    meaning = "change over time" if flagged else "decision paralysis from excess options"
    assert (
        leakage.leaks_private_content(
            reply,
            reference_graph=_graph(concept="Toffler", meaning=meaning),
            public_text="Why does it occur?",
            student_messages=[],
        )
        is flagged
    )


def test_student_said_private_atoms_and_faithful_ack_pass() -> None:
    student = "I think Toffler described change over time in 1970"
    verdict = leakage.belt_verdict(
        "So you think Toffler described change over time in 1970. What happens next?",
        reference_graph=_graph(concept="Toffler", meaning="change over time in 1970"),
        public_text="What happens?",
        student_messages=[student],
    )
    assert not verdict.hit
    assert not verdict.malformed


def test_private_phrase_of_only_function_words_is_flagged() -> None:
    verdict = leakage.belt_verdict(
        "What happens after all?",
        reference_graph=_graph(concept="private", meaning="after all"),
        public_text="What happens?",
        student_messages=[],
    )
    assert verdict.private_vocabulary == ()
    assert "after all" in verdict.private_phrases
    assert verdict.offending_atoms == ("after all",)


def test_private_strings_walk_nodes_and_edge_labels() -> None:
    graph = _graph()
    graph.nodes.append(
        build_node(
            node_type="procedure_step",
            node_id="b",
            attempt_id=1,
            source="reference",
            content={"action": "multiply by area", "purpose": "find force"},
        )
    )
    graph.edges.append(
        Edge(
            edge_type=EdgeType.DEPENDS_ON,
            from_node_id="a",
            to_node_id="b",
            attempt_id=1,
            source="reference",
            from_node_type="definition",
            to_node_type="procedure_step",
        )
    )
    assert "DEPENDS_ON" in leakage.private_strings(graph)
    assert leakage._walk_strings({"a": [" x ", 2, {"b": ""}], "c": ("y",)}) == ["x", "y"]


def test_normalized_drops_case_and_punctuation() -> None:
    assert leakage.normalized("Well, X Matters -- really!") == "well x matters really"
