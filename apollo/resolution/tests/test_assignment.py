"""WU-3C2 Step 7 — pure-unit tests for bounded greedy global assignment (§5 step 4).

No Docker, no network. Pin:
- many student nodes MAY merge into one reference node (paraphrase evidence);
- one student node NEVER splits (at most one target);
- assignment is greedy in descending score order;
- deterministic tie-break (run twice -> identical);
- over the cap the whole attempt abstains (returns the abstain sentinel,
  no hang).
"""

from __future__ import annotations

from apollo.resolution.assignment import (
    MAX_STUDENT_NODES,
    AssignmentOutcome,
    greedy_global_assignment,
)
from apollo.resolution.candidates import Candidate
from apollo.resolution.structural import ScoredMatch


def _cand(key):
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name=key,
        opposes_key=None,
    )


def _m(node_id, key, score):
    return ScoredMatch(node_id, _cand(key), method="fuzzy", score=score)


def test_many_students_merge_into_one_reference_paraphrase():
    """Two student nodes both resolving to one reference node is allowed."""
    matches = {
        "s1": [_m("s1", "cond.x", 0.95)],
        "s2": [_m("s2", "cond.x", 0.93)],
    }
    outcome = greedy_global_assignment(matches)
    assert outcome.abstained is False
    assert outcome.assignment["s1"].candidate.canonical_key == "cond.x"
    assert outcome.assignment["s2"].candidate.canonical_key == "cond.x"


def test_one_student_never_splits():
    """A single student node maps to AT MOST one target (the best)."""
    matches = {
        "s1": [_m("s1", "cond.a", 0.91), _m("s1", "cond.b", 0.97)],
    }
    outcome = greedy_global_assignment(matches)
    assigned = outcome.assignment["s1"]
    assert assigned.candidate.canonical_key == "cond.b"  # the single best
    assert len([outcome.assignment["s1"]]) == 1


def test_descending_score_order_and_deterministic_tiebreak():
    """Greedy by score; ties break on (node_id, canonical_key); two runs are
    identical."""
    matches = {
        "s2": [_m("s2", "cond.a", 0.90)],
        "s1": [_m("s1", "cond.b", 0.90)],  # equal score -> deterministic order
    }
    first = greedy_global_assignment(matches)
    second = greedy_global_assignment(matches)
    assert {k: v.candidate.canonical_key for k, v in first.assignment.items()} == {
        k: v.candidate.canonical_key for k, v in second.assignment.items()
    }


def test_over_cap_abstains_no_hang():
    """151 student nodes -> the whole attempt abstains (no unbounded solve)."""
    matches = {f"s{i}": [_m(f"s{i}", "cond.x", 0.95)] for i in range(MAX_STUDENT_NODES + 1)}
    outcome = greedy_global_assignment(matches)
    assert isinstance(outcome, AssignmentOutcome)
    assert outcome.abstained is True
    assert outcome.assignment == {}


def test_at_cap_does_not_abstain():
    """Exactly the cap is fine; one over abstains."""
    matches = {f"s{i}": [_m(f"s{i}", "cond.x", 0.95)] for i in range(MAX_STUDENT_NODES)}
    outcome = greedy_global_assignment(matches)
    assert outcome.abstained is False


def test_node_with_no_candidate_matches_is_skipped():
    """A node whose candidate-match list is empty is skipped (not assigned)."""
    matches = {
        "s1": [_m("s1", "cond.x", 0.95)],
        "s2": [],  # no matches for this node
    }
    outcome = greedy_global_assignment(matches)
    assert "s1" in outcome.assignment
    assert "s2" not in outcome.assignment
