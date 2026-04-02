from __future__ import annotations

import base64
import io
import json
import types
import urllib.request

import pytest

from ocr.mathpix import MathpixOCRProvider, MathpixConfig


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = io.BytesIO(body)

    def read(self) -> bytes:
        return self._body.read()

    def __enter__(self):  # context manager support
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_mathpix_adapter_parses_blocks_and_confidence(monkeypatch):
    # Prepare fake API response
    fake = {
        "text": "Hello world",
        "latex_styled": "\\int f(x) \\mathrm{d}x",
        "confidence": 0.8,
    }
    payload = json.dumps(fake).encode("utf-8")

    # Intercept urlopen to return the fake payload
    def _fake_urlopen(req: urllib.request.Request):  # type: ignore[override]
        # Validate request basics
        assert req.full_url.endswith("/v3/text")
        headers_lower = {k.lower(): v for k, v in req.headers.items()}
        assert headers_lower.get("app_id") == "id"
        assert headers_lower.get("app_key") == "key"
        body = json.loads(req.data.decode("utf-8"))
        assert body.get("formats") == ["text", "latex_styled"]
        assert "src" in body
        assert isinstance(body["src"], str) and body["src"].startswith("data:image/")
        return _FakeHTTPResponse(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    provider = MathpixOCRProvider(MathpixConfig(app_id="id", app_key="key", dpi=300))
    image_bytes = b"\x89PNG..."  # any bytes
    result = provider.recognize(image_bytes, mime="image/png")

    # Verify blocks
    assert len(result.blocks) == 2
    assert result.blocks[0].kind == "text"
    assert result.blocks[0].text == "Hello world"
    assert result.blocks[1].kind == "latex"
    assert "\\int" in result.blocks[1].text

    # Fused text and average confidence
    assert result.fused_text.startswith("Hello world")
    assert pytest.approx(result.average_confidence or 0.0, rel=1e-6) == 0.8

