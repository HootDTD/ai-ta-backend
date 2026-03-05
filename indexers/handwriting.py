from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

from backend.ocr import OCRBlock, get_ocr_provider_from_env


def _load_layout_module():
    backend_dir = Path(__file__).resolve().parents[1]
    layout_path = backend_dir / "text-embeder" / "layout_multimodal_embedder.py"
    spec = importlib.util.spec_from_file_location("layout_multimodal_embedder", layout_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load layout embedder module at {layout_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_LAYOUT = _load_layout_module()


@dataclass
class IngestOptions:
    dpi: int | None = 300
    max_pages: int | None = None
    embedding_model: str = "text-embedding-3-large"
    embed_dim: int = 3072
    do_embed: bool = True


def _render_pdf_pages(pdf_path: Path, dpi: int | None, max_pages: int | None) -> Iterable[tuple[int, bytes]]:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise RuntimeError("PyMuPDF is required for PDF rendering") from exc

    with fitz.open(str(pdf_path)) as doc:
        page_count = len(doc)
        limit = min(page_count, max_pages) if max_pages else page_count
        for idx in range(limit):
            page = doc[idx]
            matrix = fitz.Matrix((dpi or 300) / 72.0, (dpi or 300) / 72.0)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            yield idx + 1, pix.tobytes("png")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _to_item(doc_id: str, source_pdf: Path, page_num: int, idx: int, block: OCRBlock):
    if block.kind == "latex":
        chunk_type = "equation"
    else:
        chunk_type = "body"
    text = (block.text or "").strip()
    return SimpleNamespace(
        id=f"{doc_id}:{page_num}:{idx}",
        doc_id=doc_id,
        page=page_num,
        type=chunk_type,
        text=text,
        raw_text=text,
        section_path=[],
        figure_id=None,
        source_pdf=str(source_pdf),
        sha256=_sha256_text(f"{doc_id}:{page_num}:{idx}:{text}"),
    )


def _write_items_jsonl(items: list[SimpleNamespace], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8") as fh:
        for it in items:
            row = {
                "id": it.id,
                "doc_id": it.doc_id,
                "page": it.page,
                "type": it.type,
                "text": it.text,
                "raw_text": it.raw_text,
                "section_path": it.section_path,
                "figure_id": it.figure_id,
                "sha256": it.sha256,
                "source_pdf": it.source_pdf,
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def ingest(
    pdf_path: Path,
    *,
    doc_id: str,
    out_dir: Path,
    options: IngestOptions | None = None,
) -> list[SimpleNamespace]:
    options = options or IngestOptions()
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    provider = get_ocr_provider_from_env()
    items: list[SimpleNamespace] = []

    for page_num, image_bytes in _render_pdf_pages(pdf_path, options.dpi, options.max_pages):
        result = provider.recognize(image_bytes=image_bytes, mime="image/png", dpi=options.dpi)
        for idx, block in enumerate(result.blocks or []):
            items.append(_to_item(doc_id, pdf_path, page_num, idx, block))

    _write_items_jsonl(items, out_dir / "items.jsonl")

    embeddings = None
    if options.do_embed:
        embeddings = _LAYOUT.embed_items(items, model=options.embedding_model, dim=options.embed_dim)
        _LAYOUT.np.save(out_dir / "embeddings.npy", embeddings)
        _LAYOUT.build_faiss(embeddings, out_dir / "faiss.index")
    else:
        # Keep artifact contract stable even when embeddings are disabled.
        (out_dir / "embeddings.npy").write_bytes(b"")
        (out_dir / "faiss.index").write_bytes(b"")

    _LAYOUT.build_sqlite(items, out_dir / "sqlite.db")

    meta = {
        "doc_id": doc_id,
        "source_pdf": str(pdf_path),
        "item_count": len(items),
        "embedded": bool(options.do_embed),
        "embed_dim": int(options.embed_dim),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return items


__all__ = ["IngestOptions", "ingest", "_LAYOUT", "_render_pdf_pages", "get_ocr_provider_from_env"]
