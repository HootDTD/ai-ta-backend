"""WU-3C2 Step 1 — pure-unit tests for the two named resolver errors.

No Docker, no network. These pin that both classes subclass ``ApolloError``,
carry their structured attributes, and render those attributes in ``str()``
for the audit log.
"""

from __future__ import annotations

from apollo.errors import (
    ApolloError,
    ResolutionInvalidOutputError,
    ResolutionUnavailableError,
)


def test_resolution_unavailable_error_is_apollo_error_and_carries_stage():
    err = ResolutionUnavailableError(stage="llm_adjudication", last_error="openai timeout")
    assert isinstance(err, ApolloError)
    assert err.stage == "llm_adjudication"
    assert err.last_error == "openai timeout"
    rendered = str(err)
    assert "llm_adjudication" in rendered
    assert "openai timeout" in rendered


def test_resolution_invalid_output_error_carries_returned_and_allowed_keys():
    err = ResolutionInvalidOutputError(
        returned_key="eq.hallucinated",
        allowed_keys=("eq.bernoulli", "cond.incompressibility"),
    )
    assert isinstance(err, ApolloError)
    assert err.returned_key == "eq.hallucinated"
    assert err.allowed_keys == ("eq.bernoulli", "cond.incompressibility")
    rendered = str(err)
    assert "eq.hallucinated" in rendered
