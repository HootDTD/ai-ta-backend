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
