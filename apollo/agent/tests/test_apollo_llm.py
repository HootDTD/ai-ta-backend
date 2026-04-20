from unittest.mock import MagicMock, patch

from apollo.agent.apollo_llm import draft_reply, APOLLO_SYSTEM_PROMPT


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
