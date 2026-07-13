"""WU-4A2 — coverage pass: R_norm ⊑ S_norm, MAX over declared paths (§6.2).

Coverage measures how much of the reference solution the student's canonical
graph entails, on a PER-PATH basis: for each declared :class:`ReferencePathView`
a reference key is "covered" iff it appears in the S_norm node-key set (set
membership — ``R ⊑ S`` per path). The reported coverage is the **MAX over
paths**, so a student who took a valid alternative path is graded against the
path they actually took — never punished against the authored one (§6.6).

This module computes scores ONLY. Finding emission (covered/missing nodes) is
done in ``core.py`` against the WINNING path, which is why a node missing on a
non-taken path never becomes a false MISSING_NODE finding.

Pure + deterministic: same inputs always yield equal results; ties on score are
broken by the lowest ``path_index``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from apollo.graph_compare.canonical import CanonicalGraph, ReferenceGraph


@dataclass(frozen=True)
class PathCoverage:
    """Node-coverage of ONE declared reference path."""

    path_index: int
    covered_keys: tuple[str, ...]  # reference keys on this path present in S_norm (path order)
    missing_keys: tuple[str, ...]  # reference keys on this path absent from S_norm (path order)
    score: float  # covered / total for THIS path (vacuous 1.0 if the path is empty)
    raw_score: float = 0.0
    milestone_cap: float = 1.0


def coverage_per_path(
    student: CanonicalGraph,
    reference: ReferenceGraph,
    *,
    contracted_keys_by_path: Mapping[int, frozenset[str]] | None = None,
) -> tuple[PathCoverage, ...]:
    """One :class:`PathCoverage` per declared path, in path order. A reference
    key is covered iff it is in the S_norm node-key set or in that path's
    safeguarded contraction set."""
    student_keys = {n.canonical_key for n in student.nodes}
    out: list[PathCoverage] = []
    for path_index, path in enumerate(reference.paths):
        contracted = (
            contracted_keys_by_path.get(path_index, frozenset())
            if contracted_keys_by_path is not None
            else frozenset()
        )
        covered = tuple(k for k in path.canonical_keys if k in student_keys or k in contracted)
        missing = tuple(
            k for k in path.canonical_keys if k not in student_keys and k not in contracted
        )
        total = len(path.canonical_keys)
        # total == 0 never happens for a real declared path; guard to avoid a
        # div-by-zero and score a degenerate empty path 1.0 vacuously.
        raw_score = (len(covered) / total) if total else 1.0
        milestone_cap = 1.0
        if path.milestone_keys:
            covered_milestones = sum(
                key in student_keys or key in contracted for key in path.milestone_keys
            )
            milestone_cap = covered_milestones / len(path.milestone_keys)
        score = min(raw_score, milestone_cap)
        out.append(
            PathCoverage(
                path_index=path_index,
                covered_keys=covered,
                missing_keys=missing,
                score=score,
                raw_score=raw_score,
                milestone_cap=milestone_cap,
            )
        )
    return tuple(out)


def coverage_result(
    student: CanonicalGraph,
    reference: ReferenceGraph,
    *,
    contracted_keys_by_path: Mapping[int, frozenset[str]] | None = None,
) -> tuple[float, PathCoverage, tuple[PathCoverage, ...]]:
    """Return ``(max_score, winning_path_coverage, all_path_coverages)``.

    The winner is the path with the highest score; ties broken by the lowest
    ``path_index`` (deterministic). ``ReferenceGraph.paths`` is guaranteed length
    >= 1 upstream (WU-4A1 ``validate_reference``), so there is always a winner;
    an empty student graph scores every path 0.0 → ``max_score`` 0.0 (never NaN,
    because each real path has >= 1 key so its denominator is > 0)."""
    all_pcs = coverage_per_path(
        student,
        reference,
        contracted_keys_by_path=contracted_keys_by_path,
    )
    winning = _select_winning_path(all_pcs)
    return (winning.score, winning, all_pcs)


def _select_winning_path(all_pcs: tuple[PathCoverage, ...]) -> PathCoverage:
    """Highest score wins; tie -> lowest path_index (max() keeps the first item
    on a tie because (-score, index) is minimized — but we sort explicitly for
    clarity)."""
    return min(all_pcs, key=lambda pc: (-pc.score, pc.path_index))
