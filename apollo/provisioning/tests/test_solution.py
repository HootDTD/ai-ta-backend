"""WU-3B2e stage 2 — find-or-generate solution tests (Tier-1 unit, NO DB/network).

Tier-1 ONLY. The retrieval seam (``retrieve_fn``) and the generation/extraction
LLM (``chat_fn``) are DETERMINISTIC injected stubs — there is NO real OpenAI /
``main_chat`` / live retrieval anywhere in this module. The tests never request
``db_session`` (this unit is pure compute over injected callables), so they run
green WITHOUT the Postgres container.

The REAL ``apollo.provisioning.tag_mint.ApprovedPair`` and
``apollo.schemas.problem.Problem`` are IMPORTED (never mocked) so the
``build_approved_pair`` round-trip catches a shape drift in ``tag_mint``.

DISCRIMINATING by design (independent-mutation discipline):
  * ``test_find_or_generate_extracted_branch`` / ``_generated_branch`` /
    ``test_build_approved_pair_extracted_vs_generated_source`` each assert one
    ``solution_source`` path — collapsing the source to a constant REDs them.
  * ``test_find_or_generate_unparseable_generate_raises`` /
    ``_empty_reference_solution_raises`` pin the stage-2 fail-CLOSED property
    (never an empty-step draft).
"""

from __future__ import annotations

import json

import pytest

# IMPORT the REAL types — do NOT mock/redefine.
from apollo.provisioning.scrape import CandidateQuestion

# The not-yet-existing public names (RED on import until solution.py exists).
from apollo.provisioning.solution import (
    _SOLUTION_EXTRACT_SYSTEM_PROMPT,
    _SOLUTION_GENERATE_AUGMENT_SYSTEM_PROMPT,
    _SOLUTION_GENERATE_SYSTEM_PROMPT,
    GroundingSpan,
    ReferenceSolutionDraft,
    SolutionDraftError,
    _parse_generated,
    build_approved_pair,
    find_or_generate,
    solution_hash,
)
from apollo.provisioning.tag_mint import ApprovedPair
from apollo.schemas.problem import Problem

# pytest.ini sets asyncio_mode = auto, so async tests need no mark.


# --------------------------------------------------------------------------- #
# Deterministic stubs (NO network, NO DB)
# --------------------------------------------------------------------------- #


def _chat_returning(payload):
    """A main_chat-shaped sync stub returning a fixed JSON string (or a raw
    string for the non-JSON case). Mirrors test_tag_mint.py:58-65."""

    def _chat(*_a, **_k) -> str:
        return payload if isinstance(payload, str) else json.dumps(payload)

    return _chat


def _recording_chat(payload):
    """A chat stub that records the messages/kwargs it was handed (so a test can
    assert the retrieved span text reached the generator's context)."""
    calls: list[dict] = []

    def _chat(*a, **k) -> str:
        calls.append({"args": a, "kwargs": k})
        return payload if isinstance(payload, str) else json.dumps(payload)

    _chat.calls = calls  # type: ignore[attr-defined]
    return _chat


def _retrieve_returning(spans):
    """An async retrieve_fn returning a fixed list[GroundingSpan]."""

    async def _retrieve(*_a, **_k):
        return list(spans)

    return _retrieve


def _candidate(
    *,
    problem_text: str = "A fluid speeds up in a pipe; find the downstream pressure P2.",
    given_values: dict[str, float] | None = None,
    target_unknown: str = "P2",
    difficulty: str = "intro",
    document_id: int = 7,
    page: int | None = 3,
    chunk_content_hash: str = "abc123hash",
    concept_slug: str = "bernoulli_principle",
) -> CandidateQuestion:
    return CandidateQuestion(
        problem_text=problem_text,
        given_values=given_values
        if given_values is not None
        else {"P1": 200000.0, "v1": 2.0, "rho": 1000.0, "v2": 4.0},
        target_unknown=target_unknown,
        difficulty=difficulty,  # type: ignore[arg-type]
        document_id=document_id,
        page=page,
        chunk_content_hash=chunk_content_hash,
        concept_slug=concept_slug,
    )


def _reference_solution() -> list[dict]:
    """A minimal Problem-validatable reference_solution (bernoulli-shaped, copied
    from test_tag_mint.py:131-154)."""
    return [
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


def _recall_question() -> CandidateQuestion:
    return _candidate(
        problem_text="Define Future Shock.",
        given_values={},
        target_unknown="Future Shock",
        concept_slug="future_shock",
    )


def _definition_only() -> list[dict]:
    return [
        {
            "step": 1,
            "entry_type": "definition",
            "id": "future_shock_definition",
            "content": {
                "concept": "future shock",
                "meaning": "Disorientation caused by too much change in too little time.",
            },
            "depends_on": [],
        }
    ]


def _augmented_steps() -> list[dict]:
    return [
        *_definition_only(),
        {
            "step": 2,
            "entry_type": "procedure_step",
            "id": "state_accelerating_change",
            "content": {
                "action": "State that accelerating change outpaces adaptation.",
                "purpose": "identify the driving mechanism",
                "order": 1,
            },
            "depends_on": ["future_shock_definition"],
        },
        {
            "step": 3,
            "entry_type": "procedure_step",
            "id": "infer_disorientation",
            "content": {
                "action": "Infer the resulting disorientation.",
                "purpose": "explain why the phenomenon follows",
                "order": 2,
            },
            "depends_on": ["state_accelerating_change"],
        },
    ]


def _draft(
    *,
    solution_source: str = "generated",
    reference_solution: list[dict] | None = None,
    grounding: tuple[GroundingSpan, ...] | None = None,
    provenance: dict | None = None,
) -> ReferenceSolutionDraft:
    return ReferenceSolutionDraft(
        solution_source=solution_source,  # type: ignore[arg-type]
        reference_solution=reference_solution
        if reference_solution is not None
        else _reference_solution(),
        grounding=grounding
        if grounding is not None
        else (GroundingSpan(text="some retrieved course passage"),),
        provenance=provenance
        if provenance is not None
        else {
            "document_id": 7,
            "page": 3,
            "chunk_content_hash": "abc123hash",
            "retrieval_hits": 1,
        },
    )


# A printed/worked-solution span (the extracted branch). The marker the retrieve
# adapter sets is ``carries_solution=True``.
def _printed_solution_span() -> GroundingSpan:
    return GroundingSpan(
        text="Worked solution: apply Bernoulli, solve for P2.",
        document_id=7,
        page=3,
        chunk_content_hash="abc123hash",
        carries_solution=True,
    )


def _context_span() -> GroundingSpan:
    return GroundingSpan(
        text="Pressure and velocity in a pipe relate via the energy balance.",
        document_id=7,
        page=2,
        chunk_content_hash="abc123hash",
    )


# --------------------------------------------------------------------------- #
# Step 1 — types + solution_hash (pure)
# --------------------------------------------------------------------------- #


def test_reference_solution_draft_shape():
    """ReferenceSolutionDraft round-trips; solution_source constrained to
    extracted/generated; a bad source raises."""
    draft = _draft(solution_source="extracted")
    assert draft.solution_source == "extracted"
    assert isinstance(draft.reference_solution, list)
    assert isinstance(draft.grounding, tuple)
    # Pydantic round-trip.
    again = ReferenceSolutionDraft.model_validate(draft.model_dump())
    assert again.solution_source == "extracted"
    assert _draft(solution_source="llm_paired").solution_source == "llm_paired"
    with pytest.raises(Exception):  # noqa: B017
        _draft(solution_source="invented")


def test_reference_solution_draft_accepts_list_grounding():
    """grounding accepts a LIST (coerced to an immutable tuple) — the
    field-validator list branch. DISCRIMINATING: dropping the coercion stores a
    list and breaks the frozen-tuple contract."""
    span = GroundingSpan(text="passage")
    draft = ReferenceSolutionDraft(
        solution_source="generated",
        reference_solution=_reference_solution(),
        grounding=[span],  # a LIST, not a tuple
        provenance={},
    )
    assert isinstance(draft.grounding, tuple)
    assert draft.grounding == (span,)


def test_grounding_span_optional_provenance():
    """A GroundingSpan with only text constructs (provenance fields default)."""
    span = GroundingSpan(text="course material only, no PII")
    assert span.text
    assert span.document_id is None
    assert span.page is None
    assert span.chunk_content_hash is None
    assert span.carries_solution is False


def test_solution_hash_deterministic():
    """solution_hash(draft) == solution_hash(equal draft); differs for a
    different reference_solution."""
    d1 = _draft()
    d2 = _draft()
    assert solution_hash(d1) == solution_hash(d2)

    other_steps = _reference_solution()
    other_steps[0]["content"]["label"] = "A DIFFERENT label"
    d3 = _draft(reference_solution=other_steps)
    assert solution_hash(d3) != solution_hash(d1)


# --------------------------------------------------------------------------- #
# Step 2-4 — find_or_generate branches
# --------------------------------------------------------------------------- #


async def test_find_or_generate_extracted_branch():
    """retrieve_fn returns a span carrying a printed solution → an extraction
    chat_fn pass returns the parsed solution → solution_source=='extracted',
    grounding == the retrieved spans, retrieval_hits>0."""
    chat = _chat_returning({"reference_solution": _reference_solution()})
    draft = await find_or_generate(
        None,
        _candidate(),
        retrieve_fn=_retrieve_returning([_printed_solution_span()]),
        chat_fn=chat,
    )
    assert draft.solution_source == "extracted"
    assert len(draft.grounding) == 1
    assert draft.grounding[0].text == _printed_solution_span().text
    assert draft.provenance["retrieval_hits"] == 1


async def test_find_or_generate_structure_branch_records_llm_paired_source():
    async def _retrieve(_question):
        _retrieve.last_match_method = "structure"
        return [_printed_solution_span()]

    _retrieve.last_match_method = None
    draft = await find_or_generate(
        None,
        _candidate(),
        retrieve_fn=_retrieve,
        chat_fn=_chat_returning({"reference_solution": _reference_solution()}),
    )
    assert draft.solution_source == "llm_paired"


async def test_find_or_generate_generated_branch():
    """retrieve_fn returns context but NO printed solution → chat_fn RAG-generate
    → solution_source=='generated'."""
    chat = _chat_returning({"reference_solution": _reference_solution()})
    draft = await find_or_generate(
        None,
        _candidate(),
        retrieve_fn=_retrieve_returning([_context_span()]),
        chat_fn=chat,
    )
    assert draft.solution_source == "generated"
    assert len(draft.reference_solution) >= 1


async def test_generated_branch_carries_retrieved_grounding():
    """The generated draft's grounding equals the retrieved spans (so Phase B has
    real context); chat_fn was called with the span text in its context."""
    span = _context_span()
    chat = _recording_chat({"reference_solution": _reference_solution()})
    draft = await find_or_generate(
        None,
        _candidate(),
        retrieve_fn=_retrieve_returning([span]),
        chat_fn=chat,
    )
    assert draft.solution_source == "generated"
    assert tuple(s.text for s in draft.grounding) == (span.text,)
    # The retrieved span text reached the generator's call.
    blob = json.dumps(chat.calls)  # type: ignore[attr-defined]
    assert span.text in blob


async def test_extracted_branch_no_solution_marker_falls_to_generate():
    """retrieve returns spans none flagged as a solution → falls to the generate
    branch (the branch boundary between extracted and generated)."""
    chat = _chat_returning({"reference_solution": _reference_solution()})
    draft = await find_or_generate(
        None,
        _candidate(),
        retrieve_fn=_retrieve_returning([_context_span(), _context_span()]),
        chat_fn=chat,
    )
    assert draft.solution_source == "generated"


async def test_find_or_generate_unparseable_generate_raises():
    """retrieve empty + chat_fn non-JSON → SolutionDraftError (NOT an empty-step
    draft). Stage-2 fail-CLOSED."""
    with pytest.raises(SolutionDraftError):
        await find_or_generate(
            None,
            _candidate(),
            retrieve_fn=_retrieve_returning([]),
            chat_fn=_chat_returning("not json {"),
        )


async def test_find_or_generate_empty_reference_solution_raises():
    """chat_fn returns valid JSON but EMPTY reference_solution → SolutionDraftError
    (Problem requires min_length=1)."""
    with pytest.raises(SolutionDraftError):
        await find_or_generate(
            None,
            _candidate(),
            retrieve_fn=_retrieve_returning([]),
            chat_fn=_chat_returning({"reference_solution": []}),
        )


async def test_find_or_generate_non_object_generate_raises():
    """chat_fn returns a JSON ARRAY (not an object) → SolutionDraftError (the
    non-dict parse branch; fail-closed)."""
    with pytest.raises(SolutionDraftError):
        await find_or_generate(
            None,
            _candidate(),
            retrieve_fn=_retrieve_returning([]),
            chat_fn=_chat_returning([1, 2, 3]),
        )


async def test_find_or_generate_non_list_reference_solution_raises():
    """chat_fn returns an object whose reference_solution is NOT a list →
    SolutionDraftError (the non-list parse branch; fail-closed)."""
    with pytest.raises(SolutionDraftError):
        await find_or_generate(
            None,
            _candidate(),
            retrieve_fn=_retrieve_returning([]),
            chat_fn=_chat_returning({"reference_solution": "oops, a string"}),
        )


async def test_find_or_generate_problem_invalid_solution_raises():
    """chat_fn returns a NON-EMPTY reference_solution that is NOT Problem-valid
    (a procedure_step whose order is not 1..N contiguous) → SolutionDraftError
    (the _validate_problem_shape except branch; fail-closed, never a half-valid
    draft reaches the gate)."""
    bad_steps = [
        {
            "step": 1,
            "entry_type": "procedure_step",
            "id": "state_accelerating_change",
            "content": {"action": "do", "purpose": "why", "order": 5},
            "depends_on": [],
        }
    ]
    with pytest.raises(SolutionDraftError):
        await find_or_generate(
            None,
            _candidate(),
            retrieve_fn=_retrieve_returning([]),
            chat_fn=_chat_returning({"reference_solution": bad_steps}),
        )


async def test_find_or_generate_provenance_records_chunk_hash():
    """draft.provenance['chunk_content_hash'] == question.chunk_content_hash (the
    idempotency key threads through)."""
    q = _candidate(chunk_content_hash="deadbeefhash")
    draft = await find_or_generate(
        None,
        q,
        retrieve_fn=_retrieve_returning([_context_span()]),
        chat_fn=_chat_returning({"reference_solution": _reference_solution()}),
    )
    assert draft.provenance["chunk_content_hash"] == "deadbeefhash"


async def test_generate_augments_on_deterministic_retry():
    calls = []

    def chat(*_a, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return json.dumps(
                {
                    "reference_solution": _definition_only(),
                    "augmented_problem_text": None,
                    "augmented_target_unknown": None,
                }
            )
        return json.dumps(
            {
                "reference_solution": _augmented_steps(),
                "augmented_problem_text": "Define Future Shock and explain why it occurs.",
                "augmented_target_unknown": "why future shock occurs",
            }
        )

    draft = await find_or_generate(
        None,
        _recall_question(),
        retrieve_fn=_retrieve_returning([]),
        chat_fn=chat,
        augment_recall=True,
    )
    assert len(calls) == 2
    assert len(calls[1]["messages"]) == len(calls[0]["messages"]) + 1
    assert draft.augmented_problem_text == "Define Future Shock and explain why it occurs."
    assert draft.provenance["augmented"] == "explain_why"


async def test_generate_augments_prompt_first_single_call():
    chat = _recording_chat(
        {
            "reference_solution": _augmented_steps(),
            "augmented_problem_text": "Define Future Shock and explain why it occurs.",
            "augmented_target_unknown": "why future shock occurs",
        }
    )
    draft = await find_or_generate(
        None,
        _recall_question(),
        retrieve_fn=_retrieve_returning([]),
        chat_fn=chat,
        augment_recall=True,
    )
    assert len(chat.calls) == 1  # type: ignore[attr-defined]
    assert draft.augmented_problem_text is not None


async def test_generate_retries_once_then_returns_unaugmented():
    chat = _recording_chat(
        {
            "reference_solution": _definition_only(),
            "augmented_problem_text": None,
            "augmented_target_unknown": None,
        }
    )
    draft = await find_or_generate(
        None,
        _recall_question(),
        retrieve_fn=_retrieve_returning([]),
        chat_fn=chat,
        augment_recall=True,
    )
    assert len(chat.calls) == 2  # type: ignore[attr-defined]
    assert draft.augmented_problem_text is None
    assert draft.augmented_target_unknown is None
    assert "augmented" not in draft.provenance


async def test_generate_discards_rewrite_without_procedure_steps():
    chat = _recording_chat(
        {
            "reference_solution": _definition_only(),
            "augmented_problem_text": "Define Future Shock and explain why it occurs.",
            "augmented_target_unknown": "why future shock occurs",
        }
    )
    draft = await find_or_generate(
        None,
        _recall_question(),
        retrieve_fn=_retrieve_returning([]),
        chat_fn=chat,
        augment_recall=True,
    )
    assert len(chat.calls) == 2  # type: ignore[attr-defined]
    assert draft.augmented_problem_text is None
    assert draft.augmented_target_unknown is None


async def test_generate_default_flag_off_is_byte_identical():
    chat = _recording_chat({"reference_solution": _definition_only()})
    draft = await find_or_generate(
        None,
        _recall_question(),
        retrieve_fn=_retrieve_returning([]),
        chat_fn=chat,
    )
    assert len(chat.calls) == 1  # type: ignore[attr-defined]
    call = chat.calls[0]["kwargs"]  # type: ignore[attr-defined]
    assert call["messages"][0]["content"] == _SOLUTION_GENERATE_SYSTEM_PROMPT
    schema = call["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["reference_solution"]
    # DAG-3: the optional symbol_table property is additive (never required).
    assert set(schema["properties"]) == {"reference_solution", "symbol_table"}
    assert "symbol_table" not in schema["required"]
    assert draft.augmented_problem_text is None
    assert draft.augmented_target_unknown is None
    assert "augmented" not in draft.provenance


def test_generate_augment_prompt_epistemic_honesty_and_no_solvable_framing():
    assert "according to the course" in _SOLUTION_GENERATE_AUGMENT_SYSTEM_PROMPT
    assert "as discussed in class" in _SOLUTION_GENERATE_AUGMENT_SYSTEM_PROMPT
    assert "first principles" in _SOLUTION_GENERATE_AUGMENT_SYSTEM_PROMPT
    for prompt in (
        _SOLUTION_GENERATE_AUGMENT_SYSTEM_PROMPT,
        _SOLUTION_GENERATE_SYSTEM_PROMPT,
        _SOLUTION_EXTRACT_SYSTEM_PROMPT,
    ):
        assert "solvable problem" not in prompt.lower()


@pytest.mark.parametrize(
    "raw",
    ["not-json", json.dumps([]), json.dumps({"reference_solution": "not-a-list"})],
)
def test_parse_generated_rejects_malformed_envelopes(raw):
    assert _parse_generated(raw) == (None, None, None)


async def test_augmented_target_falls_back_to_original_target():
    draft = await find_or_generate(
        None,
        _recall_question(),
        retrieve_fn=_retrieve_returning([]),
        chat_fn=_chat_returning(
            {
                "reference_solution": _augmented_steps(),
                "augmented_problem_text": "Define Future Shock and explain why it occurs.",
                "augmented_target_unknown": None,
            }
        ),
        augment_recall=True,
    )
    assert draft.augmented_target_unknown == "Future Shock"
    assert draft.augmented_target_unknown != draft.augmented_problem_text


def test_old_stored_drafts_still_validate():
    old = _draft().model_dump()
    old.pop("augmented_problem_text", None)
    old.pop("augmented_target_unknown", None)
    restored = ReferenceSolutionDraft.model_validate(old)
    assert restored.augmented_problem_text is None
    assert restored.augmented_target_unknown is None


# --------------------------------------------------------------------------- #
# Step 11 — build_approved_pair round-trip against the REAL tag_mint.ApprovedPair
# --------------------------------------------------------------------------- #


def test_build_approved_pair_validates_against_tag_mint():
    """build_approved_pair(q, draft, search_space_id=…) returns the REAL
    tag_mint.ApprovedPair; its problem dict passes Problem.model_validate;
    solution_source carried from the draft."""
    q = _candidate()
    draft = _draft(solution_source="generated")
    pair = build_approved_pair(q, draft, search_space_id=42)

    assert isinstance(pair, ApprovedPair)
    assert pair.search_space_id == 42
    assert pair.solution_source == "generated"
    # The problem dict is Problem-validatable (round-trip against the REAL schema).
    validated = Problem.model_validate(pair.problem)
    assert validated.target_unknown == q.target_unknown
    assert len(validated.reference_solution) >= 1


def test_build_approved_pair_extracted_vs_generated_source():
    """An extracted draft → pair.solution_source=='extracted'; a generated draft →
    'generated' (the two paths DISCRIMINATE)."""
    q = _candidate()
    pair_ext = build_approved_pair(q, _draft(solution_source="extracted"), search_space_id=1)
    pair_gen = build_approved_pair(q, _draft(solution_source="generated"), search_space_id=1)
    assert pair_ext.solution_source == "extracted"
    assert pair_gen.solution_source == "generated"


def test_build_approved_pair_uses_augmented_text():
    draft = ReferenceSolutionDraft(
        solution_source="generated",
        reference_solution=_augmented_steps(),
        augmented_problem_text="Define Future Shock and explain why it occurs.",
        augmented_target_unknown="why future shock occurs",
    )
    pair = build_approved_pair(_recall_question(), draft, search_space_id=1)
    assert pair.problem["problem_text"] == draft.augmented_problem_text
    assert pair.problem["target_unknown"] == draft.augmented_target_unknown
