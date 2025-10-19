from __future__ import annotations

"""Handwriting/Slides PDF indexer (artifact-compatible).

Renders PDF pages to images, runs OCR via the pluggable OCR provider, chunks
text and LaTeX into items compatible with the existing retrieval pipeline, and
writes standard artifacts: items.jsonl, embeddings.npy, faiss.index, sqlite.db,
meta.json.

This module does not alter existing textbook pipelines; it can be invoked
explicitly to produce an index directory for image-heavy PDFs (handwritten
scans, slide decks, etc.).
"""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from pydantic import BaseModel, Field

from backend.ocr import get_ocr_provider_from_env

try:  # optional dependency for rendering
    import fitz  # type: ignore
    _HAVE_FITZ = True
except Exception:  # pragma: no cover
    fitz = None  # type: ignore
    _HAVE_FITZ = False

# Dynamically load the layout embedder utilities to avoid invalid module names
from importlib.util import spec_from_file_location, module_from_spec


def _load_layout_module():
    path = Path(__file__).resolve().parents[1] / "text-embeder" / "layout_multimodal_embedder.py"
    spec = spec_from_file_location("backend_layout_embedder", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import layout embedder from {path}")
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


_LAYOUT = _load_layout_module()


class IngestOptions(BaseModel):
    token_limit: int = Field(1000, ge=100)
    overlap_tokens: int = Field(150, ge=0)
    max_pages: Optional[int] = Field(None, ge=1)
    workers: int = Field(4, ge=1)
    embed_model: str = Field("text-embedding-3-large")
    embed_dim: int = Field(3072, ge=8)
    dpi: Optional[int] = None
    do_embed: bool = True


def _render_pdf_pages(pdf_path: Path, dpi: Optional[int], max_pages: Optional[int]) -> Iterable[Tuple[int, bytes]]:
    """Yield (page_num, png_bytes) for each page, up to max_pages.

    Uses PyMuPDF if available. Intended to be monkeypatched in tests.
    """
    if not _HAVE_FITZ:
        raise RuntimeError("PyMuPDF (fitz) is required to render PDF pages")
    doc = fitz.open(str(pdf_path))  # type: ignore[attr-defined]
    total = len(doc)
    n = min(total, max_pages or total)
    for i in range(n):
        page = doc[i]
        # scaling via DPI: scale factor ~ dpi/72
        if dpi and dpi > 0:
            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)  # type: ignore[attr-defined]
        else:
            mat = fitz.Matrix(2.0, 2.0)  # default 144 DPI approx
        pix = page.get_pixmap(matrix=mat)
        yield (i + 1, pix.tobytes("png"))


def _chunk_text(lines: List[str], doc_id: str, page: int, token_limit: int, overlap_tokens: int):
    Item = _LAYOUT.Item  # type: ignore[attr-defined]
    items: List[Item] = []
    enc = _LAYOUT.ENCODER
    chunk: List[str] = []
    tokens = 0
    idx = 0
    prev_id: Optional[str] = None

    def flush() -> None:
        nonlocal chunk, tokens, idx, prev_id
        if not chunk:
            return
        text = " ".join(chunk)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        item_id = f"{doc_id}:{page}:c{idx}"
            item = Item(
                id=item_id,
                doc_id=doc_id,
                page=page,
                type="body",
                section_path=["ocr"],
            bbox=None,
            text=text,
            raw_text=text,
            caption=None,
            figure_id=None,
            neighbors=[],
            parents=[],
            diagram_json=None,
            sha256=sha,
            source_pdf=str(doc_id),
        )
        if prev_id:
            item.neighbors.append(prev_id)
            items[-1].neighbors.append(item_id)
        items.append(item)
        prev_id = item_id
        idx += 1
        chunk = []
        tokens = 0

    for ln in lines:
        toks = enc.encode(ln)
        if tokens + len(toks) > token_limit:
            prev_text = " ".join(chunk)
            flush()
            if prev_text:
                tail = enc.encode(prev_text)[-overlap_tokens:]
                if tail:
                    if hasattr(enc, "decode"):
                        overlap = enc.decode(tail)
                    else:
                        overlap = " ".join(prev_text.split()[-overlap_tokens:])
                    chunk = [overlap]
                    tokens = len(enc.encode(overlap))
                else:
                    chunk = []
                    tokens = 0
        chunk.append(ln)
        tokens += len(toks)
    flush()
    return items


def _equation_items(latex_blocks: List[str], doc_id: str, page: int):
    Item = _LAYOUT.Item  # type: ignore[attr-defined]
    items: List[Item] = []
    for i, expr in enumerate(latex_blocks):
        text = expr.strip()
        if not text:
            continue
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        item_id = f"{doc_id}:{page}:e{i}"
        items.append(
            Item(
                id=item_id,
                doc_id=doc_id,
                page=page,
                type="equation",
                section_path=["ocr"],
                bbox=None,
                text=text,
                raw_text=text,
                caption=None,
                figure_id=None,
                neighbors=[],
                parents=[],
                diagram_json=None,
                sha256=sha,
                source_pdf=str(doc_id),
            )
        )
    return items


def ingest(pdf_path: Path, doc_id: str, out_dir: Path, *, options: Optional[IngestOptions] = None) -> List[Item]:
    """Ingest a PDF by rendering pages and running OCR, then write artifacts.

    Returns the list of Item objects that were embedded and indexed.
    """
    opts = options or IngestOptions()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "images").mkdir(parents=True, exist_ok=True)

    provider = get_ocr_provider_from_env()
    if provider is None:
        raise RuntimeError("OCR provider not configured. Set OCR_PROVIDER and credentials.")

    # Render pages and run OCR in parallel
    futures = []
    pages: List[Tuple[int, bytes]] = []
    for page_num, png_bytes in _render_pdf_pages(pdf_path, opts.dpi, opts.max_pages):
        pages.append((page_num, png_bytes))

    def _process(page_num: int, data: bytes) -> Tuple[int, List[Item], Optional[float]]:
        # Save page image for traceability
        img_name = f"page_{page_num:04d}.png"
        (out_dir / "images" / img_name).write_bytes(data)
        res = provider.recognize(data, mime="image/png", dpi=opts.dpi)
        text_lines: List[str] = []
        latex_blocks: List[str] = []
        for b in res.blocks:
            if b.kind == "latex":
                latex_blocks.append(b.text)
            elif b.text:
                # split to lines, normalize whitespace
                for ln in b.text.splitlines():
                    ln = " ".join(ln.split())
                    if ln:
                        text_lines.append(ln)
        items_text = _chunk_text(text_lines, doc_id, page_num, opts.token_limit, opts.overlap_tokens)
        items_eq = _equation_items(latex_blocks, doc_id, page_num)
        return page_num, items_text + items_eq, res.average_confidence

    items_all: List[Item] = []
    conf_samples: List[float] = []
    with ThreadPoolExecutor(max_workers=opts.workers) as ex:
        for page_num, data in pages:
            futures.append(ex.submit(_process, page_num, data))
        for fut in as_completed(futures):
            _, its, confidence = fut.result()
            items_all.extend(its)
            if isinstance(confidence, (int, float)):
                conf_samples.append(float(confidence))

    # Stable ordering by id
    items_all.sort(key=lambda it: it.id)

    # Write items.jsonl
    items_path = out_dir / "items.jsonl"
    with items_path.open("w", encoding="utf-8") as f:
        for it in items_all:
            f.write(it.to_json() + "\n")

    avg_confidence: Optional[float] = None
    if conf_samples:
        avg_confidence = sum(conf_samples) / len(conf_samples)

    if not opts.do_embed:
        # Write a minimal meta even if embeddings are skipped (useful in dry runs)
        _write_meta(out_dir, pdf_path, items_all, opts, None, avg_confidence)
        return items_all

    if not getattr(_LAYOUT, "HAVE_NUMPY", False):
        raise RuntimeError("numpy is required for embedding")

    # Embed and build indexes
    embeddings = _LAYOUT.embed_items(items_all, opts.embed_model, opts.embed_dim)
    if getattr(_LAYOUT, "np", None) is None:
        raise RuntimeError("numpy not available")
    _LAYOUT.np.save(out_dir / "embeddings.npy", embeddings)
    _LAYOUT.build_faiss(embeddings, out_dir / "faiss.index")
    _LAYOUT.build_sqlite(items_all, out_dir / "sqlite.db")

    _write_meta(out_dir, pdf_path, items_all, opts, embeddings.shape[1], avg_confidence)
    return items_all


def _write_meta(out_dir: Path, pdf_path: Path, items: List[Item], opts: IngestOptions, dims: Optional[int], avg_confidence: Optional[float]) -> None:
    from collections import Counter

    counts = Counter(it.type for it in items)
    doc_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    meta = {
        "source_pdf": str(pdf_path.name),
        "source_pdf_sha256": doc_hash,
        "model": opts.embed_model,
        "dimensions": int(dims) if dims is not None else None,
        "num_items": len(items),
        "counts_by_type": dict(counts),
        "page_count": max((it.page for it in items), default=0),
        "has_faiss": True,
        "has_ocr": True,
        "caption_model": None,
        "tokenizer": getattr(_LAYOUT.ENCODER, "name", type(_LAYOUT.ENCODER).__name__),
        "token_limit": opts.token_limit,
        "overlap_tokens": opts.overlap_tokens,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    if avg_confidence is not None:
        meta["average_confidence"] = avg_confidence
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
