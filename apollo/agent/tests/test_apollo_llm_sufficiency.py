"""P1.6 — apollo_llm system prompt branches on SufficiencyVerdict.

Pure unit tests against the suffix selector and the integration with
`draft_reply` (with the OpenAI client mocked).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from apollo.agent.apollo_llm import (
    APOLLO_SYSTEM_PROMPT,
    _suffix_for_verdict,
    draft_reply,
)
from apollo.solver.sufficiency import SufficiencyVerdict


def _verdict(state, *, hint=None, missing=()):
    return SufficiencyVerdict(
        state=state,
        missing_variables=tuple(missing),
        missing_kg_nodes=(),
        next_premise_hint=hint,
        confidence=1.0,
    )


# ---------------------------------------------------------------------------
# _suffix_for_verdict — pure function
# ---------------------------------------------------------------------------

def test_suffix_none_when_verdict_missing():
    """When no verdict is provided the prompt is byte-identical to today."""
    assert _suffix_for_verdict(None) == ""


def test_suffix_for_sufficient_state_describes_readiness():
    out = _suffix_for_verdict(_verdict("sufficient"))
    assert "SUFFICIENCY SIGNAL" in out
    assert "follow" in out.lower() or "work it out" in out.lower()


def test_suffix_for_almost_state_describes_one_loose_thing():
    out = _suffix_for_verdict(_verdict("almost"))
    assert "SUFFICIENCY SIGNAL" in out
    assert "close" in out.lower()
    assert "one" in out.lower()


def test_suffix_for_insufficient_state_interpolates_hint():
    out = _suffix_for_verdict(
        _verdict("insufficient", hint="equation: Continuity", missing=("v2",)),
    )
    assert "SUFFICIENCY SIGNAL" in out
    assert "equation: Continuity" in out


def test_suffix_for_insufficient_without_hint_uses_fallback():
    """Hint is None — suffix still produces a usable prompt."""
    out = _suffix_for_verdict(_verdict("insufficient"))
    assert "SUFFICIENCY SIGNAL" in out
    # Some sensible fallback text — checked only for non-emptiness.
    assert len(out.strip()) > 30


# ---------------------------------------------------------------------------
# draft_reply — system prompt actually contains the suffix
# ---------------------------------------------------------------------------

def _mock_openai_client(reply_text: str = "ok") -> MagicMock:
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=reply_text))]
    client = MagicMock()
    client.chat.completions.create.return_value = fake
    return client


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_includes_suffix_in_system_prompt(mock_cls):
    client = _mock_openai_client()
    mock_cls.return_value = client

    draft_reply(
        history=[{"role": "user", "content": "hi"}],
        kg_summary="(empty)",
        sufficiency=_verdict("insufficient", hint="equation: Continuity",
                             missing=("v2",)),
    )

    # Pull the messages arg out of the OpenAI call kwargs.
    kwargs = client.chat.completions.create.call_args.kwargs
    msgs = kwargs["messages"]
    sys_prompt = next(
        m["content"] for m in msgs if m["role"] == "system"
    )
    # Both the original prompt AND the suffix must be present.
    assert APOLLO_SYSTEM_PROMPT in sys_prompt
    assert "SUFFICIENCY SIGNAL" in sys_prompt
    assert "equation: Continuity" in sys_prompt


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_no_suffix_when_sufficiency_omitted(mock_cls):
    """No verdict → prompt is the original APOLLO_SYSTEM_PROMPT verbatim."""
    client = _mock_openai_client()
    mock_cls.return_value = client

    draft_reply(
        history=[{"role": "user", "content": "hi"}],
        kg_summary="(empty)",
    )

    kwargs = client.chat.completions.create.call_args.kwargs
    msgs = kwargs["messages"]
    sys_prompt = next(
        m["content"] for m in msgs if m["role"] == "system"
    )
    assert sys_prompt == APOLLO_SYSTEM_PROMPT
    assert "SUFFICIENCY SIGNAL" not in sys_prompt
