"""WU-4A2 Task 2 — coverage pass (R ⊑ S, MAX over declared paths).

RED-first. The binding rule: a reference key is covered iff it is in the S_norm
node-key set, scored PER path, and the result is the MAX over paths (a student
on a valid alternative path is graded against the path they took — never
punished against the authored one). Empty student graph → coverage 0.0, never
NaN.
"""

from __future__ import annotations

import math

from apollo.graph_compare.coverage import (
    PathCoverage,
    coverage_per_path,
    coverage_result,
)

from ._builders import cnode, empty_snorm, path, rgraph, rnode, snorm


def _ref_two_keys():
    return rgraph(
        nodes=(rnode("eq.a"), rnode("eq.b")),
        paths=(path("eq.a", "eq.b"),),
    )


def test_full_coverage_single_path():
    student = snorm(nodes=(cnode("eq.a"), cnode("eq.b")))
    pcs = coverage_per_path(student, _ref_two_keys())
    assert len(pcs) == 1
    assert pcs[0].score == 1.0
    assert pcs[0].covered_keys == ("eq.a", "eq.b")
    assert pcs[0].missing_keys == ()


def test_partial_coverage_single_path():
    ref = rgraph(
        nodes=(rnode("eq.a"), rnode("eq.b"), rnode("eq.c"), rnode("eq.d")),
        paths=(path("eq.a", "eq.b", "eq.c", "eq.d"),),
    )
    student = snorm(nodes=(cnode("eq.a"), cnode("eq.b")))
    pcs = coverage_per_path(student, ref)
    assert pcs[0].score == 0.5
    assert pcs[0].covered_keys == ("eq.a", "eq.b")
    assert pcs[0].missing_keys == ("eq.c", "eq.d")


def test_empty_student_graph_coverage_zero():
    max_score, winning, all_pcs = coverage_result(empty_snorm(), _ref_two_keys())
    assert max_score == 0.0
    assert not math.isnan(max_score)
    assert winning.score == 0.0
    assert all_pcs[0].covered_keys == ()
    assert all_pcs[0].missing_keys == ("eq.a", "eq.b")


def _two_path_ref():
    # Path A = a,b,c ; Path B = a,d,e (a shared anchor, then divergent steps).
    return rgraph(
        nodes=(rnode("a"), rnode("b"), rnode("c"), rnode("d"), rnode("e")),
        paths=(path("a", "b", "c"), path("a", "d", "e")),
    )


def test_max_over_two_paths_picks_better():
    # Student fully on path B (a,d,e), only partially on path A.
    student = snorm(nodes=(cnode("a"), cnode("d"), cnode("e")))
    max_score, winning, all_pcs = coverage_result(student, _two_path_ref())
    assert max_score == 1.0
    assert winning.path_index == 1
    # Path A scored lower (only 'a' of a,b,c covered).
    assert all_pcs[0].score == 1 / 3
    assert all_pcs[1].score == 1.0


def test_valid_alternative_path_zero_false_missings():
    student = snorm(nodes=(cnode("a"), cnode("d"), cnode("e")))
    _, winning, _ = coverage_result(student, _two_path_ref())
    # The winning path (B) has NO missing keys, even though path A keys (b,c)
    # are absent from S_norm — never punished against a non-taken path.
    assert winning.missing_keys == ()


def test_tie_broken_by_lowest_path_index():
    # Student covers exactly one key on each path (only the shared 'a').
    student = snorm(nodes=(cnode("a"),))
    max_score, winning, all_pcs = coverage_result(student, _two_path_ref())
    assert all_pcs[0].score == all_pcs[1].score == 1 / 3
    assert winning.path_index == 0  # deterministic tie-break to lowest index


def test_coverage_is_pure_same_input_same_output():
    student = snorm(nodes=(cnode("a"), cnode("d"), cnode("e")))
    ref = _two_path_ref()
    assert coverage_result(student, ref) == coverage_result(student, ref)


def test_path_coverage_is_frozen():
    pc = PathCoverage(path_index=0, covered_keys=(), missing_keys=("a",), score=0.0)
    assert pc.score == 0.0
