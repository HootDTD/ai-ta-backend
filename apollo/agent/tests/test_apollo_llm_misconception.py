"""P2.5 — Persona shift in apollo_llm.py.

Verifies:
- Probe-band signal injects the authored probe verbatim.
- Socratic-band signal injects authored rt_steps.
- Description and bank_id are NEVER serialized into the prompt.
- Flag-off path is byte-identical to a None misconception (pre-P2 prompt).
- Soft fall-through: a fired signal with no authored payload returns
  unchanged prompt (defensive — prevents auto-failing if bank rows are
  ever inserted with empty probe/rt_steps).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apollo.agent.apollo_llm import (
    APOLLO_SYSTEM_PROMPT,
    _suffix_for_misconception,
    draft_reply,
)
from apollo.overseer.misconception import MisconceptionSignal


_BANK_DESCRIPTION = "treats fluid as having no density"
_BANK_ID = "42"
_PROBE_QUESTION = "Hmm, is density part of this even when speed changes?"
_RT_STEPS = (
    "ask what density would do here",
    "compare flow with vs without density",
)


def _signal(state: str, *, fired: bool = True) -> MisconceptionSignal:
    return MisconceptionSignal(
        fired=fired,
        state=state,  # type: ignore[arg-type]
        description=_BANK_DESCRIPTION,
        bank_id=_BANK_ID,
        bank_code="no_density",
        probe=_PROBE_QUESTION,
        rt_steps=_RT_STEPS,
        confidence=0.85,
    )


# ---------------------------------------------------------------------------
# Suffix-level: no LLM call needed
# ---------------------------------------------------------------------------

def test_no_signal_returns_empty_suffix():
    assert _suffix_for_misconception(None) == ""


def test_unfired_signal_returns_empty_suffix(monkeypatch):
    monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", "1")
    sig = MisconceptionSignal(fired=False, state="default")
    assert _suffix_for_misconception(sig) == ""


def test_flag_off_returns_empty_suffix_even_when_fired(monkeypatch):
    monkeypatch.delenv("APOLLO_MISCONCEPTION_ENABLED", raising=False)
    assert _suffix_for_misconception(_signal("probe")) == ""


def test_probe_band_injects_authored_probe(monkeypatch):
    monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", "1")
    suffix = _suffix_for_misconception(_signal("probe"))
    assert _PROBE_QUESTION in suffix
    assert "(probe band)" in suffix


def test_socratic_band_injects_authored_rt_steps(monkeypatch):
    monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", "1")
    suffix = _suffix_for_misconception(_signal("socratic"))
    for step in _RT_STEPS:
        assert step in suffix
    assert "(socratic band)" in suffix


def test_suffix_never_contains_description_or_bank_id(monkeypatch):
    """The most important contract — internal-only fields never reach
    the LLM prompt under any condition."""
    monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", "1")
    for state in ("probe", "socratic", "default"):
        sig = _signal(state)
        suffix = _suffix_for_misconception(sig)
        assert _BANK_DESCRIPTION not in suffix, (
            f"description leaked into {state} suffix"
        )
        assert _BANK_ID not in suffix, (
            f"bank_id leaked into {state} suffix"
        )


def test_socratic_state_with_no_rt_steps_falls_through(monkeypatch):
    """Defensive: a malformed bank row (empty rt_steps) must not crash
    or leak — return empty suffix."""
    monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", "1")
    sig = MisconceptionSignal(
        fired=True,
        state="socratic",
        probe=_PROBE_QUESTION,
        rt_steps=(),  # empty
        bank_code="x",
    )
    assert _suffix_for_misconception(sig) == ""


def test_probe_state_with_no_probe_falls_through(monkeypatch):
    monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", "1")
    sig = MisconceptionSignal(
        fired=True,
        state="probe",
        probe=None,
        rt_steps=_RT_STEPS,
        bank_code="x",
    )
    assert _suffix_for_misconception(sig) == ""


# ---------------------------------------------------------------------------
# draft_reply integration: the misconception suffix actually reaches the LLM.
# ---------------------------------------------------------------------------

def _stub_openai_response(content: str = "I don't know what that is yet."):
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = content
    return mock_resp


def test_draft_reply_appends_probe_suffix_when_flag_on(monkeypatch):
    monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", "1")
    captured: dict = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _stub_openai_response()

    with patch("apollo.agent.apollo_llm.OpenAI") as mock_client:
        mock_client.return_value.chat.completions.create.side_effect = fake_create
        draft_reply(
            history=[{"role": "user", "content": "hi"}],
            kg_summary="(empty)",
            misconception=_signal("probe"),
        )

    system_prompt = captured["messages"][0]["content"]
    assert system_prompt.startswith(APOLLO_SYSTEM_PROMPT), (
        "system prompt prefix must remain stable"
    )
    assert _PROBE_QUESTION in system_prompt
    assert _BANK_DESCRIPTION not in system_prompt
    assert _BANK_ID not in system_prompt


def test_draft_reply_omits_misconception_suffix_when_flag_off(monkeypatch):
    monkeypatch.delenv("APOLLO_MISCONCEPTION_ENABLED", raising=False)
    captured: dict = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _stub_openai_response()

    with patch("apollo.agent.apollo_llm.OpenAI") as mock_client:
        mock_client.return_value.chat.completions.create.side_effect = fake_create
        draft_reply(
            history=[{"role": "user", "content": "hi"}],
            kg_summary="(empty)",
            misconception=_signal("probe"),
        )

    system_prompt = captured["messages"][0]["content"]
    assert _PROBE_QUESTION not in system_prompt
    assert "(probe band)" not in system_prompt


def test_draft_reply_with_no_misconception_unchanged(monkeypatch):
    """Backward-compat: callers that don't pass the new kwarg get the
    pre-P2 prompt byte-for-byte."""
    monkeypatch.setenv("APOLLO_MISCONCEPTION_ENABLED", "1")  # flag set
    captured: dict = {}

    def fake_create(**kwargs):
        captured["messages"] = kwargs["messages"]
        return _stub_openai_response()

    with patch("apollo.agent.apollo_llm.OpenAI") as mock_client:
        mock_client.return_value.chat.completions.create.side_effect = fake_create
        draft_reply(
            history=[{"role": "user", "content": "hi"}],
            kg_summary="(empty)",
            # misconception omitted
        )

    system_prompt = captured["messages"][0]["content"]
    assert system_prompt == APOLLO_SYSTEM_PROMPT
