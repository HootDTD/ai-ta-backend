"""P1.9 — smoke test for the chat handler's sufficiency-progression.

Exercises `_build_sufficiency_verdict` against a real Problem and a real
KGGraph as it would evolve over a teaching session. Verifies the verdict
transitions in the right direction as the student teaches more
equations.

Does not require Neo4j — the helper is pure (no DB) given a KGGraph and a
Problem. The chat handler's full wiring (Neo4j, OpenAI) is covered by
the focused unit tests on each component (parser_confidence, sufficiency,
apollo_llm, output_filter); this test verifies the integration of those
components in the helper that chat.py calls.
"""
from __future__ import annotations

from apollo.handlers.chat import _build_sufficiency_verdict, _find_problem
from apollo.ontology import KGGraph, build_node
from apollo.overseer.problem_selector import (
    cluster_to_concept,
    list_problems_for_cluster,
)
from apollo.subjects import load_concept


def _student_kg(equations: list[tuple[str, str]], attempt_id: int = 1) -> KGGraph:
    """Build a student KGGraph with the given (symbolic, label) equations."""
    nodes = [
        build_node(
            node_type="equation",
            node_id=f"stu_{i}",
            attempt_id=attempt_id,
            source="parser",
            content={"symbolic": s, "label": label},
            parser_confidence=0.9,
        )
        for i, (s, label) in enumerate(equations)
    ]
    return KGGraph(nodes=nodes)


def _bernoulli_problem():
    """Pick the canonical Bernoulli problem from the bank — the same one
    the e2e tests target."""
    problems = list_problems_for_cluster("fluid_mechanics")
    assert problems, "expected at least one problem in fluid_mechanics bank"
    # The find-P2 problem is the canonical happy-path target. Fall back to
    # the first available if naming changes.
    target = next(
        (p for p in problems if "find_p2" in p.id),
        problems[0],
    )
    return target


def test_verdict_progresses_insufficient_to_sufficient():
    """Empty KG → continuity only → continuity + Bernoulli should walk
    `insufficient` → `insufficient`/`almost` → `sufficient`."""
    problem = _bernoulli_problem()
    subject_id, concept_id = cluster_to_concept("fluid_mechanics")
    concept = load_concept(subject_id, concept_id)

    # Turn 1: empty KG.
    v1 = _build_sufficiency_verdict(
        student_graph=_student_kg([]),
        problem=problem,
        concept=concept,
        attempt_id=1,
    )
    assert v1.state == "insufficient"

    # Turn 2: just continuity. Solver still stuck on Bernoulli-derived
    # variables; reference still has Bernoulli unmet. Expected:
    # `insufficient` (or `almost` if continuity alone is one-from-solving
    # — depends on the problem's reference shape).
    v2 = _build_sufficiency_verdict(
        student_graph=_student_kg([
            ("rho*A1*v1 - rho*A2*v2", "Continuity"),
        ]),
        problem=problem,
        concept=concept,
        attempt_id=1,
    )
    assert v2.state in ("insufficient", "almost")

    # Turn 3: continuity + Bernoulli. SymPy can solve; reference is met
    # (or close). Expected: `sufficient` (or `almost` if the reference
    # has additional structural requirements like procedure_steps).
    v3 = _build_sufficiency_verdict(
        student_graph=_student_kg([
            ("rho*A1*v1 - rho*A2*v2", "Continuity"),
            ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
             "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
        ]),
        problem=problem,
        concept=concept,
        attempt_id=1,
    )
    assert v3.state in ("sufficient", "almost")

    # Monotonic progression: each turn is at least as confident as the
    # previous (state ranking insufficient < almost < sufficient).
    rank = {"insufficient": 0, "almost": 1, "sufficient": 2}
    assert rank[v1.state] <= rank[v2.state] <= rank[v3.state]


def test_verdict_returns_hint_on_insufficient():
    """Empty KG must produce a non-null next_premise_hint so Apollo's
    confused question can be diagnostically targeted."""
    problem = _bernoulli_problem()
    subject_id, concept_id = cluster_to_concept("fluid_mechanics")
    concept = load_concept(subject_id, concept_id)

    verdict = _build_sufficiency_verdict(
        student_graph=_student_kg([]),
        problem=problem,
        concept=concept,
        attempt_id=1,
    )
    assert verdict.state == "insufficient"
    assert verdict.next_premise_hint is not None
    assert len(verdict.next_premise_hint) > 0


def test_verdict_soft_fails_on_malformed_equation():
    """A single malformed equation in the student KG must NOT break the
    chat turn — the helper soft-fails to a low-confidence verdict."""
    problem = _bernoulli_problem()
    subject_id, concept_id = cluster_to_concept("fluid_mechanics")
    concept = load_concept(subject_id, concept_id)

    verdict = _build_sufficiency_verdict(
        student_graph=_student_kg([("@@ broken @@", "Garbage")]),
        problem=problem,
        concept=concept,
        attempt_id=1,
    )
    assert verdict.state == "insufficient"
    assert verdict.confidence == 0.0


def test_find_problem_returns_problem_for_known_cluster():
    """Sanity check on the chat helper's problem lookup."""
    problems = list_problems_for_cluster("fluid_mechanics")
    assert problems
    target_id = problems[0].id
    p = _find_problem("fluid_mechanics", target_id)
    assert p.id == target_id
