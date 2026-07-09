"""WU-3B2x — provisioning prompt↔parser contract tests (PURE, NO DB/network).

These are the deterministic tests that would have RED-flagged the original prompt
stubs: they pin each stage's declared ``json_schema`` to the Pydantic model the
parser requires (single source of truth). Mirrors
``apollo/parser/tests/test_typed_edge_extraction.py:179-295`` (the strict-schema
contract-test exemplar). NO ``db_session`` fixture, NO OpenAI, NO embed — they run
GREEN-not-skipped without Docker/Postgres and without an API key.

DISCRIMINATING by design:
  * ``test_solution_schema_outer_keys_match_reference_step`` REDs if a
    ``ReferenceStep`` field is added/removed without updating the schema (or vice
    versa) — THE test that catches the Stage-2 stub.
  * ``test_tag_schema_is_strict_and_requires_concept_slug`` REDs if the Stage-4
    tag schema drops strict/closure or the ``concept_slug`` requirement — THE test
    that catches the Stage-4 stub.
"""

from __future__ import annotations

import json

import pytest

from apollo.ontology.nodes import NODE_CONTENT_TYPES
from apollo.provisioning.provisioning_schema import (
    REFERENCE_STEP_FIELDS,
    build_pairing_phase_a_schema,
    build_pairing_phase_b_schema,
    build_solution_schema,
    build_tag_schema,
    solution_content_field_hints,
)
from apollo.schemas.problem import ReferenceStep

# pytest.ini sets asyncio_mode = auto, so async tests need no mark; the pure tests
# stay sync. All tests here are pure logic.
pytestmark = pytest.mark.unit


def _iter_objects(obj: dict):
    """Yield every nested JSON-schema object node (type == 'object'). Mirrors the
    parser test's walker (test_typed_edge_extraction.py:220-228)."""
    if obj.get("type") == "object":
        yield obj
    for v in obj.get("properties", {}).values():
        yield from _iter_objects(v)
    if "items" in obj:
        yield from _iter_objects(obj["items"])


# --------------------------------------------------------------------------- #
# Stage 2 — solution schema ↔ ReferenceStep
# --------------------------------------------------------------------------- #


def test_solution_schema_outer_keys_match_reference_step():
    """The reference_solution item's declared key set == ``ReferenceStep`` fields.
    THE test that would have caught the Stage-2 stub. DISCRIMINATING: removing a
    field from the schema OR the model RED-flags."""
    item = build_solution_schema()["schema"]["properties"]["reference_solution"]["items"]
    expected = set(ReferenceStep.model_fields)
    assert set(item["required"]) == expected
    assert set(item["properties"].keys()) == expected
    # REFERENCE_STEP_FIELDS is the single source — same set, derived from the model.
    assert set(REFERENCE_STEP_FIELDS) == expected


def test_solution_schema_wrapper_key_is_reference_solution():
    """Top-level required == ['reference_solution'] — the SAME wrapper key
    ``_parse_reference_solution`` reads (``parsed.get('reference_solution')``)."""
    schema = build_solution_schema()["schema"]
    assert schema["required"] == ["reference_solution"]
    assert "reference_solution" in schema["properties"]
    assert schema["properties"]["reference_solution"]["type"] == "array"


def test_solution_schema_entry_type_enum_is_six_ontology_types():
    """The entry_type enum == the six ontology entry types (== EntryType literal)."""
    item = build_solution_schema()["schema"]["properties"]["reference_solution"]["items"]
    assert set(item["properties"]["entry_type"]["enum"]) == set(NODE_CONTENT_TYPES)
    assert len(item["properties"]["entry_type"]["enum"]) == 6


def test_solution_schema_is_not_strict_content_is_open():
    """Stage-2 schema is NON-strict (``strict=False``) because ``content`` is an
    open per-type dict (Decision #2 — ``Problem.model_validate`` enforces it). The
    content property is a permissive object (no additionalProperties:false)."""
    schema = build_solution_schema()
    assert schema["strict"] is False
    item = schema["schema"]["properties"]["reference_solution"]["items"]
    content = item["properties"]["content"]
    assert content["type"] == "object"
    assert "additionalProperties" not in content


def test_solution_content_hints_name_every_entry_type_field():
    """``solution_content_field_hints()`` names every per-entry_type content field
    sourced from ``NODE_CONTENT_TYPES`` (plus the two procedure_step raw extras),
    so the prose can never drift from the ontology."""
    hints = solution_content_field_hints()
    for entry_type, model in NODE_CONTENT_TYPES.items():
        assert entry_type in hints
        for field in model.model_fields:
            assert field in hints
    # the two procedure_step raw-dict extras Problem._resolve_references reads.
    assert "order" in hints
    assert "uses_equations" in hints


def test_solution_schema_object_round_trips_through_problem():
    """A schema-conformant ``reference_solution`` survives ``find_or_generate`` ->
    ``Problem.model_validate`` (no ``SolutionDraftError``). Proves a schema-shaped
    payload is parser-accepted end-to-end. Deterministic stubs, no network."""
    import asyncio

    from apollo.provisioning.scrape import CandidateQuestion
    from apollo.provisioning.solution import ReferenceSolutionDraft, find_or_generate

    reference_solution = [
        {
            "step": 1,
            "entry_type": "equation",
            "id": "bernoulli",
            "content": {
                "label": "Bernoulli equation",
                "symbolic": "P1 + 0.5*rho*v1**2 - P2 - 0.5*rho*v2**2",
            },
            "depends_on": [],
        },
        {
            "step": 2,
            "entry_type": "procedure_step",
            "id": "solve_p2",
            "content": {
                "action": "Solve for P2",
                "purpose": "isolate the unknown pressure",
                "order": 1,
                "uses_equations": ["bernoulli"],
            },
            "depends_on": ["bernoulli"],
        },
    ]
    question = CandidateQuestion(
        problem_text="A fluid speeds up in a pipe; find the downstream pressure P2.",
        given_values={"P1": 200000.0, "v1": 2.0, "rho": 1000.0, "v2": 4.0},
        target_unknown="P2",
        difficulty="intro",
        document_id=7,
        page=3,
        chunk_content_hash="abc123hash",
        concept_slug="bernoulli_principle",
    )

    def _chat(*_a, **_k) -> str:
        return json.dumps({"reference_solution": reference_solution})

    async def _retrieve(*_a, **_k):
        return []

    draft = asyncio.run(find_or_generate(None, question, retrieve_fn=_retrieve, chat_fn=_chat))
    assert isinstance(draft, ReferenceSolutionDraft)
    assert len(draft.reference_solution) == 2


# --------------------------------------------------------------------------- #
# Stage 4 — tag schema ↔ _parse_tag
# --------------------------------------------------------------------------- #


def test_tag_schema_is_strict_and_requires_concept_slug():
    """Stage-4 tag schema is strict, every object closed, all three keys required,
    prereqs items require {from,to}. THE test that would have caught the Stage-4
    stub. DISCRIMINATING: dropping strict/closure or the concept_slug requirement
    RED-flags."""
    schema = build_tag_schema()
    assert schema["strict"] is True
    assert schema["name"] == "concept_tag"
    root = schema["schema"]
    # every object node: additionalProperties False + required == properties keys.
    for sub in _iter_objects(root):
        assert sub["additionalProperties"] is False
        assert set(sub["required"]) == set(sub["properties"].keys())
    assert set(root["required"]) == {"concept_slug", "display_name", "prereqs"}
    prereq_item = root["properties"]["prereqs"]["items"]
    assert set(prereq_item["required"]) == {"from", "to"}


def test_tag_schema_concept_slug_satisfies_parse_tag():
    """A minimal schema-conformant object runs through ``_parse_tag`` without a
    ``TagMintError`` — the declared schema is parser-accepted (deterministic, no
    network)."""
    from apollo.provisioning.tag_mint import _parse_tag

    obj = {
        "concept_slug": "bernoulli-equation",
        "display_name": "bernoulli-equation",
        "prereqs": [],
    }

    def _chat(_prompt: str) -> str:
        return json.dumps(obj)

    parsed = _parse_tag(_chat, {"id": "p1"})
    assert parsed["concept_slug"] == "bernoulli-equation"


def test_tag_schema_empty_prereqs_and_slug_display_name_conform():
    """An empty ``prereqs: []`` and ``display_name == concept_slug`` satisfy BOTH
    the strict schema (all keys present) and the lenient parser — the consistency
    decision (Decision #5)."""
    from apollo.provisioning.tag_mint import _parse_tag

    obj = {"concept_slug": "x", "display_name": "x", "prereqs": []}
    root = build_tag_schema()["schema"]
    # every required key present in the conformant object.
    for key in root["required"]:
        assert key in obj

    def _chat(_prompt: str) -> str:
        return json.dumps(obj)

    assert _parse_tag(_chat, {})["concept_slug"] == "x"


# --------------------------------------------------------------------------- #
# Stage 3 — pairing-gate phase schemas ↔ what validate_pair reads
# --------------------------------------------------------------------------- #


def test_pairing_phase_a_schema_strict_and_keys_match_reads():
    """Phase-A schema is strict/closed and declares exactly the keys
    ``validate_pair`` reads from the Phase-A response (``paired`` + ``confidence``).
    THE test that would have caught the Stage-3 stub (no system prompt → 400 →
    every pair rejected). DISCRIMINATING: dropping a key or strict closure REDs."""
    schema = build_pairing_phase_a_schema()
    assert schema["strict"] is True
    assert schema["name"] == "pairing_phase_a"
    root = schema["schema"]
    for sub in _iter_objects(root):
        assert sub["additionalProperties"] is False
        assert set(sub["required"]) == set(sub["properties"].keys())
    # The exact keys validate_pair reads: phase_a.get("paired") / .get("confidence").
    assert set(root["required"]) == {"paired", "confidence"}
    assert root["properties"]["paired"]["type"] == "boolean"
    assert root["properties"]["confidence"]["type"] == "number"


def test_pairing_phase_b_schema_strict_and_claims_shape_matches_reads():
    """Phase-B schema is strict/closed and its ``claims`` items declare exactly the
    keys ``validate_pair`` reads (``claim`` + ``entailed``). DISCRIMINATING:
    dropping a key or strict closure REDs."""
    schema = build_pairing_phase_b_schema()
    assert schema["strict"] is True
    assert schema["name"] == "pairing_phase_b"
    root = schema["schema"]
    for sub in _iter_objects(root):
        assert sub["additionalProperties"] is False
        assert set(sub["required"]) == set(sub["properties"].keys())
    assert set(root["required"]) == {"claims"}
    item = root["properties"]["claims"]["items"]
    # The exact keys validate_pair reads: c.get("claim") / c.get("entailed").
    assert set(item["required"]) == {"claim", "entailed"}
    assert item["properties"]["claim"]["type"] == "string"
    assert item["properties"]["entailed"]["type"] == "boolean"
