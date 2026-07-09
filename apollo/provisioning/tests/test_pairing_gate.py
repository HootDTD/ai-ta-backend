"""WU-3B2e stage 3 — pairing/correctness gate tests (Tier-1 unit, NO DB/network).

Tier-1 ONLY. The two-phase span-grounded judge (``judge_fn``) and the re-ground
retrieval (``retrieve_fn``) are DETERMINISTIC injected stubs — NO real OpenAI /
``cheap_chat`` / live retrieval anywhere. The tests never request ``db_session``.

THE LOAD-BEARING SAFETY PROPERTY — fail-CLOSED: a malformed/non-JSON/exception
judge response at EITHER phase ⇒ ``PairingVerdict(paired=False, faithful=False,
confidence=0.0)`` (a REJECT, never an approval). This is the explicit INVERSION
of ``leakage_judge.py``'s fail-OPEN (``test_leakage_judge.py`` pins fail-open;
``test_validate_pair_fails_closed_on_*`` here pins the opposite). Reverting the
fail-CLOSED default to fail-OPEN MUST red those tests (the pinned mutation).
"""

from __future__ import annotations

import json

# The not-yet-existing public names (RED on import until pairing_gate.py exists).
from apollo.provisioning.pairing_gate import (
    PairingVerdict,
    Rejection,
    rejection_from_verdict,
    validate_pair,
)
from apollo.provisioning.scrape import CandidateQuestion
from apollo.provisioning.solution import GroundingSpan, ReferenceSolutionDraft

# pytest.ini sets asyncio_mode = auto, so async tests need no mark.


# --------------------------------------------------------------------------- #
# Deterministic stubs (NO network, NO DB)
# --------------------------------------------------------------------------- #


def _judge_returning(*phase_payloads):
    """A cheap_chat-shaped sync judge_fn returning successive JSON strings per
    call (Phase A then Phase B). A raw (non-dict) string is returned verbatim for
    the fail-closed cases. Mirrors test_tag_mint.py:58-65 (one-per-call)."""
    rendered = [p if isinstance(p, str) else json.dumps(p) for p in phase_payloads]
    state = {"i": 0}

    def _judge(*_a, **_k) -> str:
        i = state["i"]
        state["i"] = i + 1
        if i < len(rendered):
            return rendered[i]
        return rendered[-1] if rendered else "{}"

    _judge.calls = state  # type: ignore[attr-defined]
    return _judge


def _recording_judge(*phase_payloads):
    """A judge_fn that records every (args, kwargs) it was handed (for the
    span-grounding assertion)."""
    rendered = [p if isinstance(p, str) else json.dumps(p) for p in phase_payloads]
    calls: list[dict] = []

    def _judge(*a, **k) -> str:
        i = len(calls)
        calls.append({"args": a, "kwargs": k})
        if i < len(rendered):
            return rendered[i]
        return rendered[-1] if rendered else "{}"

    _judge.recorded = calls  # type: ignore[attr-defined]
    return _judge


def _retrieve_returning(spans):
    async def _retrieve(*_a, **_k):
        return list(spans)

    return _retrieve


def _candidate() -> CandidateQuestion:
    return CandidateQuestion(
        problem_text="A fluid speeds up in a pipe; find the downstream pressure P2.",
        given_values={"P1": 200000.0, "v1": 2.0, "rho": 1000.0, "v2": 4.0},
        target_unknown="P2",
        difficulty="intro",
        document_id=7,
        page=3,
        chunk_content_hash="abc123hash",
        concept_slug="bernoulli_principle",
    )


def _reference_solution() -> list[dict]:
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


def _draft(*, grounding=None) -> ReferenceSolutionDraft:
    return ReferenceSolutionDraft(
        solution_source="generated",
        reference_solution=_reference_solution(),
        grounding=grounding
        if grounding is not None
        else (GroundingSpan(text="the energy balance in a pipe passage"),),
        provenance={
            "document_id": 7,
            "page": 3,
            "chunk_content_hash": "abc123hash",
            "retrieval_hits": 1,
        },
    )


# --------------------------------------------------------------------------- #
# Step 5 — PairingVerdict + Rejection + rejection_from_verdict (pure)
# --------------------------------------------------------------------------- #


def test_pairing_verdict_shape():
    """PairingVerdict round-trips; confidence clamped to [0,1]; failed_claims is a
    tuple."""
    v = PairingVerdict(paired=True, faithful=True, failed_claims=(), confidence=0.9)
    assert v.failed_claims == ()
    again = PairingVerdict.model_validate(v.model_dump())
    assert again.paired is True
    # clamp on construction
    high = PairingVerdict(paired=True, faithful=True, failed_claims=(), confidence=5.7)
    assert high.confidence == 1.0
    low = PairingVerdict(paired=False, faithful=False, failed_claims=("x",), confidence=-0.4)
    assert low.confidence == 0.0


def test_pairing_verdict_coerces_list_failed_claims():
    """failed_claims accepts a LIST (coerced to a tuple) — the field-validator
    list branch on PairingVerdict."""
    v = PairingVerdict(paired=True, faithful=False, failed_claims=["a", "b"], confidence=0.5)
    assert v.failed_claims == ("a", "b")


def test_rejection_coerces_list_failed_claims():
    """Rejection.failed_claims accepts a LIST (coerced to a tuple) — the
    field-validator list branch on Rejection."""
    rej = Rejection(
        reason="unfaithful_claims",
        diagnostic="d",
        failed_claims=["claim one", "claim two"],
    )
    assert rej.failed_claims == ("claim one", "claim two")


def test_rejection_from_verdict_none_on_approved():
    """rejection_from_verdict(paired&faithful verdict) is None."""
    approved = PairingVerdict(paired=True, faithful=True, failed_claims=(), confidence=0.8)
    assert rejection_from_verdict(approved) is None


def test_rejection_from_verdict_typed_on_fail():
    """A not-paired verdict → Rejection(reason='not_paired'); an unfaithful →
    reason='unfaithful_claims' carrying failed_claims; an unparseable marker →
    reason='unparseable_judge'."""
    not_paired = PairingVerdict(paired=False, faithful=False, failed_claims=(), confidence=0.3)
    rej = rejection_from_verdict(not_paired)
    assert isinstance(rej, Rejection)
    assert rej.stage == "pairing_gate"
    assert rej.reason == "not_paired"

    unfaithful = PairingVerdict(
        paired=True, faithful=False, failed_claims=("claim X",), confidence=0.5
    )
    rej2 = rejection_from_verdict(unfaithful)
    assert rej2 is not None
    assert rej2.reason == "unfaithful_claims"
    assert rej2.failed_claims == ("claim X",)

    unparseable = PairingVerdict(
        paired=False,
        faithful=False,
        failed_claims=("<unparseable judge response>",),
        confidence=0.0,
    )
    rej3 = rejection_from_verdict(unparseable)
    assert rej3 is not None
    assert rej3.reason == "unparseable_judge"


# --------------------------------------------------------------------------- #
# Step 6-8 — validate_pair branches
# --------------------------------------------------------------------------- #


async def test_validate_pair_approves_good_pair():
    """Phase A paired + Phase B all-entailed → paired=True, faithful=True,
    failed_claims=()."""
    judge = _judge_returning(
        {"paired": True, "confidence": 0.9},
        {"claims": [{"claim": "applies bernoulli", "entailed": True}]},
    )
    verdict = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    assert verdict.paired is True
    assert verdict.faithful is True
    assert verdict.failed_claims == ()


async def test_validate_pair_rejects_mispaired_solution():
    """Phase A says the solution answers a DIFFERENT question → paired=False;
    Phase B is short-circuited (judge called ONCE)."""
    judge = _judge_returning({"paired": False, "confidence": 0.8})
    verdict = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    assert verdict.paired is False
    assert verdict.faithful is False
    # Phase B short-circuited.
    assert judge.calls["i"] == 1  # type: ignore[attr-defined]


async def test_validate_pair_phase_b_not_called_when_not_paired():
    """The Phase-A short-circuit: judge invoked exactly once when not paired."""
    judge = _judge_returning({"paired": False})
    await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    assert judge.calls["i"] == 1  # type: ignore[attr-defined]


async def test_validate_pair_rejects_unfaithful_claim():
    """Phase A paired, Phase B marks one claim NOT entailed → faithful=False, the
    claim in failed_claims."""
    judge = _judge_returning(
        {"paired": True, "confidence": 0.9},
        {
            "claims": [
                {"claim": "good claim", "entailed": True},
                {"claim": "fabricated claim", "entailed": False},
            ]
        },
    )
    verdict = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    assert verdict.paired is True
    assert verdict.faithful is False
    assert "fabricated claim" in verdict.failed_claims


async def test_validate_pair_fails_closed_on_zero_decomposed_claims():
    """Phase A paired, but Phase B decomposes ZERO claims (``{"claims": []}``) →
    the judge produced NO positive evidence of faithfulness, so the gate FAILS
    CLOSED (faithful=False) rather than vacuously approving. The verdict maps to a
    distinct ``no_claims_decomposed`` rejection."""
    judge = _judge_returning(
        {"paired": True, "confidence": 0.9},
        {"claims": []},
    )
    verdict = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    assert verdict.paired is True
    assert verdict.faithful is False
    assert verdict.approved is False
    rejection = rejection_from_verdict(verdict)
    assert rejection is not None
    assert rejection.reason == "no_claims_decomposed"


# --------------------------------------------------------------------------- #
# Step 9 — THE LOAD-BEARING FAIL-CLOSED tests
# --------------------------------------------------------------------------- #


async def test_validate_pair_fails_closed_on_unparseable_judge():
    """judge_fn returns non-JSON → paired=False, faithful=False, confidence=0.0,
    failed_claims carries the unparseable marker — NEVER an approval. THE
    load-bearing safety test (inverts leakage_judge fail-open)."""
    judge = _judge_returning("garbage {")
    verdict = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    assert verdict.paired is False
    assert verdict.faithful is False
    assert verdict.confidence == 0.0
    assert verdict.failed_claims == ("<unparseable judge response>",)


async def test_validate_pair_fails_closed_on_non_object_judge():
    """judge_fn returns valid JSON that is NOT an object (a JSON array) → the
    non-dict fail-closed branch → paired=False (a REJECT, never an approval)."""
    judge = _judge_returning("[1, 2, 3]")
    verdict = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    assert verdict.paired is False
    assert verdict.faithful is False
    assert verdict.failed_claims == ("<unparseable judge response>",)


async def test_validate_pair_fails_closed_on_judge_exception():
    """judge_fn raises → same fail-closed verdict (not an approval)."""

    def _boom(*_a, **_k) -> str:
        raise RuntimeError("API down")

    verdict = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=_boom,
    )
    assert verdict.paired is False
    assert verdict.faithful is False
    assert verdict.confidence == 0.0


async def test_validate_pair_fails_closed_on_phase_b_unparseable():
    """Phase A paired, Phase B unparseable → faithful=False (the fail-closed
    default applies per-phase, not just Phase A)."""
    judge = _judge_returning({"paired": True, "confidence": 0.9}, "garbage")
    verdict = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    assert verdict.paired is True
    assert verdict.faithful is False
    assert verdict.failed_claims == ("<unparseable judge response>",)


# --------------------------------------------------------------------------- #
# Step 10 — span-grounding
# --------------------------------------------------------------------------- #


async def test_validate_pair_judge_sees_same_grounding():
    """The grounding text handed to judge_fn equals draft.grounding text
    (span-grounded; judge uses the generator's context)."""
    span = GroundingSpan(text="THE-UNIQUE-GROUNDING-MARKER passage text")
    judge = _recording_judge(
        {"paired": True, "confidence": 0.9},
        {"claims": [{"claim": "ok", "entailed": True}]},
    )
    await validate_pair(
        _candidate(),
        _draft(grounding=(span,)),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    blob = json.dumps(judge.recorded)  # type: ignore[attr-defined]
    assert "THE-UNIQUE-GROUNDING-MARKER" in blob


async def test_validate_pair_reground_uses_retrieve_fn_union():
    """When the draft carries NO grounding, validate_pair re-grounds via
    retrieve_fn(question) and the judge sees the re-retrieved span text."""
    reground = GroundingSpan(text="REGROUND-MARKER from retrieve_fn")
    judge = _recording_judge(
        {"paired": True, "confidence": 0.9},
        {"claims": [{"claim": "ok", "entailed": True}]},
    )
    draft = _draft(grounding=())
    verdict = await validate_pair(
        _candidate(),
        draft,
        retrieve_fn=_retrieve_returning([reground]),
        judge_fn=judge,
    )
    assert verdict.paired is True
    blob = json.dumps(judge.recorded)  # type: ignore[attr-defined]
    assert "REGROUND-MARKER" in blob


# --------------------------------------------------------------------------- #
# Step 13 — coverage closers
# --------------------------------------------------------------------------- #


async def test_validate_pair_confidence_clamped():
    """A judge confidence of 5.7 → clamps to 1.0; a non-numeric → 0.0 (mirrors
    leakage_judge)."""
    judge = _judge_returning(
        {"paired": True, "confidence": 5.7},
        {"claims": [{"claim": "ok", "entailed": True}]},
    )
    verdict = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    assert verdict.confidence == 1.0

    judge2 = _judge_returning(
        {"paired": True, "confidence": "not-a-number"},
        {"claims": [{"claim": "ok", "entailed": True}]},
    )
    verdict2 = await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge2,
    )
    assert verdict2.confidence == 0.0


# --------------------------------------------------------------------------- #
# Stage-3 fix — schema-explicit system prompt + json_schema response_format
# --------------------------------------------------------------------------- #


async def test_validate_pair_sends_system_prompt_and_json_schema():
    """Both judge phases carry a SYSTEM prompt + a ``json_schema`` response_format
    (the Stage-3 fix). Without them the LIVE call 400s ("'messages' must contain
    the word 'json'") and every pair fail-closes to a REJECT. DISCRIMINATING:
    dropping the system message or reverting to ``json_object`` REDs this."""
    from apollo.provisioning.pairing_gate import (
        _PAIRING_PHASE_A_SYSTEM_PROMPT,
        _PAIRING_PHASE_B_SYSTEM_PROMPT,
    )

    judge = _recording_judge(
        {"paired": True, "confidence": 0.9},
        {"claims": [{"claim": "ok", "entailed": True}]},
    )
    await validate_pair(
        _candidate(),
        _draft(),
        retrieve_fn=_retrieve_returning([]),
        judge_fn=judge,
    )
    calls = judge.recorded  # type: ignore[attr-defined]
    assert len(calls) == 2  # Phase A then Phase B
    phase_a = calls[0]["kwargs"]
    assert phase_a["messages"][0]["role"] == "system"
    assert phase_a["messages"][0]["content"] == _PAIRING_PHASE_A_SYSTEM_PROMPT
    assert phase_a["response_format"]["type"] == "json_schema"
    assert phase_a["response_format"]["json_schema"]["name"] == "pairing_phase_a"
    phase_b = calls[1]["kwargs"]
    assert phase_b["messages"][0]["role"] == "system"
    assert phase_b["messages"][0]["content"] == _PAIRING_PHASE_B_SYSTEM_PROMPT
    assert phase_b["response_format"]["json_schema"]["name"] == "pairing_phase_b"


def test_pairing_system_prompts_declare_keys_and_mention_json():
    """The phase prompts name the EXACT keys ``validate_pair`` reads AND contain the
    word 'json' (OpenAI requires it for a JSON ``response_format``). DISCRIMINATING:
    a vague prompt that omits a read key or 'json' REDs."""
    from apollo.provisioning.pairing_gate import (
        _PAIRING_PHASE_A_SYSTEM_PROMPT,
        _PAIRING_PHASE_B_SYSTEM_PROMPT,
    )

    for key in ("paired", "confidence"):
        assert key in _PAIRING_PHASE_A_SYSTEM_PROMPT
    for key in ("claims", "claim", "entailed"):
        assert key in _PAIRING_PHASE_B_SYSTEM_PROMPT
    assert "json" in _PAIRING_PHASE_A_SYSTEM_PROMPT.lower()
    assert "json" in _PAIRING_PHASE_B_SYSTEM_PROMPT.lower()


# --------------------------------------------------------------------------- #
# Step 12 — re-export surface
# --------------------------------------------------------------------------- #


def test_solution_pairing_public_api_reexport():
    """The package-level paths resolve to the SAME objects as the modules.
    DISCRIMINATING: dropping a re-export REDs this (mirrors
    test_tag_mint_public_api_reexport)."""
    from apollo.provisioning import (
        GroundingSpan as ReGroundingSpan,
    )
    from apollo.provisioning import (
        PairingVerdict as RePairingVerdict,
    )
    from apollo.provisioning import (
        ReferenceSolutionDraft as ReDraft,
    )
    from apollo.provisioning import (
        Rejection as ReRejection,
    )
    from apollo.provisioning import (
        SolutionDraftError as ReSolutionDraftError,
    )
    from apollo.provisioning import (
        build_approved_pair as re_build_approved_pair,
    )
    from apollo.provisioning import (
        find_or_generate as re_find_or_generate,
    )
    from apollo.provisioning import pairing_gate as pg_mod
    from apollo.provisioning import (
        rejection_from_verdict as re_rejection_from_verdict,
    )
    from apollo.provisioning import solution as sol_mod
    from apollo.provisioning import (
        validate_pair as re_validate_pair,
    )

    assert ReGroundingSpan is sol_mod.GroundingSpan
    assert ReDraft is sol_mod.ReferenceSolutionDraft
    assert ReSolutionDraftError is sol_mod.SolutionDraftError
    assert re_find_or_generate is sol_mod.find_or_generate
    assert re_build_approved_pair is sol_mod.build_approved_pair
    assert RePairingVerdict is pg_mod.PairingVerdict
    assert ReRejection is pg_mod.Rejection
    assert re_validate_pair is pg_mod.validate_pair
    assert re_rejection_from_verdict is pg_mod.rejection_from_verdict
