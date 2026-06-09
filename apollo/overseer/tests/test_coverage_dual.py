"""P3.4 — Coverage adapter for DUAL entries.

Two pure-function contracts and one integration:

    1. _student_payload — the per-node payload fed to the LLM. ACCEPTED
       and DUAL-skip pass content through unchanged. DUAL-with-belief
       adds `student_belief` to the dict; for procedure_step it also
       substitutes `action` (the matcher's primary semantic field).

    2. compute_coverage — adds `negotiation_counts` to its return shape:
       dual / disputed / paraphrased / skipped tallies. Diagnostic
       narration (P3.11) reads from there.

The LLM call itself is patched. Mocks return deterministic verdicts so
we can attest exactly what payload the LLM received.
"""
from __future__ import annotations
import asyncio

from unittest.mock import patch, MagicMock

import json

import pytest

from apollo.ontology import Edge, EdgeType, KGGraph, build_node
from apollo.overseer.coverage import _student_payload, compute_coverage


# ---------------------------------------------------------------------------
# _student_payload — pure function
# ---------------------------------------------------------------------------

def _eq(**overrides):
    base = dict(
        node_type="equation", node_id="eq1", attempt_id=1, source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
    )
    base.update(overrides)
    return build_node(**base)


def _proc(**overrides):
    base = dict(
        node_type="procedure_step", node_id="p1", attempt_id=1, source="parser",
        content={"action": "write continuity", "purpose": "find v2"},
    )
    base.update(overrides)
    return build_node(**base)


def test_accepted_node_payload_unchanged():
    """ACCEPTED is the pre-P3 baseline — payload must equal content dump."""
    n = _eq()
    assert _student_payload(n) == n.content.model_dump()


def test_dual_with_belief_appends_student_belief_field():
    n = _eq(status="DUAL", student_belief="rho A v stays the same")
    payload = _student_payload(n)
    assert payload["student_belief"] == "rho A v stays the same"
    assert payload["symbolic"] == "A1*v1 - A2*v2"  # structure preserved
    assert payload["label"] == "continuity"


def test_dual_no_belief_skip_payload_unchanged():
    """SKIP is DUAL with student_belief=None. Payload passes through."""
    n = _eq(status="DUAL", student_belief=None)
    assert _student_payload(n) == n.content.model_dump()


def test_disputed_payload_unchanged():
    """DISPUTED has no student_belief — the move is a flag, not a rewrite."""
    n = _eq(status="DISPUTED")
    assert _student_payload(n) == n.content.model_dump()


def test_dual_procedure_step_substitutes_action():
    """For procedure_step, the LLM matcher reads `action` as the primary
    semantic field. DUAL-with-belief substitutes action AND adds
    student_belief — so the substitution actually shifts the score."""
    n = _proc(
        status="DUAL",
        student_belief="apply mass conservation between sections",
    )
    payload = _student_payload(n)
    assert payload["action"] == "apply mass conservation between sections"
    assert payload["student_belief"] == "apply mass conservation between sections"
    # purpose preserved untouched.
    assert payload["purpose"] == "find v2"


# ---------------------------------------------------------------------------
# compute_coverage — integration with mocked LLM
# ---------------------------------------------------------------------------

def _ref_eq(node_id: str, label: str = "ref"):
    return build_node(
        node_type="equation", node_id=node_id, attempt_id=1, source="reference",
        content={"symbolic": "x - y", "label": label},
    )


def _build_graph(nodes, edges=None):
    return KGGraph(nodes=nodes, edges=edges or [])


@patch("apollo.overseer.coverage.OpenAI")
def test_dual_paraphrase_payload_reaches_llm(mock_client_cls):
    """Spy on the LLM call: the JSON body must include student_belief
    on the DUAL student entry."""
    captured: dict = {}

    def _create(**kwargs):
        captured["body"] = json.loads(kwargs["messages"][1]["content"])
        return MagicMock(
            choices=[MagicMock(message=MagicMock(
                content='{"matches":[{"ref_id":"r1","covered":true,"confidence":0.9}]}'
            ))]
        )

    client = MagicMock()
    client.chat.completions.create.side_effect = _create
    mock_client_cls.return_value = client

    student = _build_graph([
        build_node(
            node_type="equation", node_id="s1", attempt_id=1, source="parser",
            content={"symbolic": "A1*v1 - A2*v2", "label": "x"},
            status="DUAL", student_belief="ρAV is conserved",
        ),
    ])
    reference = _build_graph([_ref_eq("r1")])

    cov = asyncio.run(compute_coverage(student, reference))

    body = captured["body"]
    assert body["entry_type"] == "equation"
    assert len(body["student_entries"]) == 1
    se = body["student_entries"][0]
    assert se["student_belief"] == "ρAV is conserved"
    # Structural fields still present so the LLM can reason over the
    # underlying mathematics.
    assert se["symbolic"] == "A1*v1 - A2*v2"

    # And the verdict was wired through.
    assert cov["per_step"]["r1"] == "covered"


@patch("apollo.overseer.coverage.OpenAI")
def test_accepted_payload_byte_identical_to_pre_p3(mock_client_cls):
    """Pre-P3 baseline: all-ACCEPTED student graph produces exactly the
    same payload as before P3.4 — no `student_belief` keys leak in."""
    captured: dict = {}

    def _create(**kwargs):
        captured["body"] = json.loads(kwargs["messages"][1]["content"])
        return MagicMock(choices=[MagicMock(message=MagicMock(
            content='{"matches":[{"ref_id":"r1","covered":false,"confidence":1.0}]}'
        ))])

    client = MagicMock()
    client.chat.completions.create.side_effect = _create
    mock_client_cls.return_value = client

    student = _build_graph([
        build_node(
            node_type="equation", node_id="s1", attempt_id=1, source="parser",
            content={"symbolic": "x", "label": ""},
        ),
    ])
    reference = _build_graph([_ref_eq("r1")])
    asyncio.run(compute_coverage(student, reference))

    se = captured["body"]["student_entries"][0]
    assert "student_belief" not in se


@patch("apollo.overseer.coverage.OpenAI")
def test_negotiation_counts_in_coverage_output(mock_client_cls):
    """compute_coverage exposes per-status counts so diagnostic.py can
    narrate "you negotiated N entries" without re-walking the graph."""
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(
            content='{"matches":[]}'
        ))]
    )
    mock_client_cls.return_value = client

    student = _build_graph([
        build_node(
            node_type="equation", node_id="a", attempt_id=1, source="parser",
            content={"symbolic": "x", "label": ""},
        ),  # ACCEPTED
        build_node(
            node_type="equation", node_id="b", attempt_id=1, source="parser",
            content={"symbolic": "y", "label": ""},
            status="DISPUTED",
        ),
        build_node(
            node_type="equation", node_id="c", attempt_id=1, source="parser",
            content={"symbolic": "z", "label": ""},
            status="DUAL", student_belief="my way",
        ),
        build_node(
            node_type="equation", node_id="d", attempt_id=1, source="parser",
            content={"symbolic": "w", "label": ""},
            status="DUAL",
        ),
    ])
    reference = _build_graph([])

    cov = asyncio.run(compute_coverage(student, reference))
    counts = cov["negotiation_counts"]
    assert counts["disputed"] == 1
    assert counts["dual"] == 2
    assert counts["paraphrased"] == 1
    assert counts["skipped"] == 1


@patch("apollo.overseer.coverage.OpenAI")
def test_negotiation_counts_zero_on_pre_p3_graph(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"matches":[]}'))]
    )
    mock_client_cls.return_value = client

    student = _build_graph([
        build_node(
            node_type="equation", node_id="a", attempt_id=1, source="parser",
            content={"symbolic": "x", "label": ""},
        ),
    ])
    cov = asyncio.run(compute_coverage(student, _build_graph([])))
    assert cov["negotiation_counts"] == {
        "dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0,
    }


@patch("apollo.overseer.coverage.OpenAI")
def test_dual_procedure_step_action_substitution_reaches_llm(mock_client_cls):
    """For procedure_step, the action field gets substituted with the
    student's belief — verifying the matcher gets the student's wording
    as the primary action signal, not just an addendum."""
    captured: dict = {}

    def _create(**kwargs):
        captured["body"] = json.loads(kwargs["messages"][1]["content"])
        return MagicMock(choices=[MagicMock(message=MagicMock(
            content='{"score":0.9,"confidence":0.9}'
        ))])

    client = MagicMock()
    client.chat.completions.create.side_effect = _create
    mock_client_cls.return_value = client

    student_step = build_node(
        node_type="procedure_step", node_id="s1", attempt_id=1,
        source="parser",
        content={"action": "use continuity", "purpose": ""},
        status="DUAL",
        student_belief="apply mass conservation across sections",
    )
    student = _build_graph([student_step])
    ref_step = build_node(
        node_type="procedure_step", node_id="r1", attempt_id=1,
        source="reference",
        content={"action": "use continuity", "purpose": ""},
    )
    reference = _build_graph([ref_step])

    asyncio.run(compute_coverage(student, reference))

    body = captured["body"]
    student_steps = body["student_steps"]
    assert len(student_steps) == 1
    assert student_steps[0]["action"] == "apply mass conservation across sections"
    assert student_steps[0]["student_belief"] == (
        "apply mass conservation across sections"
    )
