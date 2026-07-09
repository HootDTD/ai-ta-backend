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


def _argument_reference_solution() -> list[dict]:
    return [
        {
            "step": 1,
            "entry_type": "definition",
            "id": "def_fed",
            "content": {"concept": "federalism", "meaning": "divided sovereignty"},
            "depends_on": [],
        },
        {
            "step": 2,
            "entry_type": "condition",
            "id": "premise",
            "content": {"applies_when": "authority is split across levels"},
            "depends_on": ["def_fed"],
        },
        {
            "step": 3,
            "entry_type": "procedure_step",
            "id": "veto",
            "content": {"order": 1, "action": "identify veto points", "purpose": "show checks"},
            "depends_on": ["premise"],
        },
        {
            "step": 4,
            "entry_type": "procedure_step",
            "id": "concl",
            "content": {
                "order": 2,
                "action": "weigh checks vs blurred responsibility",
                "purpose": "reach verdict",
            },
            "depends_on": ["veto"],
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
    chat = _chat_returning({"reference_solution": _argument_reference_solution()})
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
    chat = _chat_returning({"reference_solution": _argument_reference_solution()})
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
    chat = _chat_returning({"reference_solution": _argument_reference_solution()})
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
    bad = _argument_reference_solution()
    bad.append(
        {
            "step": 5,
            "entry_type": "NOT_A_REAL_TYPE",
            "id": "x",
            "content": {},
            "depends_on": [],
        }
    )
    chat = _chat_returning({"reference_solution": bad})
    with pytest.raises(SolutionDraftError, match="mint map"):
        await construct_authored_reference(authored, chat_fn=chat)


async def test_construct_allows_equation_node_for_prose_subject():
    """Subject-agnostic: an argument problem MAY now carry an ``equation`` step — no
    per-subject vocab forbids it. (The promotion lint's content-derived gates decide
    whether the symbolic rigor applies.) Old code raised here for a 'qualitative'
    subject."""
    authored = _worked_argument()
    mixed = _argument_reference_solution()
    mixed.append(
        {
            "step": 5,
            "entry_type": "equation",
            "id": "eq",
            "content": {"symbolic": "x - y", "label": "eq"},
            "depends_on": [],
        }
    )
    chat = _chat_returning({"reference_solution": mixed})
    draft = await construct_authored_reference(authored, chat_fn=chat)  # no raise
    assert any(s["entry_type"] == "equation" for s in draft.reference_solution)


async def test_construct_fail_closed_on_unparseable():
    authored = _worked_argument()
    chat = _chat_returning("this is not json")
    with pytest.raises(SolutionDraftError):
        await construct_authored_reference(authored, chat_fn=chat)


async def test_construct_fail_closed_on_non_problem_valid():
    """A reference_solution whose depends_on is dangling fails Problem validation
    -> fail-closed."""
    authored = _worked_argument()
    broken = _argument_reference_solution()
    broken[0]["depends_on"] = ["NONEXISTENT"]
    chat = _chat_returning({"reference_solution": broken})
    with pytest.raises(SolutionDraftError, match="not Problem-valid"):
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
            "step": 1,
            "entry_type": "equation",
            "id": "cont",
            "content": {"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "continuity"},
            "depends_on": [],
        },
        {
            "step": 2,
            "entry_type": "procedure_step",
            "id": "solve",
            "content": {
                "order": 1,
                "action": "solve for P2",
                "purpose": "answer",
                "uses_equations": ["cont"],
            },
            "depends_on": ["cont"],
        },
    ]
    chat = _chat_returning({"reference_solution": ref})
    draft = await construct_authored_reference(authored, chat_fn=chat)
    assert draft.solution_source == "authored"


async def test_build_authored_approved_pair_uses_authored_id():
    authored = _worked_argument()
    chat = _chat_returning({"reference_solution": _argument_reference_solution()})
    draft = await construct_authored_reference(authored, chat_fn=chat)
    pair = build_authored_approved_pair(authored, draft, search_space_id=7)
    assert pair.problem["id"] == "authored.fed1"  # matches the Tier-1 problem_code
    assert pair.search_space_id == 7
    assert pair.solution_source == "authored"
