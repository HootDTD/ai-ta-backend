import os
from unittest.mock import MagicMock, patch

import pytest

from apollo.agent.apollo_llm import (
    APOLLO_SYSTEM_PROMPT,
    ContextOverflowError,
    _TOKEN_BUDGET,
    draft_reply,
)


def _mock_reply(text: str) -> MagicMock:
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=text))]
    return fake


def test_system_prompt_contains_absolute_rules():
    # Verify the prompt enforces the ignorance contract.
    assert "know NOTHING" in APOLLO_SYSTEM_PROMPT or "knows nothing" in APOLLO_SYSTEM_PROMPT.lower()
    assert "never name" in APOLLO_SYSTEM_PROMPT.lower() or "never introduce" in APOLLO_SYSTEM_PROMPT.lower()
    assert "never correct" in APOLLO_SYSTEM_PROMPT.lower()


def test_system_prompt_does_not_mention_fluid_mechanics_or_physics_domain():
    """Domain leaks from the prompt itself are a v1 finding we refused to carry forward."""
    assert "fluid" not in APOLLO_SYSTEM_PROMPT.lower()
    assert "physics" not in APOLLO_SYSTEM_PROMPT.lower() or "you know nothing about physics" in APOLLO_SYSTEM_PROMPT.lower()


def test_system_prompt_promotes_introspection_not_premature_confidence():
    """Per v1 Session-2 finding: prompt must push toward expressing uncertainty."""
    lower = APOLLO_SYSTEM_PROMPT.lower()
    # Should not be telling Apollo to claim it 'gets it' prematurely.
    assert "\"get it\"" not in APOLLO_SYSTEM_PROMPT
    assert "get it" not in lower or "if i had" in lower or "chain break" in lower or "gap" in lower


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_returns_string(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("What does that mean?")
    mock_client_cls.return_value = client

    out = draft_reply(
        history=[{"role": "user", "content": "Pressure plus kinetic energy density is constant."}],
        kg_summary="- equation (Bernoulli): P + Rational(1,2)*rho*v**2 - C",
    )
    assert out == "What does that mean?"


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_passes_kg_summary_to_llm(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("ok")
    mock_client_cls.return_value = client

    draft_reply(history=[], kg_summary="SENTINEL_KG_SUMMARY_12345")
    called = client.chat.completions.create.call_args
    messages = called.kwargs["messages"]
    joined = " ".join(m["content"] for m in messages)
    assert "SENTINEL_KG_SUMMARY_12345" in joined


def test_system_prompt_replaces_probe_with_confusion():
    from apollo.agent.apollo_llm import APOLLO_SYSTEM_PROMPT
    lower = APOLLO_SYSTEM_PROMPT.lower()
    # Old probe-as-default language is gone.
    assert "probe for clarifications" not in lower
    # Confusion-as-default language is present.
    assert (
        "express genuine confusion" in lower
        or "express confusion" in lower
        or "don't know which one to start with" in lower
        or "stuck student" in lower
    )


def test_system_prompt_keeps_ignorance_contract():
    from apollo.agent.apollo_llm import APOLLO_SYSTEM_PROMPT
    lower = APOLLO_SYSTEM_PROMPT.lower()
    # Core invariants must survive the rewrite.
    assert "know nothing" in lower
    assert "never correct" in lower
    assert "never volunteer" in lower or "never name" in lower


def test_system_prompt_ungates_chain_break_behavior():
    from apollo.agent.apollo_llm import APOLLO_SYSTEM_PROMPT
    lower = APOLLO_SYSTEM_PROMPT.lower()
    # The old prompt gated chain-break on "if the user asks whether you have enough".
    # The new prompt should not condition the chain-break behavior on the student asking.
    assert "if the user asks whether you have enough" not in lower


def test_system_prompt_has_confusion_exit_condition():
    from apollo.agent.apollo_llm import APOLLO_SYSTEM_PROMPT
    lower = APOLLO_SYSTEM_PROMPT.lower()
    # Apollo must have an explicit instruction for when to stop expressing confusion,
    # to avoid perma-confusion after the student has fully explained the problem.
    assert (
        "accounted for" in lower
        or "every symbol" in lower
        or "trace a path" in lower
    )


def test_system_prompt_distinguishes_plan_from_subject_questions():
    from apollo.agent.apollo_llm import APOLLO_SYSTEM_PROMPT
    lower = APOLLO_SYSTEM_PROMPT.lower()
    # Core new behavior: ask about the plan, not about the subject/physics itself.
    assert "plan" in lower
    assert "subject" in lower


# ── Item #2: history summary + token budget + model env ────────────────────


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_includes_history_summary_when_provided(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("ok")
    mock_client_cls.return_value = client

    draft_reply(
        history=[{"role": "user", "content": "latest"}],
        kg_summary="kg",
        history_summary="EARLIER_SUMMARY_SENTINEL",
    )
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    joined = " ".join(m["content"] for m in messages)
    assert "EARLIER_SUMMARY_SENTINEL" in joined


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_omits_history_summary_block_when_none(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("ok")
    mock_client_cls.return_value = client

    draft_reply(history=[], kg_summary="kg", history_summary=None)
    messages = client.chat.completions.create.call_args.kwargs["messages"]
    contents = [m["content"] for m in messages]
    assert not any("Earlier-conversation summary" in c for c in contents)


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_uses_apollo_model_env_when_set(mock_client_cls, monkeypatch):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("ok")
    mock_client_cls.return_value = client

    monkeypatch.setenv("APOLLO_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("MAIN_MODEL", "gpt-4o")

    draft_reply(history=[], kg_summary="kg")
    used_model = client.chat.completions.create.call_args.kwargs["model"]
    assert used_model == "gpt-4o-mini"


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_falls_back_to_main_model(mock_client_cls, monkeypatch):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("ok")
    mock_client_cls.return_value = client

    monkeypatch.delenv("APOLLO_MODEL", raising=False)
    monkeypatch.setenv("MAIN_MODEL", "gpt-4o-2024-11-20")

    draft_reply(history=[], kg_summary="kg")
    used_model = client.chat.completions.create.call_args.kwargs["model"]
    assert used_model == "gpt-4o-2024-11-20"


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_explicit_model_arg_wins(mock_client_cls, monkeypatch):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("ok")
    mock_client_cls.return_value = client
    monkeypatch.setenv("APOLLO_MODEL", "via-env")

    draft_reply(history=[], kg_summary="kg", model="explicit")
    used_model = client.chat.completions.create.call_args.kwargs["model"]
    assert used_model == "explicit"


@patch("apollo.agent.apollo_llm.OpenAI")
def test_draft_reply_raises_on_token_overflow(mock_client_cls):
    """Pathological prompt size raises rather than silently truncating."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("ok")
    mock_client_cls.return_value = client

    huge = "word " * 200_000  # ~50k tokens of "word " -> roughly _TOKEN_BUDGET
    # Build a history big enough to push past _TOKEN_BUDGET on cl100k.
    big_history = [{"role": "user", "content": huge}] * 6
    with pytest.raises(ContextOverflowError) as exc_info:
        draft_reply(history=big_history, kg_summary="kg")
    assert exc_info.value.budget == _TOKEN_BUDGET
    assert exc_info.value.tokens > _TOKEN_BUDGET


# ── v1 diff-at-Done: dumb-chatbot reply fed only KG + problem ───────────────


from apollo.agent import apollo_llm


class _CaptureClient:
    """Fake OpenAI client capturing the messages passed to chat.completions."""
    captured = {}

    def __init__(self, *a, **k):
        pass

    class _Chat:
        class _Completions:
            def create(self, **kwargs):
                _CaptureClient.captured = kwargs

                class _Msg:
                    content = "ok — what do I do next?"

                class _Choice:
                    message = _Msg()

                class _Resp:
                    choices = [_Choice()]

                return _Resp()

        completions = _Completions()

    chat = _Chat()


def test_draft_reply_includes_problem_and_kg_only(monkeypatch):
    monkeypatch.setattr(apollo_llm, "OpenAI", _CaptureClient)
    reply = apollo_llm.draft_reply(
        history=[{"role": "user", "content": "pressure plus half rho v squared is constant"}],
        kg_summary="equation: P + 1/2 rho v^2 = const",
        problem_text="Water flows through a horizontal pipe...",
    )
    assert reply == "ok — what do I do next?"
    system_blob = "\n".join(
        m["content"] for m in _CaptureClient.captured["messages"] if m["role"] == "system"
    )
    # Problem statement is now in the prompt.
    assert "horizontal pipe" in system_blob
    # KG summary is in the prompt.
    assert "1/2 rho v^2" in system_blob
    # No leaked signal-suffix vocabulary.
    assert "SUFFICIENCY SIGNAL" not in system_blob
    assert "MISCONCEPTION SIGNAL" not in system_blob
    assert "OLM INVITE" not in system_blob


def test_draft_reply_rejects_removed_kwargs():
    import inspect
    sig = inspect.signature(apollo_llm.draft_reply)
    for gone in ("sufficiency", "misconception", "olm_invite"):
        assert gone not in sig.parameters
