from __future__ import annotations

import io
import json
from pathlib import Path
import types

import numpy as np
import pytest

from backend.ocr.provider import OCRBlock, OCRResult
from backend.qa import cmd_ingest_ocr


class _FakeProvider:
    def recognize(self, image_bytes: bytes, mime: str | None = None, dpi: int | None = None) -> OCRResult:  # type: ignore[override]
        return OCRResult(blocks=[OCRBlock(kind="text", text="test content"), OCRBlock(kind="latex", text=r"x=y")])


def test_cli_ingest_ocr(monkeypatch, tmp_path, capsys):
    # Force KB into temp
    monkeypatch.setenv("KNOWLEDGE_BASE_DIR", str(tmp_path / "kb"))

    # Monkeypatch OCR provider and rendering
    import backend.indexers.handwriting as hw
    monkeypatch.setattr(hw, "get_ocr_provider_from_env", lambda: _FakeProvider())

    # Render a single fake page
    def _fake_render(pdf_path: Path, dpi: int | None, max_pages: int | None):
        yield (1, b"\x89PNGfake")

    monkeypatch.setattr(hw, "_render_pdf_pages", _fake_render)

    # Skip embeddings/FAISS
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(hw, "embed_items", lambda items, model, dim: np.zeros((len(items), 8), dtype=np.float32))
    monkeypatch.setattr(hw, "build_faiss", lambda embeddings, out_path: Path(out_path).write_bytes(b""))

    # Prepare input PDF
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    out_dir = tmp_path / "store"

    args = types.SimpleNamespace(
        subject="Test Subject",
        kind="slides",
        pdf=str(pdf),
        out_dir=str(out_dir),
        dpi=None,
        max_pages=1,
        no_embed=True,
        workers=2,
    )

    cmd_ingest_ocr(args)

    # Artifacts exist
    assert (out_dir / "items.jsonl").exists()
    assert (out_dir / "meta.json").exists()

    # JSON printed
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert data["subject"] == "Test Subject"
    assert data["kind"] == "slides"
    assert data["index_path"].endswith("store")

