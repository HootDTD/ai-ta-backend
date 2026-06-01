"""P3.1 — Negotiable OLM node fields on the Pydantic Node base.

Three orthogonal contracts are tested here:

    1. New nodes default to status=ACCEPTED + student_belief=None — the
       pre-P3 baseline. No behavioral change for callers that don't
       construct them.
    2. status validates the closed Literal — "ACCEPTED" / "DISPUTED" /
       "DUAL" — and rejects anything else.
    3. student_belief is plain str | None.

These guarantee that pre-P3 callers get a no-op, and P3 callers can move
nodes through the three states without leaving the type system.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from apollo.ontology import build_node
from apollo.ontology.nodes import EquationNode, NodeStatus


def _build(**overrides):
    base = dict(
        node_type="equation",
        node_id="n1",
        attempt_id=1,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "x"},
    )
    base.update(overrides)
    return build_node(**base)


def test_default_status_is_accepted_and_student_belief_is_none():
    n = _build()
    assert n.status == "ACCEPTED"
    assert n.student_belief is None


def test_build_node_accepts_disputed_and_dual():
    for status in ("DISPUTED", "DUAL"):
        n = _build(status=status)
        assert n.status == status


def test_build_node_accepts_student_belief_string():
    n = _build(status="DUAL", student_belief="my words")
    assert n.student_belief == "my words"
    assert n.status == "DUAL"


def test_status_rejects_unknown_literal():
    with pytest.raises(ValidationError):
        EquationNode(
            node_id="n1", attempt_id=1, source="parser",
            content={"symbolic": "x", "label": ""},
            status="WHATEVER",  # type: ignore[arg-type]
        )


def test_status_typing_is_closed_three_member_literal():
    """Document-as-test: NodeStatus is exactly the three values. If a
    future move adds REJECTED / RESOLVED, this test breaks intentionally
    and the migration + Done-gate rules need recalibration."""
    import typing as _t
    members = _t.get_args(NodeStatus)
    assert set(members) == {"ACCEPTED", "DISPUTED", "DUAL"}


def test_student_belief_kept_alongside_other_overrides():
    """status, student_belief, and parser_confidence are all orthogonal —
    setting one must not zero out the others."""
    n = _build(
        parser_confidence=0.4,
        status="DISPUTED",
        student_belief="rephrased",
    )
    assert n.parser_confidence == 0.4
    assert n.status == "DISPUTED"
    assert n.student_belief == "rephrased"
