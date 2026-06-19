"""WU-5B3a-0 — unit tests for ``LearnerUpdateUnreconstructableError``.

Pure (no infra). Locks the terminal dead-letter error's shape (it mirrors
``RetentionError``) + the closed ``reason`` set the builder's pre-flight and the
WU-5B3a-1 janitor both reference. No LLM, no DB, no Neo4j.
"""

from __future__ import annotations

from apollo.errors import (
    LEARNER_UPDATE_UNRECONSTRUCTABLE_REASONS,
    ApolloError,
    LearnerUpdateUnreconstructableError,
)


def test_error_carries_attempt_id_and_reason() -> None:
    exc = LearnerUpdateUnreconstructableError(attempt_id=42, reason="rubric_missing")
    assert exc.attempt_id == 42
    assert exc.reason == "rubric_missing"
    message = str(exc)
    assert "42" in message
    assert "rubric_missing" in message


def test_is_apollo_error_subclass() -> None:
    # The janitor (WU-5B3a-1) catches by the ApolloError base; the NO-FALLBACK
    # handler family relies on this subclassing.
    assert issubclass(LearnerUpdateUnreconstructableError, ApolloError)


def test_reasons_tuple_is_the_closed_set() -> None:
    # Locks the contract the builder + janitor share. Order + exact membership.
    assert LEARNER_UPDATE_UNRECONSTRUCTABLE_REASONS == (
        "diagnostic_report_missing",
        "rubric_missing",
        "graded_at_missing",
    )


def test_keyword_only_construction() -> None:
    # attempt_id / reason are keyword-only (mirrors RetentionError); positional
    # construction must fail.
    try:
        LearnerUpdateUnreconstructableError(1, "rubric_missing")  # type: ignore[misc]
    except TypeError:
        pass
    else:  # pragma: no cover - the assert below makes the failure explicit
        raise AssertionError("expected keyword-only signature to reject positional args")
