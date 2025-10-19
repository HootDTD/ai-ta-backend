from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np

import backend.indexers.handwriting as hw
from backend.ocr.provider import OCRBlock, OCRResult


class _FakeProvider:
    def recognize(self, image_bytes: bytes, mime: str | None = None, dpi: int | None = None) -> OCRResult:  # type: ignore[override]
        # Ignore image content; return mixed text + latex
        blocks = [
            OCRBlock(kind="text", text="Velocity boundary layer over a flat plate."),
            OCRBlock(kind="latex", text=r"\\delta(x) = 5.0 \\sqrt{\\nu x / U_\\infty}"),
        ]
        return OCRResult(blocks=blocks)


def test_ingest_builds_artifacts_with_mocked_ocr(monkeypatch, tmp_path):
    # Monkeypatch OCR provider factory to return our fake
    monkeypatch.setattr(hw, "get_ocr_provider_from_env", lambda: _FakeProvider())

    # Monkeypatch renderer to avoid PyMuPDF dependency
    def _fake_render(pdf_path: Path, dpi: int | None, max_pages: int | None):
        yield (1, b"\x89PNGfake")

    monkeypatch.setattr(hw, "_render_pdf_pages", _fake_render)

    # Monkeypatch embedding and FAISS build to avoid network/heavy deps
    def _fake_embed(items, model, dim):
        return np.zeros((len(items), 16), dtype=np.float32)

    monkeypatch.setattr(hw, "embed_items", _fake_embed)
    monkeypatch.setattr(hw, "build_faiss", lambda embeddings, out_path: Path(out_path).write_bytes(b""))

    pdf_path = tmp_path / "fixture.pdf"
    pdf_path.write_bytes(b"%PDF-FAKE")
    out_dir = tmp_path / "out"

    items = hw.ingest(pdf_path, doc_id="docX", out_dir=out_dir, options=hw.IngestOptions(embed_dim=16, do_embed=True))

    # Items contain at least one text and one equation
    kinds = {it.type for it in items}
    assert "body" in kinds and "equation" in kinds

    # Artifacts present
    assert (out_dir / "items.jsonl").exists()
    assert (out_dir / "embeddings.npy").exists()
    assert (out_dir / "faiss.index").exists()
    assert (out_dir / "sqlite.db").exists()
    assert (out_dir / "meta.json").exists()

    # items.jsonl schema contains required keys
    first_line = (out_dir / "items.jsonl").read_text(encoding="utf-8").splitlines()[0]
    obj = json.loads(first_line)
    for key in ["id", "doc_id", "page", "type", "text", "sha256", "source_pdf"]:
        assert key in obj

