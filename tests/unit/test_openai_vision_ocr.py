import json
from unittest.mock import MagicMock

from ocr.factory import get_ocr_provider_from_env
from ocr.openai_vision import OpenAIVisionOCRProvider


def _fake_client(payload: dict) -> MagicMock:
    client = MagicMock()
    msg = MagicMock()
    msg.content = json.dumps(payload)
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    return client


def test_recognize_returns_block_with_confidence():
    client = _fake_client({"text": "x = \\frac{1}{2} g t^2", "confidence": 0.82})
    prov = OpenAIVisionOCRProvider(client=client, model="gpt-4o")
    result = prov.recognize(b"\x89PNG fake", mime="image/png")
    assert result.fused_text == "x = \\frac{1}{2} g t^2"
    assert result.blocks[0].confidence == 0.82
    # the image was sent as a data URL on an image_url content part
    sent = client.chat.completions.create.call_args.kwargs
    parts = sent["messages"][-1]["content"]
    assert any(p.get("type") == "image_url" for p in parts)


def test_recognize_unparseable_response_is_low_confidence_not_crash():
    client = MagicMock()
    msg = MagicMock()
    msg.content = "not json"
    client.chat.completions.create.return_value = MagicMock(choices=[MagicMock(message=msg)])
    prov = OpenAIVisionOCRProvider(client=client, model="gpt-4o")
    result = prov.recognize(b"img", mime="image/png")
    assert result.fused_text == ""
    assert (result.average_confidence or 0.0) == 0.0


def test_factory_selects_openai_provider(monkeypatch):
    monkeypatch.setenv("OCR_PROVIDER", "openai")
    monkeypatch.setenv("APOLLO_OCR_MODEL", "gpt-4o")

    provider = get_ocr_provider_from_env()

    assert isinstance(provider, OpenAIVisionOCRProvider)
