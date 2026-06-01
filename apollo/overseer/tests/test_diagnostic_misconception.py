"""P2.9 — Diagnostic narration line for navigated misconceptions.

The line appears only when at least one misconception fired in the
attempt. Wording is deterministic; it never names the misconception.

Tests cover the post-LLM append step in isolation — the LLM call is
mocked to return a known narrative.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apollo.overseer.diagnostic import (
    _append_misconception_line,
    generate_diagnostic,
)


def _rubric(detected: int, resolved: int):
    return {
        "overall": {"score": 80, "letter": "B+"},
        "misconception_corrected": {
            "score": 100 if detected > 0 else 0,
            "letter": "A+" if detected > 0 else "F",
            "present": detected > 0,
            "detected": detected,
            "resolved": resolved,
        },
    }


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------

def test_no_line_when_zero_detected():
    narrative = "Solid teaching overall."
    assert _append_misconception_line(narrative, _rubric(0, 0)) == narrative


def test_line_appended_with_singular_phrasing():
    out = _append_misconception_line("done", _rubric(1, 1))
    assert "1 suspected misconception;" in out
    assert "you resolved 1 of them" in out


def test_line_appended_with_plural_phrasing():
    out = _append_misconception_line("done", _rubric(3, 2))
    assert "3 suspected misconceptions;" in out
    assert "you resolved 2 of them" in out


def test_line_does_not_name_specific_misconception():
    """Generic phrasing only — no leak of bank codes/descriptions even
    if a future caller wires those into the rubric block."""
    rubric = _rubric(1, 1)
    rubric["misconception_corrected"]["bank_code"] = "no_density"  # extraneous
    out = _append_misconception_line("done", rubric)
    assert "no_density" not in out
    assert "density" not in out  # belt-and-suspenders


def test_handles_missing_misconception_block_gracefully():
    """A pre-P2.8 rubric (no axis at all) must not crash the appender."""
    rubric_legacy = {"overall": {"score": 70, "letter": "B-"}}
    assert _append_misconception_line("done", rubric_legacy) == "done"


# ---------------------------------------------------------------------------
# Through generate_diagnostic with a stubbed LLM
# ---------------------------------------------------------------------------

def _mock_reply(text: str):
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=text))]
    return fake


@patch("apollo.overseer.diagnostic.OpenAI")
def test_generate_diagnostic_appends_line_when_detected(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply(
        "Your teaching covered the procedure well."
    )
    mock_client_cls.return_value = client

    out = generate_diagnostic(
        coverage={"per_step": {}, "procedure_scores": {}, "confidences": {}},
        solver_result={"status": "solved"},
        reference_steps=[],
        problem_text="x",
        rubric=_rubric(2, 1),
    )
    assert out.startswith("Your teaching covered the procedure well.")
    assert "2 suspected misconceptions" in out
    assert "you resolved 1 of them" in out


@patch("apollo.overseer.diagnostic.OpenAI")
def test_generate_diagnostic_omits_line_when_no_detection(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("Looks good.")
    mock_client_cls.return_value = client

    out = generate_diagnostic(
        coverage={"per_step": {}, "procedure_scores": {}, "confidences": {}},
        solver_result={"status": "solved"},
        reference_steps=[],
        problem_text="x",
        rubric=_rubric(0, 0),
    )
    assert out == "Looks good."
    assert "misconception" not in out
