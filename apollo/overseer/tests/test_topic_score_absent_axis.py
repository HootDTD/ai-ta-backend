"""THE named hazard (P3.2 spec §2.5, crit-A A6): the rubric absent-axis flip.

`rubric.compute_rubric` treats an axis as PRESENT iff its dict is non-empty. The
moment anything feeds `misconception_scores` (in production:
`done._attempt_misconception_scores`, which reads
`TutoringMessage.message_metadata['misconception']`),
`AXIS_WEIGHTS["misconception_corrected"] = 0.05` flips from absent to present,
every other axis rescales by 0.95, and `score_details.llm_rubric.overall` MOVES
— one level below the one labelled "score".

P3.2's wrongness findings live in `TopicScoreResult.topics[].misconceptions` and
must NEVER reach that argument. These tests prove (a) the hazard is real, and
(b) populating the P3.2 containers — even with the dark ceiling active — leaves
`llm_rubric` verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer.rubric import compute_rubric
from apollo.overseer.topic_score import compute_centrality, compute_topic_score

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Finding:
    node_id: str
    quote: str
    resolved: bool = False


def _nodes():
    return [
        build_node(
            node_type="procedure_step",
            node_id="p.one",
            attempt_id=1,
            source="reference",
            content={"action": "isolate v"},
        ),
        build_node(
            node_type="condition",
            node_id="c.one",
            attempt_id=1,
            source="reference",
            content={"applies_when": "steady flow", "label": "steady"},
        ),
        build_node(
            node_type="simplification",
            node_id="s.one",
            attempt_id=1,
            source="reference",
            content={"applies_when": "friction negligible", "transformation": "drop it"},
        ),
    ]


def _coverage():
    return {
        "per_step": {"p.one": "covered", "c.one": "covered", "s.one": "covered"},
        "procedure_scores": {"p.one": 1.0, "c.one": 1.0, "s.one": 1.0},
    }


def test_the_absent_axis_flip_is_real_and_moves_the_overall():
    """The tripwire, demonstrated: a single unresolved misconception score turns
    a perfect 100 into 98. This is what must never happen from P3.2 data."""
    nodes, coverage = _nodes(), _coverage()
    clean = compute_rubric(coverage, nodes)
    flipped = compute_rubric(coverage, nodes, misconception_scores={"some_code": 0.5})

    assert clean["misconception_corrected"]["present"] is False
    assert clean["overall"]["score"] == 100
    assert flipped["misconception_corrected"]["present"] is True
    assert flipped["overall"]["score"] != clean["overall"]["score"]


def test_populated_misconceptions_leave_the_llm_rubric_verbatim():
    """The guarantee: the rubric is computed from `coverage` + reference nodes
    only. Populating `topics[].misconceptions` — and even applying the dark
    ceiling — cannot change one byte of it, because the topic result is not an
    input to `compute_rubric` at all."""
    nodes, coverage = _nodes(), _coverage()
    centrality = compute_centrality(KGGraph(nodes=nodes))
    before = compute_rubric(coverage, nodes)

    scored = compute_topic_score(
        coverage=coverage,
        reference_nodes=nodes,
        centrality=centrality,
        misconceptions={"p.one": _Finding("p.one", "pressure rises with speed")},
        ceiling_active=True,
    )
    after = compute_rubric(coverage, nodes)

    # The ceiling really did fire on the topic lane...
    assert scored.score == 84
    assert scored.topics[0].misconceptions[0].dock_points == 16.0
    # ...and the axis rubric did not notice.
    assert after == before
    assert after["misconception_corrected"] == {
        "score": 0,
        "letter": "F",
        "present": False,
        "detected": 0,
        "resolved": 0,
    }
    assert after["overall"]["score"] == 100


def test_topic_score_never_touches_the_misconception_axis_inputs():
    """Structural pin: this module has no way to feed the axis even by
    accident — it neither imports `compute_rubric` nor mentions
    `misconception_scores` / `_attempt_misconception_scores` / the
    `TutoringMessage` metadata key the axis is fed from."""
    from apollo.overseer import topic_score

    with open(topic_score.__file__ or "", encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in (
        "misconception_scores",
        "_attempt_misconception_scores",
        "compute_rubric",
        "TutoringMessage",
        "message_metadata",
    ):
        assert forbidden not in source, forbidden
