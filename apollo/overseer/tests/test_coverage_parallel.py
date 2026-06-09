import asyncio
import inspect

import pytest

from apollo.overseer import coverage
from apollo.ontology import KGGraph
from apollo.schemas.problem import load_problem


def _ref_graph():
    p = load_problem(
        "apollo/subjects/fluid_mechanics/concepts/bernoulli_principle/problems/problem_01.json"
    )
    return p.to_kg_graph(attempt_id=1)


def test_compute_coverage_is_async():
    assert inspect.iscoroutinefunction(coverage.compute_coverage)


def test_compute_coverage_parallel_matches_contract(monkeypatch):
    # Stub the two private matchers so no network is hit; assert the
    # assembled return shape covers every reference node.
    monkeypatch.setattr(
        coverage, "_procedure_match_score",
        lambda *a, **k: (1.0, 0.9),
    )

    def _fake_binary(*, entry_type, reference_nodes, student_nodes, model=None):
        return {n.node_id: {"covered": True, "confidence": 0.9} for n in reference_nodes}

    monkeypatch.setattr(coverage, "_batch_binary_match", _fake_binary)

    ref = _ref_graph()
    student = KGGraph(nodes=[], edges=[])
    result = asyncio.run(coverage.compute_coverage(student, ref))

    # Every reference node has a verdict.
    for n in ref.nodes:
        assert n.node_id in result["per_step"]
    assert "negotiation_counts" in result
