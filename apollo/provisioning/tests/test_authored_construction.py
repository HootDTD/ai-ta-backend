"""Subject-AGNOSTIC Apollo Stage-5 — authored reference-graph construction (pure).

NO DB / NO network: ``chat_fn`` is a deterministic stub. Covers the three
completeness branches, the UNIVERSAL mint-map vocab guard (any of the 6 entry
types is allowed for any subject), the authored grounding, and the fail-closed
property. Construction no longer takes a subject profile — it never forces a
node vocab or a symbolic target shape.
"""

from __future__ import annotations

import json

import pytest

from apollo.provisioning.ingest import AuthoredProblem
from apollo.provisioning.solution import (
    SolutionDraftError,
    build_authored_approved_pair,
    construct_authored_reference,
)
from apollo.schemas.problem import Problem

# pytest.ini sets asyncio_mode = auto.


def _chat_returning(payload):
    def _chat(*_a, **_k) -> str:
        return payload if isinstance(payload, str) else json.dumps(payload)

    return _chat


def _chat_sequence(payloads):
    calls = []

    def _chat(*_a, **kwargs) -> str:
        calls.append(kwargs)
        payload = payloads[len(calls) - 1]
        return payload if isinstance(payload, str) else json.dumps(payload)

    _chat.calls = calls
    return _chat


def _argument_steps() -> list[dict]:
    return [
        {
            "entry_type": "definition",
            "id": "def_fed",
            "content": {"concept": "federalism", "meaning": "divided sovereignty"},
        },
        {
            "entry_type": "condition",
            "id": "premise",
            "content": {"applies_when": "authority is split across levels"},
        },
        {
            "entry_type": "procedure_step",
            "id": "veto",
            "content": {"action": "identify veto points", "purpose": "show checks"},
        },
        {
            "entry_type": "procedure_step",
            "id": "concl",
            "content": {
                "action": "weigh checks vs blurred responsibility",
                "purpose": "reach verdict",
            },
        },
    ]


def _worked_argument() -> AuthoredProblem:
    return AuthoredProblem(
        problem_code="authored.fed1",
        concept_slug="federalism",
        statement="Argue whether federalism strengthens accountability.",
        solution="Federalism creates veto points that both check power and blur blame.",
        worked_procedure=[{"order": 1, "text": "define federalism"}],
        completeness="worked",
    )


async def test_construct_worked_argument():
    authored = _worked_argument()
    chat = _chat_returning({"steps": _argument_steps()})
    draft = await construct_authored_reference(authored, chat_fn=chat)
    assert draft.solution_source == "authored"
    assert draft.provenance["completeness"] == "worked"
    assert draft.provenance["flagged"] is False
    assert "profile_kind" not in draft.provenance  # subject-agnostic: no profile stamp
    # grounding is the AUTHORED solution (not RAG): the professor's text is present.
    assert "AUTHORED SOLUTION" in draft.grounding[0].text
    # the constructed graph is Problem-valid under the authored problem dict
    Problem.model_validate(authored.to_problem_dict(draft.reference_solution))


async def test_construct_answer_only_is_authored_not_flagged():
    authored = AuthoredProblem(
        problem_code="authored.fed2",
        concept_slug="federalism",
        statement="Does separation of powers prevent tyranny?",
        solution="Yes, by distributing authority so ambition checks ambition.",
        completeness="answer_only",
    )
    chat = _chat_returning({"steps": _argument_steps()})
    draft = await construct_authored_reference(authored, chat_fn=chat)
    assert draft.solution_source == "authored"
    assert draft.provenance["flagged"] is False


async def test_construct_none_is_generated_and_flagged():
    authored = AuthoredProblem(
        problem_code="authored.fed3",
        concept_slug="federalism",
        statement="Explain the separation of powers.",
        completeness="none",
    )
    chat = _chat_returning({"steps": _argument_steps()})
    draft = await construct_authored_reference(authored, chat_fn=chat)
    assert draft.solution_source == "generated"
    assert draft.provenance["flagged"] is True  # 'none' is flagged for review


async def test_construct_rejects_node_type_outside_universal_mint_map():
    """A node type NOT in the universal mint map (the 6 entry types) is fail-closed
    -> SolutionDraftError (never a half-built draft). DISCRIMINATING: dropping the
    _validate_authored_node_vocab guard lets it through. An ``equation`` step is now
    ALLOWED for ANY subject (subject-agnostic), so the rejected type must be a
    genuinely-foreign one."""
    authored = _worked_argument()
    bad = _argument_steps()
    bad.append(
        {
            "entry_type": "NOT_A_REAL_TYPE",
            "id": "foreign_node_type",
            "content": {},
        }
    )
    chat = _chat_returning({"steps": bad})
    with pytest.raises(SolutionDraftError, match="entry_type must be one of"):
        await construct_authored_reference(authored, chat_fn=chat)


async def test_construct_allows_equation_node_for_prose_subject():
    """Subject-agnostic: an argument problem MAY now carry an ``equation`` step — no
    per-subject vocab forbids it. (The promotion lint's content-derived gates decide
    whether the symbolic rigor applies.) Old code raised here for a 'qualitative'
    subject."""
    authored = _worked_argument()
    mixed = _argument_steps()
    mixed.append(
        {
            "entry_type": "equation",
            "id": "compare_outcomes",
            "content": {"symbolic": "x - y", "label": "eq"},
        }
    )
    chat = _chat_returning({"steps": mixed, "symbol_table": {"x": {}, "y": {}}})
    draft = await construct_authored_reference(authored, chat_fn=chat)  # no raise
    assert any(s["entry_type"] == "equation" for s in draft.reference_solution)


async def test_construct_fail_closed_on_unparseable():
    authored = _worked_argument()
    chat = _chat_returning("this is not json")
    with pytest.raises(SolutionDraftError):
        await construct_authored_reference(authored, chat_fn=chat)


async def test_construct_fail_closed_on_non_prior_declared_reference():
    """Declared references must resolve to an earlier meaningful step."""
    authored = _worked_argument()
    broken = _argument_steps()
    broken[0]["references"] = ["later_step"]
    chat = _chat_returning({"steps": broken})
    with pytest.raises(SolutionDraftError, match="references non-prior ids"):
        await construct_authored_reference(authored, chat_fn=chat)


async def test_construct_worked_quantitative_with_equation_nodes():
    """A quantitative worked problem constructs with equation/procedure nodes — all
    6 types are in the universal mint map, no profile needed."""
    authored = AuthoredProblem(
        problem_code="authored.flu1",
        concept_slug="bernoulli",
        statement="Find P2 in a horizontal pipe.",
        solution="P2 = 197 kPa",
        worked_procedure=[{"order": 1, "text": "continuity"}],
        given_values={"v1": 2.0},
        target_unknown="P2",
        completeness="worked",
    )
    ref = [
        {
            "entry_type": "equation",
            "id": "cont",
            "content": {"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "continuity"},
        },
        {
            "entry_type": "procedure_step",
            "id": "solve",
            "content": {
                "action": "solve for P2",
                "purpose": "answer",
                "uses_equations": ["cont"],
            },
        },
    ]
    chat = _chat_returning(
        {
            "steps": ref,
            "symbol_table": {"rho": {}, "A1": {}, "A2": {}, "v2": {}},
        }
    )
    draft = await construct_authored_reference(authored, chat_fn=chat)
    assert draft.solution_source == "authored"


async def test_build_authored_approved_pair_uses_authored_id():
    authored = _worked_argument()
    chat = _chat_returning({"steps": _argument_steps()})
    draft = await construct_authored_reference(authored, chat_fn=chat)
    pair = build_authored_approved_pair(authored, draft, search_space_id=7)
    assert pair.problem["id"] == "authored.fed1"  # matches the Tier-1 problem_code
    assert pair.search_space_id == 7
    assert pair.solution_source == "authored"


@pytest.mark.parametrize(
    ("bad_step", "diagnostic"),
    [
        (
            {
                "entry_type": "definition",
                "id": "step_1",
                "content": {"concept": "federalism", "meaning": "divided sovereignty"},
            },
            "semantic_key",
        ),
        (
            {
                "entry_type": "equation",
                "id": "calculate_balance",
                "content": {"symbolic": "answer = missing + 1"},
            },
            "symbol_closure",
        ),
        (
            {
                "entry_type": "equation",
                "id": "calculate_balance",
                "content": {"symbolic": "answer = x(x + 1)"},
            },
            "equation_parse",
        ),
    ],
)
async def test_construct_repairs_one_mechanical_defect(bad_step, diagnostic):
    authored = _worked_argument()
    bad_payload: dict = {"steps": [bad_step]}
    if diagnostic == "equation_parse":
        bad_payload["symbol_table"] = {"x": {}}
    chat = _chat_sequence(
        [
            bad_payload,
            {"steps": _argument_steps()},
        ]
    )

    draft = await construct_authored_reference(authored, chat_fn=chat)

    assert draft.provenance["construction_attempts"] == 2
    assert diagnostic in chat.calls[1]["messages"][-1]["content"]
    assert draft.provenance["construction_diagnostics"][0].startswith("attempt 1:")


@pytest.mark.parametrize(
    "bad_payload",
    [
        pytest.param(["steps", "not", "an", "object"], id="non_dict_response"),
        pytest.param(
            {"steps": _argument_steps(), "unexpected_key": "x"}, id="unsupported_extra_key"
        ),
        pytest.param({"steps": "not-a-list"}, id="steps_not_a_list"),
    ],
)
async def test_construct_repairs_response_schema_defect(bad_payload):
    """A malformed top-level JSON envelope (not an object, an unsupported key, or
    a non-list ``steps``) is caught by the response-schema guard BEFORE
    ``build_ordered_problem`` runs, surfaces as a ``response_schema:`` repair
    diagnostic, and still repairs successfully on the next attempt."""
    chat = _chat_sequence(
        [
            bad_payload,
            {"steps": _argument_steps()},
        ]
    )

    draft = await construct_authored_reference(_worked_argument(), chat_fn=chat)

    assert draft.provenance["construction_attempts"] == 2
    assert draft.provenance["construction_diagnostics"][0].startswith("attempt 1: response_schema:")


async def test_construct_exhaustion_accumulates_every_attempt_diagnostic():
    chat = _chat_sequence(
        [
            {"steps": [{"entry_type": "definition", "id": "step_1", "content": {}}]},
            {
                "steps": [
                    {
                        "entry_type": "equation",
                        "id": "broken_equation",
                        "content": {"symbolic": "x("},
                    }
                ]
            },
            "not json",
        ]
    )

    with pytest.raises(SolutionDraftError) as exc_info:
        await construct_authored_reference(_worked_argument(), chat_fn=chat)

    diagnostic = str(exc_info.value)
    assert "attempt 1:" in diagnostic
    assert "attempt 2:" in diagnostic
    assert "attempt 3:" in diagnostic
    final_prompt = chat.calls[2]["messages"][-1]["content"]
    assert "attempt 1:" in final_prompt
    assert "attempt 2:" in final_prompt
