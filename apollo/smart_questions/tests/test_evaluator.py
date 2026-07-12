import pytest

from apollo.ontology import KGGraph, build_node
from apollo.smart_questions import evaluator
from apollo.smart_questions.planner import NodeCoverage


@pytest.mark.asyncio
async def test_maps_continuous_credit_to_question_states(monkeypatch):
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="a",
                attempt_id=1,
                source="reference",
                content={"concept": "a", "meaning": "A"},
            ),
            build_node(
                node_type="definition",
                node_id="b",
                attempt_id=1,
                source="reference",
                content={"concept": "b", "meaning": "B"},
            ),
            build_node(
                node_type="definition",
                node_id="c",
                attempt_id=1,
                source="reference",
                content={"concept": "c", "meaning": "C"},
            ),
        ],
        edges=[],
    )

    def fake_call(**kwargs):
        return '{"nodes":[{"node_id":"a","state":"covered","credit":0.7},{"node_id":"b","state":"partial","credit":0.3},{"node_id":"c","state":"missing","credit":0.0}]}'

    monkeypatch.setattr(evaluator, "_call_evaluator", fake_call)
    result = await evaluator.evaluate_reference_coverage(
        transcript=[("student", "hello")],
        reference_graph=graph,
        problem=type("P", (), {"problem_text": "p"})(),
    )
    assert [(item.node_id, item.state) for item in result] == [
        ("a", "covered"),
        ("b", "partial"),
        ("c", "missing"),
    ]


@pytest.mark.asyncio
async def test_missing_or_hallucinated_verdicts_fail_closed(monkeypatch):
    graph = KGGraph(
        nodes=[
            build_node(
                node_type="definition",
                node_id="a",
                attempt_id=1,
                source="reference",
                content={"concept": "a", "meaning": "A"},
            )
        ],
        edges=[],
    )
    monkeypatch.setattr(
        evaluator,
        "_call_evaluator",
        lambda **kwargs: '{"nodes":[{"node_id":"invented","state":"covered","credit":1.0}]}',
    )
    result = await evaluator.evaluate_reference_coverage(
        transcript=[], reference_graph=graph, problem=type("P", (), {"problem_text": "p"})()
    )
    assert result == [NodeCoverage("a", "missing", 0.0)]
