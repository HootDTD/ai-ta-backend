from unittest.mock import MagicMock, patch

import pytest

from apollo.errors import NoMatchingConceptError
from apollo.overseer.concept_inference import infer_concept_cluster


def _mock_reply(text: str) -> MagicMock:
    fake = MagicMock()
    fake.choices = [MagicMock(message=MagicMock(content=text))]
    return fake


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_returns_known_cluster(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply('{"cluster_id": "fluid_mechanics"}')
    mock_client_cls.return_value = client

    cluster = infer_concept_cluster(
        transcript="Student asked about Bernoulli's principle in horizontal pipes.",
        available_clusters=["fluid_mechanics"],
    )
    assert cluster == "fluid_mechanics"


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_raises_on_unknown_cluster(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply('{"cluster_id": "cooking"}')
    mock_client_cls.return_value = client

    with pytest.raises(NoMatchingConceptError):
        infer_concept_cluster(
            transcript="How do I bake a cake?",
            available_clusters=["fluid_mechanics"],
        )


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_raises_on_null_cluster(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply('{"cluster_id": null}')
    mock_client_cls.return_value = client

    with pytest.raises(NoMatchingConceptError):
        infer_concept_cluster(
            transcript="General chat, no topic.",
            available_clusters=["fluid_mechanics"],
        )


@patch("apollo.overseer.concept_inference.OpenAI")
def test_infer_raises_on_invalid_json(mock_client_cls):
    client = MagicMock()
    client.chat.completions.create.return_value = _mock_reply("not json at all")
    mock_client_cls.return_value = client

    with pytest.raises(NoMatchingConceptError):
        infer_concept_cluster(
            transcript="whatever",
            available_clusters=["fluid_mechanics"],
        )
