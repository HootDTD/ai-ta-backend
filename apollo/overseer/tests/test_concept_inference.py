"""Tests for the concept-id inference LLM hop (WU-3D Task 3).

`infer_concept_id` is given the transcript + the course's candidate concepts
({concept_id, display_name}) and returns exactly one concept_id (int) from the
provided set, or raises NoMatchingConceptError. The LLM is mocked deterministically.
"""

from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import NoMatchingConceptError
from apollo.overseer.concept_inference import infer_concept_id
from apollo.subjects.curriculum_db import ConceptRow


def _mock_reply(text: str) -> MagicMock:
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=text))]
    return fake


_CANDIDATES = [ConceptRow(concept_id=11, slug="bernoulli", display_name="Bernoulli")]


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_returns_concept_id_from_candidates(mock_client_cls):
    """T3.1 — LLM returns a candidate id; infer returns it as an int."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply('{"concept_id": 11}')
    mock_client_cls.return_value = client

    result = infer_concept_id(
        transcript="Student explained Bernoulli's principle in horizontal pipes.",
        candidates=_CANDIDATES,
    )
    assert result == 11
    assert isinstance(result, int)


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_raises_on_unknown_concept_id(mock_client_cls):
    """T3.2 — an id not in the candidate set raises."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply('{"concept_id": 999}')
    mock_client_cls.return_value = client

    with pytest.raises(NoMatchingConceptError):
        infer_concept_id(transcript="off-topic", candidates=_CANDIDATES)


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_raises_on_null(mock_client_cls):
    """T3.3 — a null concept_id raises (incl. the no-curriculum empty-candidates
    path that yields null)."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply('{"concept_id": null}')
    mock_client_cls.return_value = client

    with pytest.raises(NoMatchingConceptError):
        infer_concept_id(transcript="general chat", candidates=_CANDIDATES)


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_raises_on_invalid_json(mock_client_cls):
    """T3.4 — invalid JSON raises."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("not json at all")
    mock_client_cls.return_value = client

    with pytest.raises(NoMatchingConceptError):
        infer_concept_id(transcript="whatever", candidates=_CANDIDATES)


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_sends_candidate_display_names_to_llm(mock_client_cls):
    """T3.5 — the candidate concept_ids + display_names are sent to the LLM
    (proves the course's concepts, not a constant, drive the prompt)."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply('{"concept_id": 11}')
    mock_client_cls.return_value = client

    candidates = [
        ConceptRow(concept_id=11, slug="bernoulli", display_name="Bernoulli"),
        ConceptRow(concept_id=12, slug="ohm", display_name="Ohm's Law"),
    ]
    infer_concept_id(transcript="t", candidates=candidates)

    _, kwargs = client.chat.completions.create.call_args
    user_content = next(m["content"] for m in kwargs["messages"] if m["role"] == "user")
    for c in candidates:
        assert str(c.concept_id) in user_content
        assert c.display_name in user_content


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_rejects_bool_concept_id(mock_client_cls):
    """T3.x — a boolean (JSON true) must NOT pass the int membership check
    (avoids the True == 1 foot-gun)."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply('{"concept_id": true}')
    mock_client_cls.return_value = client

    candidates = [ConceptRow(concept_id=1, slug="x", display_name="X")]
    with pytest.raises(NoMatchingConceptError):
        infer_concept_id(transcript="t", candidates=candidates)
