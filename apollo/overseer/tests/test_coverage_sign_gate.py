"""T10 (D4 fix): SymPy sign pre-gate on equation coverage matching.

Contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 8, task T10, amended by A1-A8 (see also spec §5.2 sympy_veto).

Behavior change lives entirely inside ``_batch_binary_match`` for
``entry_type == "equation"``, flag-gated by
``apollo.overseer.misconception_detector.config.detector_enabled()``:

  * flag ON: for every (student, reference) equation pair the LLM marks
    ``covered=True``, re-check with ``apollo.resolution.tiers._symbolic_equiv``
    (sign-exact). If the student equation is NOT sign-exact equivalent to the
    reference AND IS sign-exact equivalent to the reference's negation, force
    ``covered=False`` (a sign-reversed mutant is never "equivalent" even if the
    LLM says so). A genuine sign-preserving algebraic rearrangement remains
    sign-exact equivalent to the reference itself, so it is untouched.
  * flag OFF: prompt string and verdict are byte-identical to pre-change
    behavior (no SymPy pre-gate is even attempted).
  * The stale prompt clause "Sign flips and algebraic rearrangements are
    equivalent." must no longer appear in the prompt text (replaced with
    sign-NOT-equivalent wording).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apollo.ontology import build_node
from apollo.overseer.coverage import (
    _BATCH_BINARY_PROMPT,
    _batch_binary_match,
    _sign_gate_equation_verdicts,
    _sign_reversed_zero_form,
)
from apollo.resolution.tiers import _extended_locals

_FLAG = "APOLLO_MISCONCEPTION_DETECTOR"


@pytest.fixture(autouse=True)
def _clean_flag_env():
    """Never leak the flag across tests regardless of pass/fail."""
    prior = os.environ.pop(_FLAG, None)
    yield
    if prior is None:
        os.environ.pop(_FLAG, None)
    else:
        os.environ[_FLAG] = prior


def _eq_node(node_id: str, symbolic: str, label: str = "", *, attempt_id: int = 1):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": symbolic, "label": label},
    )


def _mock_openai_always(payload: str):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=payload))]
    )
    return client


# ---------------------------------------------------------------------------
# (a) flag ON: sign-reversed pair forced to missing regardless of LLM verdict.
# ---------------------------------------------------------------------------


@patch("apollo.overseer.coverage.OpenAI")
def test_sign_reversed_pair_forced_uncovered_when_flag_on(mock_client_cls):
    os.environ[_FLAG] = "true"
    # Reference: S - I (savings-investment identity, S = I).
    # Student:   I - S — the exact negation of the reference's zero-form:
    # a genuine sign-reversal mutant, not a rearrangement.
    ref = _eq_node("ref_si", "S - I", "Savings-investment identity")
    student = _eq_node("stu_si", "I - S")

    # LLM (mocked) WRONGLY says covered=True with high confidence — the
    # pre-gate must override this.
    payload = json.dumps(
        {
            "matches": [
                {"ref_id": "ref_si", "covered": True, "confidence": 0.95},
            ]
        }
    )
    mock_client_cls.return_value = _mock_openai_always(payload)

    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=[ref],
        student_nodes=[student],
    )
    assert result["ref_si"]["covered"] is False


# ---------------------------------------------------------------------------
# (b) flag ON: genuine sign-preserving rearrangement still covers.
# ---------------------------------------------------------------------------


@patch("apollo.overseer.coverage.OpenAI")
def test_equals_bearing_sign_reversed_pair_forced_uncovered_when_flag_on(mock_client_cls):
    """REAL A2 regression (net_exports_sign family): reference equations carry an
    '=' (``NX = X - M``). A sign flip of the RHS (``NX = M - X``) is a genuine
    sign-reversal mutant and must be forced to covered=False even though the LLM
    says covered=True. The pre-fix gate wrapped the raw '='-bearing string in
    ``-1*(...)`` which the '='-splitting parser mangled, so this case slipped
    through as covered=True. The no-'=' fixtures above cannot catch this."""
    os.environ[_FLAG] = "true"
    ref = _eq_node("ref_nx", "NX = X - M", "Net exports identity")
    student = _eq_node("stu_nx", "NX = M - X")

    payload = json.dumps(
        {
            "matches": [
                {"ref_id": "ref_nx", "covered": True, "confidence": 0.95},
            ]
        }
    )
    mock_client_cls.return_value = _mock_openai_always(payload)

    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=[ref],
        student_nodes=[student],
    )
    assert result["ref_nx"]["covered"] is False


@patch("apollo.overseer.coverage.OpenAI")
def test_equals_bearing_side_swap_still_covered_when_flag_on(mock_client_cls):
    """Swapping the two SIDES of an '='-bearing equation (``NX = X - M`` ->
    ``X - M = NX``) is the SAME relationship, not a sign reversal, so it must
    stay covered=True — the gate must not over-correct a legitimate
    rearrangement of an '='-bearing equation."""
    os.environ[_FLAG] = "true"
    ref = _eq_node("ref_nx", "NX = X - M", "Net exports identity")
    student = _eq_node("stu_nx", "X - M = NX")

    payload = json.dumps(
        {
            "matches": [
                {"ref_id": "ref_nx", "covered": True, "confidence": 0.9},
            ]
        }
    )
    mock_client_cls.return_value = _mock_openai_always(payload)

    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=[ref],
        student_nodes=[student],
    )
    assert result["ref_nx"]["covered"] is True


@patch("apollo.overseer.coverage.OpenAI")
def test_sign_preserving_rearrangement_still_covered_when_flag_on(mock_client_cls):
    os.environ[_FLAG] = "true"
    # Reference: S - I. Student rearranges (still sign-exact equivalent,
    # same physical relationship, just an added zero term): S - I + 0.
    ref = _eq_node("ref_si", "S - I", "Savings-investment identity")
    student = _eq_node("stu_si", "S - I + 0")

    payload = json.dumps(
        {
            "matches": [
                {"ref_id": "ref_si", "covered": True, "confidence": 0.9},
            ]
        }
    )
    mock_client_cls.return_value = _mock_openai_always(payload)

    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=[ref],
        student_nodes=[student],
    )
    assert result["ref_si"]["covered"] is True


@patch("apollo.overseer.coverage.OpenAI")
def test_llm_says_not_covered_stays_not_covered_when_flag_on(mock_client_cls):
    """The pre-gate only ever downgrades covered=True -> False; it never
    upgrades an LLM covered=False verdict (no over-correction, A amend)."""
    os.environ[_FLAG] = "true"
    ref = _eq_node("ref_si", "S - I", "Savings-investment identity")
    student = _eq_node("stu_si", "S - I + 0")

    payload = json.dumps(
        {
            "matches": [
                {"ref_id": "ref_si", "covered": False, "confidence": 0.7},
            ]
        }
    )
    mock_client_cls.return_value = _mock_openai_always(payload)

    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=[ref],
        student_nodes=[student],
    )
    assert result["ref_si"]["covered"] is False


@patch("apollo.overseer.coverage.OpenAI")
def test_non_equation_entry_type_untouched_by_gate(mock_client_cls):
    """The pre-gate is equation-only; condition/simplification batches are
    never re-checked even with the flag on."""
    os.environ[_FLAG] = "true"
    ref = build_node(
        node_type="condition",
        node_id="ref_c1",
        attempt_id=1,
        source="parser",
        content={"applies_when": "steady flow", "label": ""},
    )
    student = build_node(
        node_type="condition",
        node_id="stu_c1",
        attempt_id=1,
        source="parser",
        content={"applies_when": "the flow is steady", "label": ""},
    )
    payload = json.dumps(
        {
            "matches": [
                {"ref_id": "ref_c1", "covered": True, "confidence": 0.9},
            ]
        }
    )
    mock_client_cls.return_value = _mock_openai_always(payload)

    result = _batch_binary_match(
        entry_type="condition",
        reference_nodes=[ref],
        student_nodes=[student],
    )
    assert result["ref_c1"]["covered"] is True


# ---------------------------------------------------------------------------
# (c) flag OFF: verdict byte-identical to pre-change behavior.
# ---------------------------------------------------------------------------


@patch("apollo.overseer.coverage.OpenAI")
def test_flag_off_sign_reversed_pair_verdict_unchanged(mock_client_cls):
    """Flag OFF => no SymPy pre-gate is even attempted; the LLM's (wrong)
    covered=True verdict passes through untouched, exactly as it did before
    this task's change existed."""
    assert os.environ.get(_FLAG) is None  # confirm truly unset
    ref = _eq_node("ref_si", "S - I", "Savings-investment identity")
    student = _eq_node("stu_si", "I - S")

    payload = json.dumps(
        {
            "matches": [
                {"ref_id": "ref_si", "covered": True, "confidence": 0.95},
            ]
        }
    )
    mock_client_cls.return_value = _mock_openai_always(payload)

    result = _batch_binary_match(
        entry_type="equation",
        reference_nodes=[ref],
        student_nodes=[student],
    )
    assert result["ref_si"]["covered"] is True
    assert result["ref_si"]["confidence"] == 0.95


@patch("apollo.overseer.coverage.OpenAI")
def test_flag_off_prompt_payload_byte_identical(mock_client_cls):
    """Flag OFF => the exact system/user payload sent to the LLM is
    unchanged from pre-task behavior (no extra fields, no different
    prompt)."""
    ref = _eq_node("ref_si", "S - I", "Savings-investment identity")
    student = _eq_node("stu_si", "S - I + 0")
    payload = json.dumps(
        {
            "matches": [
                {"ref_id": "ref_si", "covered": True, "confidence": 0.9},
            ]
        }
    )
    client = _mock_openai_always(payload)
    mock_client_cls.return_value = client

    _batch_binary_match(
        entry_type="equation",
        reference_nodes=[ref],
        student_nodes=[student],
    )

    _, kwargs = client.chat.completions.create.call_args
    system_msg = kwargs["messages"][0]["content"]
    assert system_msg == _BATCH_BINARY_PROMPT


# ---------------------------------------------------------------------------
# (d) prompt text: stale equivalence clause removed either way.
# ---------------------------------------------------------------------------


def test_prompt_no_longer_claims_sign_flips_equivalent():
    assert "Sign flips and algebraic rearrangements are equivalent." not in _BATCH_BINARY_PROMPT


def test_prompt_states_sign_not_equivalent():
    lowered = _BATCH_BINARY_PROMPT.lower()
    assert "sign" in lowered
    assert "not equivalent" in lowered or "not the same" in lowered or "different" in lowered


# ---------------------------------------------------------------------------
# (e) `_sign_reversed_zero_form` — parse-failure branch (lines 251-252).
# ---------------------------------------------------------------------------


def test_sign_reversed_zero_form_returns_none_on_equals_bearing_parse_failure():
    """An '='-bearing symbolic string whose LHS/RHS split cannot be parsed by
    SymPy must return None (a non-parse is a non-match, never a crash) rather
    than propagating the parse exception."""
    local_dict = _extended_locals("x")
    result = _sign_reversed_zero_form("x = )(( malformed", local_dict)
    assert result is None


def test_sign_reversed_zero_form_handles_bare_expression_without_equals():
    """Sanity check for the non-'=' branch (already covered elsewhere) so the
    two branches of the function are both exercised in this test class."""
    local_dict = _extended_locals("x")
    result = _sign_reversed_zero_form("x", local_dict)
    assert result is not None


# ---------------------------------------------------------------------------
# (f) `_sign_gate_equation_verdicts` — direct branch tests (lines 286, 294,
#     297, 318). These call the module-private gate function directly since
#     `_batch_binary_match`'s LLM-mocking path cannot construct a verdicts
#     dict with a ref_id absent from `reference_nodes`, nor a reference node
#     with empty surface text, without contorting the mock.
# ---------------------------------------------------------------------------


def _fake_equation_node(node_id: str, symbolic: str):
    """A minimal duck-typed stand-in for an EquationNode whose ``symbolic``
    field is empty. The real pydantic ``EquationContent`` model enforces
    ``min_length=1`` on ``symbolic``, so an empty-string surface text can only
    be exercised via a fake object honoring the same attribute shape that
    ``student_surface_text``/``_sign_gate_equation_verdicts`` duck-type
    against (``node_id``, ``node_type``, ``content.symbolic``) — no isinstance
    checks are involved on this path."""
    return SimpleNamespace(
        node_id=node_id,
        node_type="equation",
        content=SimpleNamespace(symbolic=symbolic, label=""),
    )


def test_sign_gate_returns_verdicts_unchanged_when_no_student_surface_text():
    """Line 286: if every student equation node's surface text is empty/falsy
    (empty `symbolic`), there is nothing to sign-check against, so the
    original verdicts dict is returned as-is."""
    ref = _eq_node("ref_si", "S - I", "Savings-investment identity")
    # Empty symbolic -> student_surface_text(node) is falsy for this node.
    student = _fake_equation_node("stu_si", "")
    verdicts = {"ref_si": {"covered": True, "confidence": 0.9}}

    result = _sign_gate_equation_verdicts(
        verdicts=verdicts,
        reference_nodes=[ref],
        student_nodes=[student],
    )

    assert result == verdicts


def test_sign_gate_skips_verdict_whose_ref_id_has_no_matching_reference_node():
    """Line 294: a verdict entry whose ref_id is not present in
    reference_nodes must be skipped (continue), not raise a KeyError, and
    must be left untouched in the returned mapping."""
    student = _eq_node("stu_si", "S - I + 0")
    # No reference node with node_id "unknown_ref" is supplied.
    ref = _eq_node("ref_si", "S - I", "Savings-investment identity")
    verdicts = {
        "unknown_ref": {"covered": True, "confidence": 0.8},
    }

    result = _sign_gate_equation_verdicts(
        verdicts=verdicts,
        reference_nodes=[ref],
        student_nodes=[student],
    )

    assert result["unknown_ref"] == {"covered": True, "confidence": 0.8}


def test_sign_gate_skips_reference_node_with_empty_surface_text():
    """Line 297: a resolved reference node whose surface text
    (student_surface_text) is empty/falsy must be skipped (continue) rather
    than fed into the SymPy comparison."""
    student = _eq_node("stu_si", "S - I + 0")
    ref = _fake_equation_node("ref_si", "")  # empty symbolic
    verdicts = {"ref_si": {"covered": True, "confidence": 0.9}}

    result = _sign_gate_equation_verdicts(
        verdicts=verdicts,
        reference_nodes=[ref],
        student_nodes=[student],
    )

    # Left untouched — the gate can't evaluate an empty reference symbolic.
    assert result["ref_si"] == {"covered": True, "confidence": 0.9}


def test_sign_gate_skips_student_with_unparseable_zero_form_in_reversed_loop():
    """Line 318: inside the per-student sign-reversed comparison loop, a
    student equation whose zero-form parses to None (malformed symbolic) must
    be skipped (continue) rather than crash on `simplify(student_zf - ...)`.
    A second, parseable-but-unrelated student equation is also supplied so the
    loop actually reaches (and continues past) the malformed one without
    finding a sign-reversed match, proving the branch executes without
    short-circuiting the whole gate."""
    ref = _eq_node("ref_si", "S - I", "Savings-investment identity")
    malformed_student = _eq_node("stu_bad", "S -- === I ((", attempt_id=1)
    unrelated_student = _eq_node("stu_ok", "x + y", attempt_id=1)
    verdicts = {"ref_si": {"covered": True, "confidence": 0.9}}

    result = _sign_gate_equation_verdicts(
        verdicts=verdicts,
        reference_nodes=[ref],
        student_nodes=[malformed_student, unrelated_student],
    )

    # No sign-exact match and no sign-reversed match found (malformed student
    # is skipped, unrelated student doesn't reverse-match) -> stays covered.
    assert result["ref_si"] == {"covered": True, "confidence": 0.9}
