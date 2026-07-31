"""Pure tests for ``apollo.projections.performance_problems`` — the per-problem
``problems[]`` block (best-wins letter distribution, per-problem student list, and
the reference-node right/wrong breakdown). The node breakdown reuses the served
topic score's own ``_credit_for_node``, so the hand-computed fixtures here are the
same covered/partial/missing verdicts the student was shown. Real-PG loaders
(``load_problem_meta`` / ``load_graded_reference_nodes``) are exercised by
``tests/database/test_class_performance_postgres.py``."""

from __future__ import annotations

import pytest

from apollo.ontology import Node, build_node
from apollo.overseer.rubric import LETTER_BANDS
from apollo.projections import performance_problems as pp

pytestmark = pytest.mark.unit


def _eq() -> Node:
    return build_node(
        node_type="equation",
        node_id="eq1",
        attempt_id=0,
        source="reference",
        content={"symbolic": "P + v = c", "label": "Bernoulli"},
    )


def _cond() -> Node:
    return build_node(
        node_type="condition",
        node_id="c1",
        attempt_id=0,
        source="reference",
        content={"applies_when": "steady flow", "label": "Steady"},
    )


# _credit_for_node verdicts (reused from the scorer):
#   covered → per_step[node]=="covered" AND node in procedure_scores
#   partial → node in procedure_scores, NOT covered, in per_step, credit>0
#   missing → node NOT in procedure_scores (and not covered)
_COV_COVERED = {
    "per_step": {"eq1": "covered", "c1": "covered"},
    "procedure_scores": {"eq1": 1.0, "c1": 1.0},
}
_COV_PARTIAL = {
    "per_step": {"eq1": "missing", "c1": "covered"},
    "procedure_scores": {"eq1": 0.5, "c1": 1.0},
}
_COV_MISSED = {"per_step": {"c1": "covered"}, "procedure_scores": {"c1": 1.0}}


# --- letter_distribution ----------------------------------------------------


def test_letter_distribution_presents_every_grader_band_in_order():
    rows = pp.letter_distribution([])
    assert [row["letter"] for row in rows] == [letter for _t, letter in LETTER_BANDS]
    assert all(row["count"] == 0 for row in rows)


def test_letter_distribution_counts_served_letters_verbatim():
    rows = pp.letter_distribution(
        [
            {"letter": "A-"},
            {"letter": "A+"},
            {"letter": "B+"},
            {"letter": "A-"},
        ]
    )
    counts = {row["letter"]: row["count"] for row in rows}
    assert counts["A-"] == 2 and counts["A+"] == 1 and counts["B+"] == 1
    assert sum(counts.values()) == 4


# --- _usable_coverage -------------------------------------------------------


@pytest.mark.parametrize(
    "coverage,expected_usable",
    [
        ({"per_step": {"eq1": "covered"}}, True),
        ({"procedure_scores": {"eq1": 0.5}}, True),
        ({"per_step": {}, "procedure_scores": {}}, False),
        ({}, False),
        (None, False),
        ("not-a-dict", False),
    ],
)
def test_usable_coverage(coverage, expected_usable):
    result = pp._usable_coverage(coverage)
    assert (result is not None) is expected_usable
    if expected_usable:
        assert result is coverage


# --- aggregate_nodes --------------------------------------------------------


def test_aggregate_nodes_tallies_each_status_over_best_attempts():
    nodes = pp.aggregate_nodes([_eq(), _cond()], [_COV_COVERED, _COV_PARTIAL, _COV_MISSED])
    by_id = {n["node_id"]: n for n in nodes}
    # display_name + node_type come from the reference node (scorer's own resolver).
    assert (by_id["eq1"]["display_name"], by_id["eq1"]["node_type"]) == ("Bernoulli", "equation")
    assert (by_id["c1"]["display_name"], by_id["c1"]["node_type"]) == ("Steady", "condition")
    # eq1: covered / partial / missing across the three attempts.
    assert by_id["eq1"] == {
        "node_id": "eq1",
        "display_name": "Bernoulli",
        "node_type": "equation",
        "understood": 1,
        "partial": 1,
        "missed": 1,
        "graded": 3,
    }
    # c1: covered in all three.
    assert (by_id["c1"]["understood"], by_id["c1"]["partial"], by_id["c1"]["missed"]) == (3, 0, 0)
    assert by_id["c1"]["graded"] == 3


def test_aggregate_nodes_no_graded_nodes_is_empty():
    assert pp.aggregate_nodes([], [_COV_COVERED]) == []


def test_aggregate_nodes_no_attempts_reports_zeroed_nodes():
    nodes = pp.aggregate_nodes([_eq()], [])
    assert nodes == [
        {
            "node_id": "eq1",
            "display_name": "Bernoulli",
            "node_type": "equation",
            "understood": 0,
            "partial": 0,
            "missed": 0,
            "graded": 0,
        }
    ]


# --- students_for -----------------------------------------------------------


def test_students_for_orders_by_score_desc_and_resolves_email():
    rows = [
        {"user_id": "u1", "score": 85.0, "letter": "A-"},
        {"user_id": "u2", "score": 100.0, "letter": "A+"},
        {"user_id": "u3", "score": 30.0, "letter": "F"},
    ]
    identities = {"u1": {"email": "u1@e"}, "u2": {"email": "u2@e"}}
    students = pp.students_for(rows, identities)
    assert [s["user_id"] for s in students] == ["u2", "u1", "u3"]  # score desc
    assert students[0] == {"user_id": "u2", "email": "u2@e", "score": 100.0, "letter": "A+"}
    assert students[2]["email"] is None  # u3 not in identities -> None


def test_students_for_ties_break_on_user_id():
    rows = [
        {"user_id": "ub", "score": 90.0, "letter": "A"},
        {"user_id": "ua", "score": 90.0, "letter": "A"},
    ]
    students = pp.students_for(rows, {})
    assert [s["user_id"] for s in students] == ["ua", "ub"]  # equal score -> id asc


# --- build_problems ---------------------------------------------------------


def test_build_problems_groups_orders_and_composes_all_blocks():
    best_rows = [
        {"user_id": "u1", "problem_id": 1, "score": 85.0, "letter": "A-", "coverage": _COV_PARTIAL},
        {
            "user_id": "u2",
            "problem_id": 1,
            "score": 100.0,
            "letter": "A+",
            "coverage": _COV_COVERED,
        },
        {"user_id": "u3", "problem_id": 1, "score": 30.0, "letter": "F", "coverage": {}},
        {"user_id": "u1", "problem_id": 2, "score": 55.0, "letter": "D", "coverage": None},
    ]
    meta = {
        1: {
            "problem_code": "pb",
            "problem_text": "Explain flow",
            "concept_id": 7,
            "concept_name": "Beta",
        },
        2: {
            "problem_code": "pa",
            "problem_text": "Explain pressure",
            "concept_id": 5,
            "concept_name": "Alpha",
        },
    }
    identities = {"u1": {"email": "u1@e"}, "u2": {"email": "u2@e"}}
    nodes_by_problem = {1: [_eq(), _cond()], 2: []}

    block = pp.build_problems(best_rows, meta, identities, nodes_by_problem)

    # ordered by concept_name then problem_code: Alpha/pa (problem 2) before Beta/pb.
    assert [p["problem_id"] for p in block] == [2, 1]
    alpha, beta = block

    assert alpha["problem_text"] == "Explain pressure"
    assert (alpha["students_graded"], alpha["avg_best"]) == (1, 55.0)
    assert alpha["nodes"] == []  # no graded reference nodes
    assert alpha["students"] == [{"user_id": "u1", "email": "u1@e", "score": 55.0, "letter": "D"}]

    assert beta["problem_text"] == "Explain flow"
    assert (beta["problem_code"], beta["concept_name"]) == ("pb", "Beta")
    assert (beta["students_graded"], beta["avg_best"]) == (3, 71.7)  # (85+100+30)/3
    beta_dist = {row["letter"]: row["count"] for row in beta["distribution"]}
    assert beta_dist["A-"] == 1 and beta_dist["A+"] == 1 and beta_dist["F"] == 1
    # students: all three distinct students, score desc, u3 email degraded to None.
    assert [s["user_id"] for s in beta["students"]] == ["u2", "u1", "u3"]
    assert beta["students"][2] == {"user_id": "u3", "email": None, "score": 30.0, "letter": "F"}
    # nodes: u3's empty coverage is EXCLUDED, so graded=2 (u1 partial + u2 covered).
    beta_nodes = {n["node_id"]: n for n in beta["nodes"]}
    assert beta_nodes["eq1"]["graded"] == 2
    assert (
        beta_nodes["eq1"]["understood"],
        beta_nodes["eq1"]["partial"],
        beta_nodes["eq1"]["missed"],
    ) == (1, 1, 0)
    assert (beta_nodes["c1"]["understood"], beta_nodes["c1"]["graded"]) == (2, 2)


def test_build_problems_empty():
    assert pp.build_problems([], {}, {}, {}) == []
