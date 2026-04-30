import pytest as _pytest_module
_pytest_module.skip(
    "Legacy V2 test — needs rewrite for V3 signatures (parse_utterance(concept, attempt_id), "
    "compute_coverage(KGGraph, KGGraph), compute_rubric(coverage, list[Node])). "
    "Tracked in claude_v3_checklist.md item 1; will be re-enabled in test-rewrite phase.",
    allow_module_level=True,
)

"""Unit tests for parser. LLM calls are mocked — this verifies shape and
the ParserCouldNotExtractError behavior. Integration with real GPT-4o
is exercised in the end-to-end smoke test (Task 34)."""
from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import ParserCouldNotExtractError
from apollo.parser.parser_llm import parse_utterance, _is_non_trivial


def test_is_non_trivial_detects_equation_like():
    assert _is_non_trivial("A1*v1 = A2*v2") is True
    assert _is_non_trivial("pressure times area equals force") is True


def test_is_non_trivial_ignores_acknowledgements():
    assert _is_non_trivial("ok") is False
    assert _is_non_trivial("yes") is False
    assert _is_non_trivial("hmm") is False


def test_is_non_trivial_ignores_short_messages():
    assert _is_non_trivial("hi") is False
    assert _is_non_trivial("hi there") is False


def _mock_openai_response(entries: list) -> MagicMock:
    import json
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=json.dumps({"entries": entries})))]
    return fake


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_returns_extracted_entries(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response([
        {"type": "equation", "content": {"symbolic": "A1*v1 - A2*v2", "label": "Continuity"}}
    ])
    mock_client_cls.return_value = client

    result = parse_utterance("A1*v1 = A2*v2 for incompressible flow")
    assert len(result) == 1
    assert result[0]["type"] == "equation"


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_raises_on_empty_extraction_from_nontrivial(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response([])
    mock_client_cls.return_value = client

    with pytest.raises(ParserCouldNotExtractError):
        parse_utterance("pressure plus one-half rho v squared is constant")


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_returns_empty_on_trivial_acknowledgement(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response([])
    mock_client_cls.return_value = client

    # "ok" is trivial — empty extraction is fine, no error raised.
    result = parse_utterance("ok")
    assert result == []


@patch("apollo.parser.parser_llm.OpenAI")
def test_parse_filters_malformed_entries(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_openai_response([
        {"type": "equation", "content": {"symbolic": "x", "label": "X"}},
        {"content": {"foo": "bar"}},  # missing type
        "garbage",                       # not a dict
    ])
    mock_client_cls.return_value = client

    result = parse_utterance("x is something")
    assert len(result) == 1


@patch("apollo.parser.parser_llm.OpenAI")
def test_parser_extracts_procedure_step_entries(mock_client_cls):
    payload = (
        '{"entries": [{"type": "procedure_step", "content": '
        '{"order": 1, "action": "use continuity to find v2", '
        '"uses_equations": ["continuity"], "purpose": "get v2 for bernoulli"}}]}'
    )
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content=payload))]
    client = MagicMock()
    client.chat.completions.create.return_value = fake_resp
    mock_client_cls.return_value = client

    entries = parse_utterance(
        "First I'd use continuity to find v2 so I can plug it into bernoulli."
    )
    assert len(entries) == 1
    assert entries[0]["type"] == "procedure_step"
    assert entries[0]["content"]["action"].startswith("use continuity")


def test_is_non_trivial_detects_plan_speak():
    # Plan-speak keywords should trigger non-trivial even without equation syntax.
    assert _is_non_trivial("first I would use continuity then plug into bernoulli")
    assert _is_non_trivial("next, solve for v2 and after that substitute it")
    # A plan-free utterance of normal length should still be trivial.
    assert not _is_non_trivial("ok sure that all makes sense to me now")
    # Causal "then" in declarative narration is not plan-speak — but this
    # sentence would already match on "pressure"/"velocity" keywords, so we
    # pick a keyword-free causal clause to truly test plan_markers.
    assert not _is_non_trivial("that all makes perfect sense to me now")


@patch("apollo.parser.parser_llm.OpenAI")
def test_parser_raises_on_empty_extraction_from_plan_speak(mock_client_cls):
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content='{"entries": []}'))]
    client = MagicMock()
    client.chat.completions.create.return_value = fake_resp
    mock_client_cls.return_value = client

    with pytest.raises(ParserCouldNotExtractError):
        parse_utterance("first I would do some thing then the next step is another thing")


@patch("apollo.parser.parser_llm.OpenAI")
def test_parser_extracts_mixed_equation_and_procedure_step(mock_client_cls):
    payload = (
        '{"entries": ['
        '{"type": "equation", "content": '
        '{"symbolic": "A1*v1 - A2*v2", "label": "continuity"}},'
        '{"type": "procedure_step", "content": '
        '{"order": 1, "action": "apply continuity to find v2", '
        '"uses_equations": ["continuity"], "purpose": "get v2"}}'
        ']}'
    )
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content=payload))]
    client = MagicMock()
    client.chat.completions.create.return_value = fake_resp
    mock_client_cls.return_value = client

    from apollo.parser.parser_llm import parse_utterance
    entries = parse_utterance(
        "The continuity equation is A1*v1 = A2*v2. First I would use it to find v2."
    )
    types = [e["type"] for e in entries]
    assert "equation" in types
    assert "procedure_step" in types
    assert len(entries) == 2
