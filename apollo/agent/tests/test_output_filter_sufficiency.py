"""P1.7 — output filter logs (warns) when draft ignores sufficiency hint.

Filter is warn-only on alignment — it never blocks a draft that ignores
the hint. The test verifies the log line fires (and does not fire) at
the expected times.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from apollo.agent.leakage_judge import JudgeVerdict, LeakageJudge
from apollo.agent.output_filter import validate_or_raise
from apollo.solver.sufficiency import SufficiencyVerdict
from apollo.subjects import load_concept


@pytest.fixture(scope="module")
def concept():
    return load_concept("fluid_mechanics", "bernoulli_principle")


def _clean_judge():
    """LeakageJudge that approves all drafts. Lets us exercise the
    sufficiency-alignment path without engaging the leakage stage."""
    fn = MagicMock(spec=LeakageJudge)
    fn.return_value = JudgeVerdict(
        leaks=False, confidence=1.0, offending_phrase=None, reason="clean",
    )
    return fn


def _verdict(state, *, hint=None, missing=()):
    return SufficiencyVerdict(
        state=state,
        missing_variables=tuple(missing),
        missing_kg_nodes=(),
        next_premise_hint=hint,
        confidence=1.0,
    )


def _has_signal_ignored_log(records):
    return any(
        getattr(r, "event", None) == "sufficiency_signal_ignored"
        for r in records
    )


def test_no_log_when_sufficiency_omitted(concept, caplog):
    caplog.set_level(logging.INFO, logger="apollo.agent.output_filter")
    validate_or_raise(
        "okay, sounds fine",
        concept=concept,
        history=[],
        kg_summary="(empty)",
        judge=_clean_judge(),
        sufficiency=None,
    )
    assert not _has_signal_ignored_log(caplog.records)


def test_no_log_when_state_is_sufficient(concept, caplog):
    caplog.set_level(logging.INFO, logger="apollo.agent.output_filter")
    validate_or_raise(
        "okay sounds fine",
        concept=concept,
        history=[],
        kg_summary="(empty)",
        judge=_clean_judge(),
        sufficiency=_verdict("sufficient"),
    )
    assert not _has_signal_ignored_log(caplog.records)


def test_warns_when_insufficient_and_draft_ignores_hint(concept, caplog):
    caplog.set_level(logging.INFO, logger="apollo.agent.output_filter")
    validate_or_raise(
        "great, that all sounds clear",  # mentions nothing of v2 / Continuity
        concept=concept,
        history=[],
        kg_summary="(empty)",
        judge=_clean_judge(),
        sufficiency=_verdict(
            "insufficient", hint="equation: Continuity", missing=("v2",),
        ),
    )
    assert _has_signal_ignored_log(caplog.records)


def test_does_not_warn_when_draft_references_missing_var(concept, caplog):
    caplog.set_level(logging.INFO, logger="apollo.agent.output_filter")
    validate_or_raise(
        "I have no idea how to find v2 from what you've told me",
        concept=concept,
        history=[],
        kg_summary="(empty)",
        judge=_clean_judge(),
        sufficiency=_verdict(
            "insufficient", hint="equation: Continuity", missing=("v2",),
        ),
    )
    assert not _has_signal_ignored_log(caplog.records)


def test_does_not_warn_when_draft_references_hint_term(concept, caplog):
    """Hint = "horizontal pipe geometry"; draft references "horizontal" —
    the alphabetic-token check on the hint matches and the warning is
    suppressed. Picked a non-named-law hint so the leakage pre-filter
    stays out of the way."""
    caplog.set_level(logging.INFO, logger="apollo.agent.output_filter")
    validate_or_raise(
        "ah, you mean the horizontal piece — okay",
        concept=concept,
        history=[],
        kg_summary="(empty)",
        judge=_clean_judge(),
        sufficiency=_verdict(
            "insufficient", hint="horizontal pipe geometry", missing=("v2",),
        ),
    )
    assert not _has_signal_ignored_log(caplog.records)


def test_warn_only_does_not_raise(concept):
    """Critical contract: warning never blocks the draft."""
    out = validate_or_raise(
        "great, that all sounds clear",
        concept=concept,
        history=[],
        kg_summary="(empty)",
        judge=_clean_judge(),
        sufficiency=_verdict(
            "insufficient", hint="equation: Continuity", missing=("v2",),
        ),
    )
    assert out == "great, that all sounds clear"
