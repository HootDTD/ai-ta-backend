"""No-infra econ before/after grading-delta harness (Phase 1a, D4).

Drives the REAL macroeconomics Q4 ``nominal_vs_real_gdp`` ``problem_01`` through
``build_problem_candidates -> resolve_attempt -> build_student_canonical ->
grade_attempt`` against a hand-authored STRONG student graph (mirrors RESULTS.md
attempt-20), and prints a JSON metrics dict. PURE + DETERMINISTIC: no DB, no
Neo4j, no OpenAI, no server. ``llm_adjudicator`` is a proc-only pure stub.

The point is the BEFORE/AFTER delta for the ``derived`` equation-alignment tier:

  * ``stu_eq_base`` (``deflator - (nomGDP/realGDP)*100``) — sign-exact control;
    resolves to ``eq.gdp_deflator`` via the symbolic tier BEFORE and AFTER.
  * ``stu_eq_rearranged`` (``realGDP - nomGDP/(PI/100)``) — the rearranged form.
    BEFORE: ``unresolved`` (the sign-exact symbolic tier rejects it). AFTER:
    ``resolved``/``eq.gdp_deflator`` via ``derived@0.95``.
  * ``USES`` edge ``stu_proc -> stu_eq_rearranged`` — BEFORE dropped (rearranged
    endpoint unresolved), AFTER retained (both endpoints resolved).

The captured BEFORE / AFTER JSON blocks live at the bottom of this file (the
§10 calibration record).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the repo root importable when run as a bare script (mirrors the other
# scripts/ entrypoints) so ``apollo`` resolves without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apollo.graph_compare.canonical import (  # noqa: E402  (path-setup before import)
    build_reference_canonical,
    build_student_canonical,
)
from apollo.graph_compare.core import grade_attempt  # noqa: E402
from apollo.graph_compare.problem_inputs import build_problem_candidates  # noqa: E402
from apollo.ontology.edges import Edge, EdgeType  # noqa: E402
from apollo.ontology.graph import KGGraph  # noqa: E402
from apollo.ontology.nodes import Node, build_node  # noqa: E402
from apollo.resolution import resolve_attempt  # noqa: E402
from apollo.resolution.tiers import student_surface_text  # noqa: E402

# Real macroeconomics Q4 problem (nominal_vs_real_gdp / real_gdp_from_deflator).
_PROBLEM_01 = (
    Path(__file__).resolve().parents[1]
    / "apollo"
    / "subjects"
    / "macroeconomics"
    / "concepts"
    / "nominal_vs_real_gdp"
    / "problems"
    / "problem_01.json"
)

# The reference deflator definition's sign-exact zero-form (the control).
_EQ_BASE = "deflator - (nomGDP/realGDP)*100"
# The rearranged-for-realGDP derived form (the node under test).
_EQ_REARRANGED = "realGDP - nomGDP/(PI/100)"

# The procedure step is resolved by fiat (a pure stub adjudicator), so the proc
# endpoint of the USES edge is NOT the variable under test.
_PROC_KEY = "proc.rearrange_for_real_gdp"


def _load_problem() -> dict:
    return json.loads(_PROBLEM_01.read_text(encoding="utf-8"))


def _eq_node(node_id: str, symbolic: str) -> Node:
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content={"symbolic": symbolic, "label": "", "variables": []},
    )


def _proc_node(node_id: str, action: str) -> Node:
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content={"action": action, "purpose": ""},
    )


def _student_graph() -> KGGraph:
    """The hand-authored STRONG student graph (RESULTS.md attempt-20)."""
    eq_base = _eq_node("stu_eq_base", _EQ_BASE)
    eq_rearranged = _eq_node("stu_eq_rearranged", _EQ_REARRANGED)
    proc = _proc_node(
        "stu_proc",
        "rearrange the deflator definition to solve for real GDP",
    )
    uses_edge = Edge(
        edge_type=EdgeType.USES,
        from_node_id="stu_proc",
        to_node_id="stu_eq_rearranged",
        attempt_id=1,
        from_node_type="procedure_step",
        to_node_type="equation",
    )
    return KGGraph(nodes=[eq_base, eq_rearranged, proc], edges=[uses_edge])


def _proc_only_adjudicator(_request: object) -> dict[str, str]:
    """Pure stub: resolve ONLY the procedure step by fiat. The equation nodes
    must resolve (or not) via the deterministic content tiers — the stub never
    touches them. No live OpenAI call ever fires."""
    return {"stu_proc": _PROC_KEY}


def run_delta() -> dict:
    """Run the full no-infra pipeline and return the metrics dict.

    Returns ``{per_node, unresolved_rate, dropped_edge_count, sub_scores}`` where
    ``per_node`` is ``[{content, resolution, method}]`` for every student node and
    ``sub_scores`` is ``{coverage, node_coverage, edge_coverage, scoping, usage}``.
    """
    problem = _load_problem()
    inputs = build_problem_candidates(
        problem,
        {"misconceptions": []},
        canon_key_by_canonical_key={},
    )

    student = _student_graph()
    resolution = resolve_attempt(
        student,
        inputs.candidates,
        llm_adjudicator=_proc_only_adjudicator,
        symbolic_mappings=inputs.symbolic_mappings,
    )

    resolved_by_id = {rn.node_id: rn for rn in resolution.resolved}
    per_node: list[dict[str, str]] = []
    for node in student.nodes:
        rn = resolved_by_id[node.node_id]
        per_node.append(
            {
                # student_surface_text returns the equation symbolic / proc action
                # via the existing typed accessor (no per-type union-attr).
                "content": student_surface_text(node),
                "resolution": rn.resolution,
                "method": rn.method,
            }
        )

    student_canonical = build_student_canonical(student, resolution)
    reference_graph = build_reference_canonical(problem)
    grade = grade_attempt(student_canonical, reference_graph)

    total = len(resolution.resolved)
    unresolved = sum(1 for rn in resolution.resolved if rn.resolution == "unresolved")
    unresolved_rate = unresolved / total if total else 0.0

    return {
        "per_node": per_node,
        "unresolved_rate": unresolved_rate,
        "dropped_edge_count": student_canonical.dropped_edge_count,
        "sub_scores": {
            "coverage": grade.coverage_score,
            "node_coverage": grade.node_coverage_score,
            "edge_coverage": grade.edge_coverage_score,
            "scoping": grade.scoping_score,
            "usage": grade.usage_score,
        },
    }


if __name__ == "__main__":  # pragma: no cover - thin CLI guard
    print(json.dumps(run_delta(), indent=2, sort_keys=True))


# === BEFORE (base=a58bdbf) ===
# {
#   "dropped_edge_count": 1,
#   "per_node": [
#     {
#       "content": "deflator - (nomGDP/realGDP)*100",
#       "method": "exact",
#       "resolution": "resolved"
#     },
#     {
#       "content": "realGDP - nomGDP/(PI/100)",
#       "method": "unresolved",
#       "resolution": "unresolved"
#     },
#     {
#       "content": "rearrange the deflator definition to solve for real GDP",
#       "method": "llm",
#       "resolution": "resolved"
#     }
#   ],
#   "sub_scores": {
#     "coverage": 0.6666666666666666,
#     "edge_coverage": 0.0,
#     "node_coverage": 0.6666666666666666,
#     "scoping": 1.0,
#     "usage": 0.0
#   },
#   "unresolved_rate": 0.3333333333333333
# }
#
# === AFTER (phase1a) ===
# {
#   "dropped_edge_count": 0,
#   "per_node": [
#     {
#       "content": "deflator - (nomGDP/realGDP)*100",
#       "method": "exact",
#       "resolution": "resolved"
#     },
#     {
#       "content": "realGDP - nomGDP/(PI/100)",
#       "method": "derived",
#       "resolution": "resolved"
#     },
#     {
#       "content": "rearrange the deflator definition to solve for real GDP",
#       "method": "llm",
#       "resolution": "resolved"
#     }
#   ],
#   "sub_scores": {
#     "coverage": 0.6666666666666666,
#     "edge_coverage": 0.25,
#     "node_coverage": 0.6666666666666666,
#     "scoping": 1.0,
#     "usage": 1.0
#   },
#   "unresolved_rate": 0.0
# }
