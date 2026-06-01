"""P1.1 / P1.2 — parser_confidence end-to-end through the parser.

Covers:
- Field is on every node type with default 1.0.
- Parser passes the LLM-reported confidence through to the Node.
- Missing or malformed confidence falls back to 1.0 (preserves legacy
  behavior — does not false-fire the P3 OLM Done-gate).
- Range is clipped to [0, 1].
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from apollo.ontology import build_node
from apollo.parser.parser_llm import _entry_to_node, parse_utterance
from apollo.subjects import load_concept


# ---------------------------------------------------------------------------
# Schema-level: every node type carries parser_confidence with default 1.0
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("node_type,content", [
    ("equation", {"symbolic": "A1*v1 - A2*v2", "label": "continuity"}),
    ("condition", {"applies_when": "incompressible flow", "label": "incompr"}),
    ("simplification", {"applies_when": "horizontal pipe", "transformation": "drop rho*g*h"}),
    ("definition", {"concept": "density", "meaning": "mass per unit volume"}),
    ("variable_mapping", {"term": "speed", "symbol": "v"}),
    ("procedure_step", {"action": "apply continuity", "purpose": "find v2"}),
])
def test_default_parser_confidence_is_one(node_type, content):
    """Default 1.0 — non-parser sources are authoritative; legacy data safe."""
    node = build_node(
        node_type=node_type,
        node_id="n1",
        attempt_id=1,
        source="reference",
        content=content,
    )
    assert node.parser_confidence == 1.0


def test_explicit_parser_confidence_round_trips():
    node = build_node(
        node_type="equation",
        node_id="n1",
        attempt_id=1,
        source="parser",
        content={"symbolic": "x", "label": "x"},
        parser_confidence=0.42,
    )
    assert node.parser_confidence == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Entry-level: _entry_to_node respects LLM-reported confidence
# ---------------------------------------------------------------------------

def test_entry_to_node_uses_reported_confidence():
    entry = {
        "type": "equation",
        "content": {"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
        "confidence": 0.55,
    }
    node = _entry_to_node(entry, attempt_id=1, fallback_node_id="x")
    assert node is not None
    assert node.parser_confidence == pytest.approx(0.55)


def test_entry_to_node_missing_confidence_defaults_to_one():
    """Legacy LLM responses without `confidence` must keep the safe default."""
    entry = {
        "type": "equation",
        "content": {"symbolic": "x", "label": "x"},
    }
    node = _entry_to_node(entry, attempt_id=1, fallback_node_id="x")
    assert node is not None
    assert node.parser_confidence == 1.0


def test_entry_to_node_malformed_confidence_falls_back():
    entry = {
        "type": "equation",
        "content": {"symbolic": "x", "label": "x"},
        "confidence": "not a number",
    }
    node = _entry_to_node(entry, attempt_id=1, fallback_node_id="x")
    assert node is not None
    assert node.parser_confidence == 1.0


def test_entry_to_node_clips_confidence_above_one():
    entry = {
        "type": "equation",
        "content": {"symbolic": "x", "label": "x"},
        "confidence": 1.7,
    }
    node = _entry_to_node(entry, attempt_id=1, fallback_node_id="x")
    assert node is not None
    assert node.parser_confidence == 1.0


def test_entry_to_node_clips_confidence_below_zero():
    entry = {
        "type": "equation",
        "content": {"symbolic": "x", "label": "x"},
        "confidence": -0.2,
    }
    node = _entry_to_node(entry, attempt_id=1, fallback_node_id="x")
    assert node is not None
    assert node.parser_confidence == 0.0


# ---------------------------------------------------------------------------
# parse_utterance: confidence flows from the LLM JSON through to the Node
# ---------------------------------------------------------------------------

def _mock_openai_response(entries: list) -> MagicMock:
    fake = MagicMock()
    fake.choices = [
        MagicMock(message=MagicMock(content=json.dumps({"entries": entries})))
    ]
    return fake


@pytest.fixture(scope="module")
def concept():
    return load_concept("fluid_mechanics", "bernoulli_principle")


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_utterance_propagates_confidence(mock_client_cls, concept):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response([
        {
            "type": "equation",
            "content": {"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
            "confidence": 0.45,
        },
    ])
    mock_client_cls.return_value = client

    nodes, _ = parse_utterance(
        "stuff in equals stuff out",
        concept=concept,
        attempt_id=1,
    )
    assert len(nodes) == 1
    assert nodes[0].parser_confidence == pytest.approx(0.45)


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_utterance_legacy_response_defaults_to_one(mock_client_cls, concept):
    """Pre-P1 prompt versions did not include `confidence`. Behavior is
    identical to today: nodes carry confidence 1.0 and never trigger the
    OLM Done-gate."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response([
        {
            "type": "equation",
            "content": {"symbolic": "x", "label": "x"},
        },
    ])
    mock_client_cls.return_value = client

    nodes, _ = parse_utterance(
        "x is something the student wrote at length",
        concept=concept,
        attempt_id=1,
    )
    assert len(nodes) == 1
    assert nodes[0].parser_confidence == 1.0
