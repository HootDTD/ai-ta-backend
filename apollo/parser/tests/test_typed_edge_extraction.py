"""WU-2A: context-aware one-call typed-edge extraction with provenance.

Parser-ONLY tests. The GPT-4o call is mocked deterministically
(`@patch("apollo.parser.parser_llm.OpenAI")`); the triviality classifier is
mocked where math-free prose is exercised
(`@patch("apollo.parser.parser_llm.cheap_chat")`). NO Neo4j, NO network, NO
live API, NO `scripts.spikes` import.

Covers (plan "Full test list"):
- A. Edge.provenance default + accept inferred.
- B. GraphContext: is_empty / type_of / frozen.
- C. Strict json_schema invariants.
- D. One-call extraction, backward-compatible (no graph_context).
- E. All four edge types.
- F. Cross-turn linking via injected graph_context.
- G. Provenance tagging (explicit / inferred / default).
- H. Invalid-edge rejection against EDGE_ALLOWED_PAIRS (no silent drop).
- I. Triviality + no-fallback contract UNCHANGED.
- J. Prompt template + context rendering.
"""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import ParserCouldNotExtractError
from apollo.ontology import Edge, EdgeType
from apollo.subjects import load_concept

# ---------------------------------------------------------------------------
# Fixtures + mock helpers (mirror test_parser_confidence.py / test_triviality.py)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def concept():
    return load_concept("fluid_mechanics", "bernoulli_principle")


def _mock_strict_response(entries: list, edges: list | None = None) -> MagicMock:
    """Fake OpenAI response: choices[0].message.content is a strict JSON string
    with `entries` and `edges` (the WU-2A one-call shape)."""
    payload: dict = {"entries": entries}
    if edges is not None:
        payload["edges"] = edges
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=json.dumps(payload)))]
    return fake


def _mock_client(entries: list, edges: list | None = None) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_strict_response(entries, edges)
    return client


# Reusable entry literals — FLAT (every node field sits directly on the entry,
# per the strict json_schema; NO nested "content"). Helpers fill the rest of
# the strict-required fields with null so each literal is schema-conformant.
def _flat_entry(**fields) -> dict:
    """A FLAT, strict-schema-conformant entry: all 15 entry fields present,
    the ones not supplied set to null (the strict schema requires every key)."""
    base = {
        "type": None,
        "confidence": 1.0,
        "reuse_of": None,
        "symbolic": None,
        "label": None,
        "variables": None,
        "applies_when": None,
        "transformation": None,
        "concept": None,
        "meaning": None,
        "term": None,
        "symbol": None,
        "action": None,
        "purpose": None,
        "uses_equation_ordinals": None,
    }
    base.update(fields)
    return base


_EQ = _flat_entry(type="equation", symbolic="A1*v1 - A2*v2", label="continuity")
_STEP = _flat_entry(type="procedure_step", action="apply continuity", purpose="find v2")
_STEP2 = _flat_entry(type="procedure_step", action="apply bernoulli", purpose="find P2")
_COND = _flat_entry(type="condition", applies_when="incompressible flow", label="incompr")
_SIMP = _flat_entry(
    type="simplification", applies_when="horizontal pipe", transformation="drop rho*g*h"
)
_DEF = _flat_entry(type="definition", concept="density", meaning="mass per volume")

_LONG = "the student writes a long teaching explanation A1*v1 = A2*v2 here"


# ===========================================================================
# A. Edge model — provenance
# ===========================================================================


def test_edge_provenance_defaults_explicit():
    edge = Edge(
        edge_type=EdgeType.USES,
        from_node_id="s1",
        to_node_id="e1",
        attempt_id=1,
        source="parser",
        from_node_type="procedure_step",
        to_node_type="equation",
    )
    assert edge.provenance == "explicit"


def test_edge_provenance_accepts_inferred():
    edge = Edge(
        edge_type=EdgeType.SCOPES,
        from_node_id="c1",
        to_node_id="e1",
        attempt_id=1,
        source="parser",
        from_node_type="condition",
        to_node_type="equation",
        provenance="inferred",
    )
    assert edge.provenance == "inferred"


# ===========================================================================
# B. GraphContext
# ===========================================================================


def test_graph_context_is_empty():
    from apollo.parser.graph_context import ContextNode, GraphContext

    assert GraphContext().is_empty() is True
    populated = GraphContext(
        nodes=(ContextNode(node_id="eq_prev", node_type="equation", label="bernoulli"),)
    )
    assert populated.is_empty() is False


def test_graph_context_type_of():
    from apollo.parser.graph_context import ContextNode, GraphContext

    ctx = GraphContext(
        nodes=(
            ContextNode(node_id="eq_prev", node_type="equation", label="bernoulli"),
            ContextNode(node_id="def_prev", node_type="definition", label="density"),
        )
    )
    assert ctx.type_of("eq_prev") == "equation"
    assert ctx.type_of("def_prev") == "definition"
    assert ctx.type_of("missing") is None


def test_graph_context_is_frozen():
    from apollo.parser.graph_context import ContextNode, GraphContext

    ctx = GraphContext(
        nodes=(ContextNode(node_id="eq_prev", node_type="equation", label="bernoulli"),)
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.nodes = ()  # type: ignore[misc]
    node = ctx.nodes[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.label = "changed"  # type: ignore[misc]


# ===========================================================================
# C. Strict json_schema
# ===========================================================================


def test_extraction_schema_is_strict():
    from apollo.parser.extraction_schema import build_extraction_schema

    schema = build_extraction_schema()
    # Top-level strict-mode invariants.
    assert schema["strict"] is True
    assert schema["name"] == "kg_extraction"
    root = schema["schema"]
    assert root["type"] == "object"
    assert root["additionalProperties"] is False
    assert set(root["required"]) == {"entries", "edges"}

    # Every object node must have additionalProperties False and list EVERY
    # property key in `required` (OpenAI structured-outputs invariant).
    def _assert_strict_object(obj: dict) -> None:
        for sub in _iter_objects(obj):
            assert sub["additionalProperties"] is False
            assert set(sub["required"]) == set(sub["properties"].keys())

    _assert_strict_object(root)

    entry_item = root["properties"]["entries"]["items"]
    assert entry_item["additionalProperties"] is False
    assert set(entry_item["required"]) == set(entry_item["properties"].keys())

    edge_item = root["properties"]["edges"]["items"]
    assert edge_item["additionalProperties"] is False
    assert set(edge_item["required"]) == set(edge_item["properties"].keys())

    # provenance enum present on edges, exactly the two values.
    assert edge_item["properties"]["provenance"]["enum"] == ["explicit", "inferred"]
    # edge_type enum is exactly the four EdgeType values.
    assert set(edge_item["properties"]["edge_type"]["enum"]) == {
        "PRECEDES",
        "USES",
        "DEPENDS_ON",
        "SCOPES",
    }
    assert set(edge_item["properties"]["edge_type"]["enum"]) == {e.value for e in EdgeType}


def _iter_objects(obj: dict):
    """Yield every nested JSON-schema object node (type == 'object')."""
    if obj.get("type") == "object":
        yield obj
    for key in ("properties",):
        for v in obj.get(key, {}).values():
            yield from _iter_objects(v)
    if "items" in obj:
        yield from _iter_objects(obj["items"])


def _json_type(value: object) -> str:
    """Map a Python value to its JSON-schema primitive type name."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise AssertionError(f"unmappable value type: {value!r}")


def _assert_conforms(value: object, schema: dict, path: str = "") -> None:
    """Minimal OpenAI-strict json_schema validator (no external dependency).

    Enforces exactly the invariants the production call relies on: declared
    type(s)/nullability, enum membership, `required` completeness, and
    `additionalProperties: false`. Recurses into object properties and array
    items. Used to prove every mock payload is what a strict GPT-4o call could
    actually return (review-required: payloads must validate against
    build_extraction_schema())."""
    allowed = schema.get("type")
    if allowed is not None:
        allowed_set = {allowed} if isinstance(allowed, str) else set(allowed)
        actual = _json_type(value)
        # JSON-schema: an integer is a valid `number`.
        ok = actual in allowed_set or (actual == "integer" and "number" in allowed_set)
        assert ok, f"{path or '<root>'}: type {actual} not in {sorted(allowed_set)}"
    if "enum" in schema and value is not None:
        assert value in schema["enum"], f"{path}: {value!r} not in enum {schema['enum']}"
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            assert not extra, f"{path}: additional properties {extra}"
        for key in schema.get("required", []):
            assert key in value, f"{path}: missing required key {key!r}"
        for key, sub in props.items():
            if key in value:
                _assert_conforms(value[key], sub, f"{path}.{key}" if path else key)
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            _assert_conforms(item, schema["items"], f"{path}[{i}]")


def test_fixture_entries_conform_to_strict_schema(concept):
    """Every reusable FLAT fixture entry validates against the strict
    json_schema the production call enforces. Guards against the prior bug
    where fixtures used a nested `content` shape the schema forbids, so a
    schema-conformant LLM response could never produce nodes."""
    from apollo.parser.extraction_schema import build_extraction_schema

    schema = build_extraction_schema()["schema"]
    payload = {
        "entries": [_EQ, _STEP, _STEP2, _COND, _SIMP, _DEF],
        "edges": [_edge("USES", "n1", "n0", "explicit")],
    }
    _assert_conforms(payload, schema)


@patch("apollo.parser.parser_llm.OpenAI")
def test_schema_conformant_payload_yields_nodes(mock_cls, concept):
    """A payload that validates against build_extraction_schema() (FLAT shape)
    must produce nodes through parse_utterance. This is the regression guard
    for the dead-node-path bug: the prior `"content" in e` gate dropped every
    strict-conformant entry, yielding zero nodes in production."""
    from apollo.parser.extraction_schema import build_extraction_schema
    from apollo.parser.parser_llm import parse_utterance

    schema = build_extraction_schema()["schema"]
    payload_edges = [_edge("USES", "n1", "n0", "explicit")]
    _assert_conforms({"entries": [_EQ, _STEP], "edges": payload_edges}, schema)

    client = _mock_client(entries=[_EQ, _STEP], edges=payload_edges)
    mock_cls.return_value = client
    nodes, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert len(nodes) == 2
    assert {n.node_type for n in nodes} == {"equation", "procedure_step"}
    # The flat fields reached the typed content payloads (adapter works).
    eq_node = next(n for n in nodes if n.node_type == "equation")
    assert eq_node.content.symbolic == "A1*v1 - A2*v2"
    assert len(edges) == 1


def _edge(edge_type, from_ref, to_ref, provenance=None):
    e = {"edge_type": edge_type, "from_ref": from_ref, "to_ref": to_ref}
    if provenance is not None:
        e["provenance"] = provenance
    return e


def _find(edges, edge_type):
    return [e for e in edges if e.edge_type == edge_type]


# ===========================================================================
# D. One-call extraction, backward-compatible (no graph_context)
# ===========================================================================


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_backward_compat_no_graph_context(mock_cls, concept):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(
        entries=[_EQ, _STEP],
        edges=[_edge("USES", "n1", "n0", "explicit")],
    )
    mock_cls.return_value = client

    nodes, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)

    assert len(nodes) == 2
    assert len(edges) == 1
    assert edges[0].edge_type == EdgeType.USES
    # The strict json_schema response_format was used (not json_object).
    _, kwargs = client.chat.completions.create.call_args
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["name"] == "kg_extraction"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_emits_typed_edges_from_strict_payload(mock_cls, concept):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(
        entries=[_STEP, _EQ],
        edges=[_edge("USES", "n0", "n1", "explicit")],
    )
    mock_cls.return_value = client

    nodes, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert len(edges) == 1
    e = edges[0]
    assert e.edge_type == EdgeType.USES
    assert e.from_node_id == nodes[0].node_id  # n0 -> first entry's node
    assert e.to_node_id == nodes[1].node_id  # n1 -> second entry's node
    assert e.provenance == "explicit"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_no_model_edges_falls_back_to_deterministic(mock_cls, concept):
    """Empty `edges` + graph_context None => today's deterministic fallback:
    within-turn USES (from uses_equation_ordinals) + PRECEDES chain."""
    from apollo.parser.parser_llm import parse_utterance

    step_a = _flat_entry(
        type="procedure_step",
        action="apply continuity",
        purpose="find v2",
        uses_equation_ordinals=[2],
    )
    step_b = _flat_entry(
        type="procedure_step",
        action="apply bernoulli",
        purpose="find P2",
    )
    client = _mock_client(entries=[step_a, step_b, _EQ], edges=[])
    mock_cls.return_value = client

    nodes, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    uses = _find(edges, EdgeType.USES)
    precedes = _find(edges, EdgeType.PRECEDES)
    assert len(uses) == 1
    assert len(precedes) == 1
    assert all(e.provenance == "explicit" for e in edges)


# ===========================================================================
# E. All four edge types
# ===========================================================================


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_emits_precedes_step_to_step(mock_cls, concept):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(
        entries=[_STEP, _STEP2],
        edges=[_edge("PRECEDES", "n0", "n1", "explicit")],
    )
    mock_cls.return_value = client
    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert len(_find(edges, EdgeType.PRECEDES)) == 1


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_emits_uses_step_to_equation(mock_cls, concept):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(
        entries=[_STEP, _EQ],
        edges=[_edge("USES", "n0", "n1", "explicit")],
    )
    mock_cls.return_value = client
    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert len(_find(edges, EdgeType.USES)) == 1


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_emits_scopes_condition_to_equation(mock_cls, concept):
    """SCOPES comes alive (§4 — dead code in the deterministic parser)."""
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(
        entries=[_COND, _EQ],
        edges=[_edge("SCOPES", "n0", "n1", "explicit")],
    )
    mock_cls.return_value = client
    nodes, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    scopes = _find(edges, EdgeType.SCOPES)
    assert len(scopes) == 1
    assert scopes[0].from_node_type == "condition"
    assert scopes[0].to_node_type == "equation"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_emits_scopes_simplification_to_equation(mock_cls, concept):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(
        entries=[_SIMP, _EQ],
        edges=[_edge("SCOPES", "n0", "n1", "explicit")],
    )
    mock_cls.return_value = client
    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    scopes = _find(edges, EdgeType.SCOPES)
    assert len(scopes) == 1
    assert scopes[0].from_node_type == "simplification"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_emits_depends_on_any_to_any(mock_cls, concept):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(
        entries=[_EQ, _DEF],
        edges=[_edge("DEPENDS_ON", "n0", "n1", "explicit")],
    )
    mock_cls.return_value = client
    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    dep = _find(edges, EdgeType.DEPENDS_ON)
    assert len(dep) == 1
    assert dep[0].from_node_type == "equation"
    assert dep[0].to_node_type == "definition"


# ===========================================================================
# F. Cross-turn linking via injected graph_context
# ===========================================================================


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_links_to_existing_graph_node(mock_cls, concept):
    from apollo.parser.graph_context import ContextNode, GraphContext
    from apollo.parser.parser_llm import parse_utterance

    ctx = GraphContext(
        nodes=(ContextNode(node_id="eq_prev", node_type="equation", label="bernoulli"),)
    )
    client = _mock_client(
        entries=[_COND],
        edges=[_edge("SCOPES", "n0", "eq_prev", "explicit")],
    )
    mock_cls.return_value = client

    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1, graph_context=ctx)
    scopes = _find(edges, EdgeType.SCOPES)
    assert len(scopes) == 1
    assert scopes[0].to_node_id == "eq_prev"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_cross_turn_late_condition_scopes_earlier_equation(mock_cls, concept):
    """§4 spike scenario: a late condition SCOPES-links to an earlier-turn
    equation supplied via graph_context."""
    from apollo.parser.graph_context import ContextNode, GraphContext
    from apollo.parser.parser_llm import parse_utterance

    ctx = GraphContext(
        nodes=(ContextNode(node_id="t0_n0", node_type="equation", label="bernoulli P+..."),)
    )
    late_cond = _flat_entry(
        type="condition",
        applies_when="flow is steady and incompressible",
        label="steady",
    )
    client = _mock_client(
        entries=[late_cond],
        edges=[_edge("SCOPES", "n0", "t0_n0", "inferred")],
    )
    mock_cls.return_value = client

    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1, graph_context=ctx)
    scopes = _find(edges, EdgeType.SCOPES)
    assert len(scopes) == 1
    assert scopes[0].to_node_id == "t0_n0"
    assert scopes[0].provenance == "inferred"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_cross_turn_endpoint_type_from_context(mock_cls, concept):
    """Endpoint type for a context node is read from graph_context.type_of,
    NOT re-derived. A DEPENDS_ON to a context `definition` validates."""
    from apollo.parser.graph_context import ContextNode, GraphContext
    from apollo.parser.parser_llm import parse_utterance

    ctx = GraphContext(
        nodes=(ContextNode(node_id="def_prev", node_type="definition", label="density"),)
    )
    client = _mock_client(
        entries=[_EQ],
        edges=[_edge("DEPENDS_ON", "n0", "def_prev", "explicit")],
    )
    mock_cls.return_value = client

    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1, graph_context=ctx)
    dep = _find(edges, EdgeType.DEPENDS_ON)
    assert len(dep) == 1
    assert dep[0].to_node_id == "def_prev"
    assert dep[0].to_node_type == "definition"


# ===========================================================================
# G. Provenance tagging
# ===========================================================================


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_tags_explicit_edge(mock_cls, concept):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[_STEP, _EQ], edges=[_edge("USES", "n0", "n1", "explicit")])
    mock_cls.return_value = client
    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert edges[0].provenance == "explicit"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_tags_inferred_edge(mock_cls, concept):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[_STEP, _EQ], edges=[_edge("USES", "n0", "n1", "inferred")])
    mock_cls.return_value = client
    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert edges[0].provenance == "inferred"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_missing_provenance_defaults_explicit(mock_cls, concept):
    """Defensive path: an edge with no provenance coerces to explicit."""
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[_STEP, _EQ], edges=[_edge("USES", "n0", "n1")])
    mock_cls.return_value = client
    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert edges[0].provenance == "explicit"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_invalid_provenance_coerces_explicit(mock_cls, concept):
    """An out-of-vocabulary provenance value coerces to the safe default."""
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[_STEP, _EQ], edges=[_edge("USES", "n0", "n1", "garbage")])
    mock_cls.return_value = client
    _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert edges[0].provenance == "explicit"


# ===========================================================================
# H. Invalid-edge rejection against EDGE_ALLOWED_PAIRS (no silent drop)
# ===========================================================================


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_rejects_disallowed_pair(mock_cls, concept, caplog):
    from apollo.parser.parser_llm import parse_utterance

    # SCOPES with equation -> condition (reversed; not allowed).
    client = _mock_client(entries=[_EQ, _COND], edges=[_edge("SCOPES", "n0", "n1", "explicit")])
    mock_cls.return_value = client
    with caplog.at_level("INFO"):
        _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert _find(edges, EdgeType.SCOPES) == []
    assert "parser_edge_rejected" in caplog.text
    assert "disallowed_pair" in caplog.text


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_rejects_self_loop(mock_cls, concept, caplog):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[_EQ, _DEF], edges=[_edge("DEPENDS_ON", "n0", "n0", "explicit")])
    mock_cls.return_value = client
    with caplog.at_level("INFO"):
        nodes, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert edges == []
    assert len(nodes) == 2  # valid nodes survive
    assert "self_loop" in caplog.text


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_rejects_unresolvable_ref(mock_cls, concept, caplog):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[_STEP, _EQ], edges=[_edge("USES", "n0", "n99", "explicit")])
    mock_cls.return_value = client
    with caplog.at_level("INFO"):
        _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert edges == []
    assert "unresolvable_ref" in caplog.text


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_rejects_unknown_endpoint_type(mock_cls, concept, caplog):
    """A bare id not in graph_context (type unknown) is dropped + logged."""
    from apollo.parser.graph_context import GraphContext
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[_COND], edges=[_edge("SCOPES", "n0", "ghost_id", "explicit")])
    mock_cls.return_value = client
    with caplog.at_level("INFO"):
        _, edges = parse_utterance(
            _LONG, concept=concept, attempt_id=1, graph_context=GraphContext()
        )
    assert edges == []
    assert "unresolvable_ref" in caplog.text or "unknown_endpoint_type" in caplog.text


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_rejects_bad_edge_type(mock_cls, concept, caplog):
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(
        entries=[_STEP, _EQ], edges=[_edge("RESOLVES_TO", "n0", "n1", "explicit")]
    )
    mock_cls.return_value = client
    with caplog.at_level("INFO"):
        _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert edges == []
    assert "bad_edge_type" in caplog.text


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_one_bad_edge_does_not_drop_valid_edges(mock_cls, concept, caplog):
    """One valid USES + one disallowed SCOPES: the USES survives."""
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(
        entries=[_STEP, _EQ, _COND],
        edges=[
            _edge("USES", "n0", "n1", "explicit"),  # valid
            _edge("SCOPES", "n1", "n2", "explicit"),  # equation->condition: bad
        ],
    )
    mock_cls.return_value = client
    with caplog.at_level("INFO"):
        _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert len(_find(edges, EdgeType.USES)) == 1
    assert _find(edges, EdgeType.SCOPES) == []
    assert "disallowed_pair" in caplog.text


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_rejects_malformed_non_dict_edge(mock_cls, concept, caplog):
    """A non-dict element in `edges` is dropped + logged `malformed_edge`."""
    from apollo.parser.parser_llm import parse_utterance

    # _mock_client builds the payload; inject a non-dict edge directly.
    client = MagicMock()
    payload = {
        "entries": [_STEP, _EQ],
        "edges": ["not-a-dict", _edge("USES", "n0", "n1", "explicit")],
    }
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps(payload)))]
    )
    mock_cls.return_value = client
    with caplog.at_level("INFO"):
        _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    # The one valid USES survives; the malformed string edge is dropped.
    assert len(_find(edges, EdgeType.USES)) == 1
    assert "malformed_edge" in caplog.text


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_rejects_bare_id_with_no_context(mock_cls, concept, caplog):
    """A bare (non-"n<i>") id when graph_context is None cannot resolve ->
    `unresolvable_ref` (no prior graph to look it up in)."""
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[_STEP, _EQ], edges=[_edge("USES", "n0", "eq_prev", "explicit")])
    mock_cls.return_value = client
    with caplog.at_level("INFO"):
        _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)  # graph_context=None
    assert edges == []
    assert "unresolvable_ref" in caplog.text


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_rejects_known_id_unknown_type(mock_cls, concept, caplog):
    """A bare id the LLM names but that is absent from the supplied
    graph_context resolves to an id with type None -> `unknown_endpoint_type`.
    (Distinct from `n<i>` misses, which are `unresolvable_ref`.)"""
    from apollo.parser.graph_context import ContextNode, GraphContext
    from apollo.parser.parser_llm import parse_utterance

    # Context has eq_prev (equation) but NOT the named "absent_id".
    ctx = GraphContext(
        nodes=(ContextNode(node_id="eq_prev", node_type="equation", label="bernoulli"),)
    )
    client = _mock_client(entries=[_STEP], edges=[_edge("USES", "n0", "absent_id", "explicit")])
    mock_cls.return_value = client
    with caplog.at_level("INFO"):
        _, edges = parse_utterance(_LONG, concept=concept, attempt_id=1, graph_context=ctx)
    assert edges == []
    assert "unknown_endpoint_type" in caplog.text


def test_build_typed_edge_validator_rejected_defensive(caplog, monkeypatch):
    """Defensive belt-and-braces: if the Edge validator raises despite the pair
    pre-check passing, the edge is dropped + logged `validator_rejected`."""
    from apollo.ontology import build_node
    from apollo.parser import edge_resolver
    from apollo.parser.edge_resolver import _build_typed_edge

    step = build_node(
        node_type="procedure_step",
        node_id="s0",
        attempt_id=1,
        source="parser",
        content={"action": "do x", "purpose": "y"},
    )
    eq = build_node(
        node_type="equation",
        node_id="e0",
        attempt_id=1,
        source="parser",
        content={"symbolic": "x", "label": "x"},
    )

    class _BoomEdge:
        def __init__(self, *a, **k):
            raise ValueError("forced validator failure")

    monkeypatch.setattr(edge_resolver, "Edge", _BoomEdge)
    raw = {"edge_type": "USES", "from_ref": "n0", "to_ref": "n1", "provenance": "explicit"}
    with caplog.at_level("INFO"):
        out = _build_typed_edge(
            raw,
            index_to_node={0: step, 1: eq},
            graph_context=None,
            attempt_id=1,
        )
    assert out is None
    assert "validator_rejected" in caplog.text


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_skips_non_dict_and_typeless_entries(mock_cls, concept):
    """`_build_nodes` skips entries that are not dicts or lack a `type` key
    (defensive guard) while keeping the valid ones."""
    from apollo.parser.parser_llm import parse_utterance

    typeless = _flat_entry()  # type=None -> not a recognized node type
    del typeless["type"]  # drop the key entirely to hit the `"type" not in e` guard
    client = MagicMock()
    payload = {
        "entries": ["not-a-dict", typeless, _EQ],  # bad(0), bad(1), good(2)
        "edges": [],
    }
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps(payload)))]
    )
    mock_cls.return_value = client
    nodes, _ = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert len(nodes) == 1
    assert nodes[0].node_type == "equation"


def test_build_precedes_chain_skips_validator_rejection(monkeypatch, concept):
    """Defensive: if `Edge` construction raises ValueError inside the PRECEDES
    chain, that pair is skipped (no abort). Mirrors the edge-resolver
    belt-and-braces guard."""
    from apollo.ontology import build_node
    from apollo.parser import parser_llm
    from apollo.parser.parser_llm import _build_precedes_chain

    steps = [
        build_node(
            node_type="procedure_step",
            node_id=f"s{i}",
            attempt_id=1,
            source="parser",
            content={"action": f"step {i}", "purpose": "p"},
        )
        for i in range(2)
    ]

    class _BoomEdge:
        def __init__(self, *a, **k):
            raise ValueError("forced validator failure")

    monkeypatch.setattr(parser_llm, "Edge", _BoomEdge)
    edges = _build_precedes_chain(steps, attempt_id=1)
    assert edges == []


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_ref_index_survives_skipped_entry(mock_cls, concept):
    """Risk #1: a malformed entry between two good ones must NOT shift the
    "n<i>" ref mapping. Ref keys on the ORIGINAL entry index."""
    from apollo.parser.parser_llm import parse_utterance

    malformed = _flat_entry(type="equation")  # missing required `symbolic` -> skipped
    client = _mock_client(
        entries=[_STEP, malformed, _EQ],  # good(0), bad(1 -> skipped), good(2)
        edges=[_edge("USES", "n0", "n2", "explicit")],
    )
    mock_cls.return_value = client

    nodes, edges = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert len(nodes) == 2  # malformed skipped
    uses = _find(edges, EdgeType.USES)
    assert len(uses) == 1
    # n0 -> the procedure_step node; n2 -> the equation node (index preserved).
    assert uses[0].from_node_id == nodes[0].node_id
    assert uses[0].to_node_id == nodes[1].node_id
    assert nodes[0].node_type == "procedure_step"
    assert nodes[1].node_type == "equation"


# ===========================================================================
# I. Triviality + no-fallback contract UNCHANGED
# ===========================================================================


@patch("apollo.parser.parser_llm.OpenAI")
def test_triviality_short_circuit_unchanged(mock_cls, concept):
    """A short ACK ("ok") returns ([], []) and does NOT raise.

    Behavior is UNCHANGED from the pre-WU-2A parser: there is no pre-call
    short-circuit (the parser always makes the one LLM call), and an empty
    payload for a *trivial* utterance (length floor / ACK list) yields
    ([], []) instead of ParserCouldNotExtractError. The `_is_non_trivial`
    gate is consulted only AFTER the call, to decide raise-vs-empty.
    """
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[], edges=[])
    mock_cls.return_value = client
    nodes, edges = parse_utterance("ok", concept=concept, attempt_id=1)
    assert (nodes, edges) == ([], [])
    # cheap_chat (the triviality classifier) is never reached for an ACK —
    # "ok" short-circuits on the ACK list before the classifier.


@patch("apollo.parser.parser_llm.OpenAI")
def test_raises_on_nontrivial_zero_nodes(mock_cls, concept):
    """Non-trivial (math chars) utterance + empty entries => raise."""
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[], edges=[])
    mock_cls.return_value = client
    with pytest.raises(ParserCouldNotExtractError):
        parse_utterance("A1*v1 = A2*v2 here is my equation", concept=concept, attempt_id=1)


@patch("apollo.parser.parser_llm.cheap_chat")
@patch("apollo.parser.parser_llm.OpenAI")
def test_returns_empty_on_trivial_zero_nodes(mock_cls, mock_cheap, concept):
    """Trivial prose (classifier says not-teaching) + empty entries => ([], [])."""
    from apollo.parser.parser_llm import parse_utterance

    client = _mock_client(entries=[], edges=[])
    mock_cls.return_value = client
    mock_cheap.return_value = json.dumps(
        {"is_teaching": False, "confidence": 0.9, "reason": "chitchat"}
    )
    nodes, edges = parse_utterance(
        "alright that all sounds good to me, thanks", concept=concept, attempt_id=1
    )
    assert (nodes, edges) == ([], [])


def _mock_client_raw(content: str) -> MagicMock:
    """Fake OpenAI client whose response content is an arbitrary raw string."""
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=content))]
    client = MagicMock()
    client.chat.completions.create.return_value = fake
    return client


@patch("apollo.parser.parser_llm.OpenAI")
def test_raises_on_invalid_json_when_nontrivial(mock_cls, concept):
    """Non-JSON LLM content + non-trivial (math) utterance => raise (unchanged)."""
    from apollo.parser.parser_llm import parse_utterance

    mock_cls.return_value = _mock_client_raw("not json at all {{{")
    with pytest.raises(ParserCouldNotExtractError):
        parse_utterance("A1*v1 = A2*v2 here is my equation", concept=concept, attempt_id=1)


@patch("apollo.parser.parser_llm.cheap_chat")
@patch("apollo.parser.parser_llm.OpenAI")
def test_returns_empty_on_invalid_json_when_trivial(mock_cls, mock_cheap, concept):
    """Non-JSON LLM content + trivial prose => ([], []) (no raise, unchanged)."""
    from apollo.parser.parser_llm import parse_utterance

    mock_cls.return_value = _mock_client_raw("garbage not-json output")
    mock_cheap.return_value = json.dumps(
        {"is_teaching": False, "confidence": 0.9, "reason": "chitchat"}
    )
    nodes, edges = parse_utterance(
        "alright that all sounds good to me, thanks", concept=concept, attempt_id=1
    )
    assert (nodes, edges) == ([], [])


@patch("apollo.parser.parser_llm.OpenAI")
def test_node_parser_confidence_still_propagates(mock_cls, concept):
    """The P1 confidence contract still holds under the strict-schema path."""
    from apollo.parser.parser_llm import parse_utterance

    eq = _flat_entry(
        type="equation",
        symbolic="A1*v1 - A2*v2",
        label="continuity",
        confidence=0.45,
    )
    client = _mock_client(entries=[eq], edges=[])
    mock_cls.return_value = client
    nodes, _ = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert nodes[0].parser_confidence == pytest.approx(0.45)


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_is_pure_function_replay(mock_cls, concept):
    """Same canned response twice => structurally-equal edge topology +
    provenance (node ids differ only by the uuid4 fallback)."""
    from apollo.parser.parser_llm import parse_utterance

    def _topology(nodes, edges):
        idx = {n.node_id: i for i, n in enumerate(nodes)}
        return (
            [n.node_type for n in nodes],
            sorted(
                (e.edge_type, idx[e.from_node_id], idx[e.to_node_id], e.provenance) for e in edges
            ),
        )

    payload_entries = [_STEP, _EQ]
    payload_edges = [_edge("USES", "n0", "n1", "explicit")]

    mock_cls.return_value = _mock_client(payload_entries, payload_edges)
    out1 = parse_utterance(_LONG, concept=concept, attempt_id=1)
    mock_cls.return_value = _mock_client(payload_entries, payload_edges)
    out2 = parse_utterance(_LONG, concept=concept, attempt_id=1)
    assert _topology(*out1) == _topology(*out2)


# ===========================================================================
# J. Prompt template + context rendering
# ===========================================================================


def test_prompt_includes_edge_vocabulary(concept):
    from apollo.parser.prompt_builder import build_system_prompt

    low = build_system_prompt(concept).lower()
    for edge in ("precedes", "uses", "scopes", "depends_on"):
        assert edge in low, f"{edge} missing from prompt"
    # endpoint-pair phrasing present (condition/simplification -> equation, etc.)
    assert "procedure_step" in low
    assert "existing graph" in low


def test_render_graph_context_empty_vs_populated(concept):
    from apollo.parser.graph_context import ContextNode, GraphContext
    from apollo.parser.parser_llm import _render_graph_context

    empty_block = _render_graph_context(None)
    assert "empty" in empty_block.lower()
    assert _render_graph_context(GraphContext()) == empty_block

    ctx = GraphContext(
        nodes=(
            ContextNode(node_id="eq_prev", node_type="equation", label="bernoulli"),
            ContextNode(node_id="def_prev", node_type="definition", label="density"),
        )
    )
    block = _render_graph_context(ctx)
    assert "eq_prev" in block
    assert "equation" in block
    assert "bernoulli" in block
    assert "def_prev" in block
    assert "density" in block


def test_render_graph_context_injected_into_user_message(concept):
    """When graph_context is supplied, its block reaches the LLM call."""
    from apollo.parser.graph_context import ContextNode, GraphContext

    ctx = GraphContext(
        nodes=(ContextNode(node_id="eq_prev", node_type="equation", label="bernoulli"),)
    )
    with patch("apollo.parser.parser_llm.OpenAI") as mock_cls:
        from apollo.parser.parser_llm import parse_utterance

        client = _mock_client(entries=[_COND], edges=[])
        mock_cls.return_value = client
        parse_utterance(_LONG, concept=concept, attempt_id=1, graph_context=ctx)
        _, kwargs = client.chat.completions.create.call_args
        sent = json.dumps(kwargs["messages"])
        assert "eq_prev" in sent
        assert "EXISTING GRAPH" in sent or "existing graph" in sent.lower()


def test_prompt_builder_guard_still_passes(concept):
    """The template edit must not regress the existing prompt-builder guard:
    typing instructions remain, no unresolved {{...}} slots."""
    from apollo.parser.prompt_builder import build_system_prompt

    prompt = build_system_prompt(concept)
    low = prompt.lower()
    assert "procedure_step" in low
    assert "equation" in low
    assert "{{concept_name}}" not in prompt
    assert "{{canonical_symbols_csv}}" not in prompt
    assert "{{subscript_convention}}" not in prompt
    assert "canonical symbols for this concept" not in low
