#!/usr/bin/env python3
from __future__ import annotations
"""
Layout-aware, multimodal PDF embedder.

This script is a more advanced version of Embeder.py.  It uses PyMuPDF
(also known as `fitz`) to extract text blocks and images with their
bounding boxes, performs optional OCR on extracted images and prepares a
rich JSONL suitable for hybrid search.  The resulting text for each item
is embedded with OpenAI's `text-embedding-3-large` model and the vectors
can be indexed with FAISS.  Metadata is also persisted to an SQLite
FTS5 database for keyword search.

The code is written to operate even in constrained environments.  If a
dependency such as PyMuPDF or FAISS is not available the script will
emit a clear warning and skip the functionality that requires it.  This
keeps the pipeline testable on systems where heavy dependencies cannot be
installed (e.g. offline sandboxes).
"""

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

try:  # pragma: no cover - optional dependency
    import numpy as np
    HAVE_NUMPY = True
except Exception:  # pragma: no cover
    np = None
    HAVE_NUMPY = False

try:  # pragma: no cover - optional progress bar
    from tqdm import tqdm
except Exception:  # pragma: no cover
    def tqdm(x, *args, **kwargs):
        return x

# --- Optional imports -----------------------------------------------------
# PyMuPDF provides layout extraction.  It may not be available in some
# execution environments; in that case the script can still be imported and
# partially executed (e.g. unit tests) but will abort when run.
try:  # pragma: no cover - best effort import
    import fitz  # PyMuPDF
    HAVE_FITZ = True
except Exception:  # pragma: no cover - if library is missing
    fitz = None
    HAVE_FITZ = False

# FAISS for vector search.  Optional in the same way.
try:  # pragma: no cover
    import faiss
    HAVE_FAISS = True
except Exception:  # pragma: no cover
    faiss = None
    HAVE_FAISS = False

# OCR via pytesseract; Tesseract binary might not exist.  If it is missing we
# simply skip OCR text.
try:  # pragma: no cover
    import pytesseract
    from PIL import Image
    HAVE_TESS = True
except Exception:  # pragma: no cover
    pytesseract = None
    Image = None
    HAVE_TESS = False

# OpenCV for simple image preprocessing before OCR
try:  # pragma: no cover
    import cv2
    HAVE_CV2 = True
except Exception:  # pragma: no cover
    cv2 = None
    HAVE_CV2 = False

# Vision captioning model loaded lazily based on CLI flag
CAPTION_PROCESSOR = CAPTION_MODEL = None
HAVE_VISION = False
torch = None

def load_caption_model(model_name: str) -> None:  # pragma: no cover - heavyweight
    """Load a vision captioning model on demand."""
    global CAPTION_PROCESSOR, CAPTION_MODEL, HAVE_VISION, torch
    if CAPTION_MODEL is not None:
        return
    try:
        from transformers import BlipProcessor, BlipForConditionalGeneration
        import torch as _torch
        CAPTION_PROCESSOR = BlipProcessor.from_pretrained(model_name)
        CAPTION_MODEL = BlipForConditionalGeneration.from_pretrained(model_name)
        CAPTION_MODEL.eval()
        torch = _torch
        HAVE_VISION = True
    except Exception:
        CAPTION_PROCESSOR = CAPTION_MODEL = None
        HAVE_VISION = False

# Tokenization for chunk sizing
try:  # pragma: no cover
    import tiktoken
    ENCODER = tiktoken.get_encoding("cl100k_base")
    HAVE_TIKTOKEN = True
except Exception:  # pragma: no cover
    tiktoken = None
    HAVE_TIKTOKEN = False

    class _DummyEncoder:
        def encode(self, text: str):
            return text.split()

    ENCODER = _DummyEncoder()

# OpenAI API client imported lazily to allow running without the package

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Item:
    """Represents a chunk/figure/table extracted from the PDF."""

    id: str
    doc_id: str
    page: int
    type: str  # body|figure|table|caption|equation
    section_path: List[str]
    bbox: Optional[List[float]]
    text: str
    raw_text: str
    caption: Optional[str]
    figure_id: Optional[str]
    neighbors: List[str]
    parents: List[str]
    diagram_json: Optional[str]
    sha256: str
    source_pdf: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# ---------------------------------------------------------------------------
# PDF extraction utilities
# ---------------------------------------------------------------------------

def ocr_image(image_path: Path) -> str:
    """Run OCR on an image if Tesseract is available."""
    if not HAVE_TESS:
        return ""
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:  # pragma: no cover
        print(f"[warn] Tesseract not functional: {exc}")
        return ""
    try:
        img = Image.open(str(image_path))
        # Optional preprocessing to improve label/axes OCR
        if HAVE_CV2:
            import numpy as _np
            arr = _np.array(img)
            arr = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
            arr = cv2.resize(arr, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            _, arr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            img = Image.fromarray(arr)
        text = pytesseract.image_to_string(img)
        return " ".join(text.split())
    except Exception:
        return ""


def structure_caption(text: str, ocr: str) -> dict:
    """Expand a raw vision caption + OCR labels into structured JSON."""
    try:
        from openai import OpenAI
        client = OpenAI()
    except Exception:
        return {}


def extract_from_image(
    img_path: Path,
    doc_id: str,
    out_dir: Path,
    token_limit: int = 1000,
    overlap_tokens: int = 150,
    do_ocr: bool = True,
) -> List[Item]:
    """Extract text from a single image as Item objects."""
    if Image is None:
        raise SystemExit("Pillow is required for image extraction.")

    # Ensure output image directory exists for consistency with PDF mode
    (out_dir / "images").mkdir(parents=True, exist_ok=True)

    # Load image to confirm path is valid
    Image.open(str(img_path))

    text = ""
    if do_ocr and HAVE_TESS:
        text = ocr_image(img_path)

    lines = [" ".join(ln.split()) for ln in text.splitlines() if ln.strip()] if text else []
    enc = ENCODER
    items: List[Item] = []
    chunk: List[str] = []
    tokens = 0
    idx = 0
    prev_item_id: Optional[str] = None

    def flush() -> None:
        nonlocal chunk, tokens, idx, prev_item_id
        if not chunk:
            return
        t = " ".join(chunk)
        sha = hashlib.sha256(t.encode("utf-8")).hexdigest()
        item_id = f"{doc_id}:1:o{idx}"
        item = Item(
            id=item_id,
            doc_id=doc_id,
            page=1,
            type="body",
            section_path=["screenshot"],
            bbox=None,
            text=t,
            raw_text=t,
            caption=None,
            figure_id=None,
            neighbors=[],
            parents=[],
            diagram_json=None,
            sha256=sha,
            source_pdf=str(img_path),
        )
        if prev_item_id:
            item.neighbors.append(prev_item_id)
            items[-1].neighbors.append(item_id)
        items.append(item)
        prev_item_id = item_id
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
    system = (
        "You are describing a technical diagram. "
        "Given a raw caption and OCR text, return ONLY valid JSON with keys: "
        "caption (string), entities (list of strings), relations (list of 3-tuples "
        "[subject, relation, object]), labels (list), axes (object), equations (list), "
        "takeaways (list). Use empty lists/objects when unsure."
    )
    user = f"caption: {text}\nocr: {ocr}"
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0,
        )
        raw = resp.choices[0].message.content
        start = raw.find("{"); end = raw.rfind("}")
        payload = raw[start:end + 1] if start != -1 and end != -1 else raw
        data = json.loads(payload)
        for k, v in [("entities", []), ("relations", []), ("labels", []), ("axes", {}), ("equations", []), ("takeaways", [])]:
            data.setdefault(k, v)
        if not isinstance(data.get("relations"), list):
            data["relations"] = []
        return data
    except Exception:
        return {}

def extract_document(
    pdf_path: Path,
    doc_id: str,
    out_dir: Path,
    token_limit: int = 1000,
    overlap_tokens: int = 150,
    min_figure_area_ratio: float = 0.01,
    caption_model: str = "Salesforce/blip2-opt-2.7b",
    page_start: int = 1,
    page_end: Optional[int] = None,
    write_every: int = 0,
    items_path: Optional[Path] = None,
    debug: bool = False,
    do_ocr: bool = True,
) -> List[Item]:
    """Extract text blocks and images from the PDF as Item objects.

    Parameters
    ----------
    pdf_path: Path to the source PDF
    doc_id: identifier for the document
    out_dir: directory where images will be stored
    """
    if not HAVE_FITZ:
        raise SystemExit("PyMuPDF (fitz) is required for layout extraction.")

    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)
    if page_end is None or page_end > total_pages:
        page_end = total_pages
    page_start = max(1, page_start)

    items: List[Item] = []
    image_dir = out_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    diagram_dir = out_dir / "diagram_json"
    diagram_dir.mkdir(parents=True, exist_ok=True)

    pending: List[Item] = []
    first_write = True

    if caption_model:
        load_caption_model(caption_model)

    enc = ENCODER
    current_section: List[str] = []
    prev_item_id: Optional[str] = None
    block_freq: dict = {}

    for page_index in range(page_start - 1, page_end):
        page = doc[page_index]
        page_num = page_index + 1

        blocks = page.get_text("blocks")
        chunk_text: List[str] = []
        token_count = 0
        chunk_idx = 0
        page_item_indices: List[int] = []
        page_text_chars = 0
        ocr_line_count = 0
        last_block_y0 = last_block_y1 = 0

        def flush_chunk():
            nonlocal chunk_text, token_count, chunk_idx, prev_item_id
            if not chunk_text:
                return
            text = " ".join(chunk_text)
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            item_id = f"{doc_id}:{page_num}:c{chunk_idx}"
            item = Item(
                id=item_id,
                doc_id=doc_id,
                page=page_num,
                type="body",
                section_path=current_section.copy(),
                # store coarse y-span from last block to help figure linkage
                bbox=[0, last_block_y0, page.rect.width, last_block_y1] if last_block_y1 else None,
                text=text,
                raw_text=text,
                caption=None,
                figure_id=None,
                neighbors=[],
                parents=[],
                diagram_json=None,
                sha256=sha,
                source_pdf=str(pdf_path),
            )
            if prev_item_id:
                item.neighbors.append(prev_item_id)
                items[-1].neighbors.append(item_id)
            items.append(item)
            page_item_indices.append(len(items) - 1)
            prev_item_id = item_id
            chunk_idx += 1
            chunk_text = []
            token_count = 0

        for block in blocks:
            x0, y0, x1, y1, text, _, _ = block
            last_block_y0, last_block_y1 = y0, y1
            clean = " ".join((text or "").split())
            if not clean:
                continue
            page_text_chars += len(clean)
            h = hashlib.sha1(clean.encode("utf-8")).hexdigest()
            band = int((y0 + y1) // 50)
            key = (h, band)
            count = block_freq.get(key, 0) + 1
            block_freq[key] = count
            # Only treat as repeating header/footer after enough pages and when short
            if (page_index + 1) >= 10 and (count / (page_index + 1)) > 0.3 and len(clean) <= 120:
                continue
            m = re.match(r"^(\d+(?:\.\d+)*)\s+(.*)", clean)
            if m:
                flush_chunk()
                numbering, title = m.groups()
                levels = numbering.split(".")
                level_index = len(levels) - 1
                current_section[level_index:] = []
                current_section.append(f"{numbering} {title.strip()}")
                sha = hashlib.sha256(clean.encode("utf-8")).hexdigest()
                item_id = f"{doc_id}:{page_num}:h{chunk_idx}"
                item = Item(
                    id=item_id,
                    doc_id=doc_id,
                    page=page_num,
                    type="heading",
                    section_path=current_section.copy(),
                    bbox=[x0, y0, x1, y1],
                    text=clean,
                    raw_text=clean,
                    caption=None,
                    figure_id=None,
                    neighbors=[],
                    parents=[],
                    diagram_json=None,
                    sha256=sha,
                    source_pdf=str(pdf_path),
                )
                if prev_item_id:
                    item.neighbors.append(prev_item_id)
                    items[-1].neighbors.append(item_id)
                items.append(item)
                page_item_indices.append(len(items) - 1)
                prev_item_id = item_id
                chunk_idx += 1
                continue

            tokens = enc.encode(clean)
            if token_count + len(tokens) > token_limit:
                prev_text = " ".join(chunk_text)
                flush_chunk()
                if prev_text:
                    tail_tokens = enc.encode(prev_text)[-overlap_tokens:]
                    if tail_tokens:
                        if hasattr(enc, "decode"):
                            overlap_text = enc.decode(tail_tokens)
                        else:
                            overlap_text = " ".join(prev_text.split()[-overlap_tokens:])
                        chunk_text = [overlap_text]
                        token_count = len(enc.encode(overlap_text))
                    else:
                        chunk_text = []
                        token_count = 0
            chunk_text.append(clean)
            token_count += len(tokens)

        flush_chunk()

        if do_ocr and HAVE_TESS and page_text_chars < 500:
            try:  # pragma: no cover - heavy
                pix = page.get_pixmap(dpi=300)
                ocr_path = image_dir / f"page_{page_num:04d}_ocr.png"
                pix.save(str(ocr_path))
                ocr_txt = ocr_image(ocr_path)
                if ocr_txt:
                    ocr_lines = [" ".join(ln.split()) for ln in ocr_txt.splitlines() if ln.strip()]
                    ocr_line_count = len(ocr_lines)
                    o_chunk: List[str] = []
                    o_tokens = 0
                    o_idx = 0

                    def flush_ocr_chunk():
                        nonlocal o_chunk, o_tokens, o_idx, prev_item_id
                        if not o_chunk:
                            return
                        text = " ".join(o_chunk)
                        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
                        item_id = f"{doc_id}:{page_num}:o{o_idx}"
                        item = Item(
                            id=item_id,
                            doc_id=doc_id,
                            page=page_num,
                            type="body",
                            section_path=current_section.copy(),
                            bbox=None,
                            text=text,
                            raw_text=text,
                            caption=None,
                            figure_id=None,
                            neighbors=[],
                            parents=[],
                            diagram_json=None,
                            sha256=sha,
                            source_pdf=str(pdf_path),
                        )
                        if prev_item_id:
                            item.neighbors.append(prev_item_id)
                            items[-1].neighbors.append(item_id)
                        items.append(item)
                        page_item_indices.append(len(items) - 1)
                        prev_item_id = item_id
                        o_idx += 1
                        o_chunk = []
                        o_tokens = 0

                    for ln in ocr_lines:
                        toks = enc.encode(ln)
                        if o_tokens + len(toks) > token_limit:
                            prev_text = " ".join(o_chunk)
                            flush_ocr_chunk()
                            if prev_text:
                                tail = enc.encode(prev_text)[-overlap_tokens:]
                                if tail:
                                    if hasattr(enc, "decode"):
                                        overlap = enc.decode(tail)
                                    else:
                                        overlap = " ".join(prev_text.split()[-overlap_tokens:])
                                    o_chunk = [overlap]
                                    o_tokens = len(enc.encode(overlap))
                                else:
                                    o_chunk = []
                                    o_tokens = 0
                        o_chunk.append(ln)
                        o_tokens += len(toks)
                    flush_ocr_chunk()
            except Exception:
                pass

        # --- Images -------------------------------------------------------
        for img_index, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            rects = page.get_image_rects(xref)
            if not rects:
                continue
            bbox = max(rects, key=lambda r: (r.x1 - r.x0) * (r.y1 - r.y0))
            page_area = page.rect.width * page.rect.height
            area = (bbox.x1 - bbox.x0) * (bbox.y1 - bbox.y0)
            if area / page_area < min_figure_area_ratio:
                continue
            img_name = f"page_{page_num:04d}_{img_index:02d}.png"
            img_path = image_dir / img_name
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.alpha or pix.n >= 5:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                pix.save(str(img_path))
            except Exception:  # pragma: no cover - corrupt images etc.
                continue
            ocr_txt = ocr_image(img_path) if do_ocr else ""
            caption = ""
            if HAVE_VISION:
                try:  # pragma: no cover
                    raw = CAPTION_PROCESSOR(images=Image.open(str(img_path)), return_tensors="pt")
                    with torch.no_grad():
                        out = CAPTION_MODEL.generate(**raw)
                    caption = CAPTION_PROCESSOR.decode(out[0], skip_special_tokens=True)
                except Exception:
                    caption = ""
            caption_data = structure_caption(caption, ocr_txt)
            if not caption_data:
                caption_data = {
                    "caption": caption,
                    "entities": [],
                    "relations": [],
                    "labels": ocr_txt.split() if ocr_txt else [],
                    "axes": {},
                    "equations": [],
                    "takeaways": [],
                }
            if ocr_txt and not caption_data.get("labels"):
                caption_data["labels"] = ocr_txt.split()
            canon_parts = [caption_data.get("caption", "")]
            if caption_data.get("entities"):
                canon_parts.append("Entities: " + ", ".join(caption_data["entities"]))
            if caption_data.get("relations"):
                rel_strs = []
                for r in caption_data["relations"]:
                    if isinstance(r, (list, tuple)) and len(r) == 3:
                        rel_strs.append(f"{r[0]}-{r[1]}-{r[2]}")
                if rel_strs:
                    canon_parts.append("Relations: " + "; ".join(rel_strs))
            if caption_data.get("labels"):
                canon_parts.append("Labels: " + ", ".join(caption_data["labels"]))
            if caption_data.get("axes"):
                canon_parts.append("Axes: " + ", ".join(f"{k}={v}" for k, v in caption_data["axes"].items()))
            if caption_data.get("takeaways"):
                canon_parts.append("Takeaways: " + "; ".join(caption_data["takeaways"]))
            caption_text = " ".join(canon_parts).strip()
            sha = hashlib.sha256(caption_text.encode("utf-8")).hexdigest()
            diag_json_path = diagram_dir / (img_name.replace(".png", ".json"))
            diag_json_path.write_text(json.dumps(caption_data, ensure_ascii=False), encoding="utf-8")
            item_id = f"{doc_id}:{page_num}:f{img_index}"
            fig_id = f"p{page_num}-{img_index + 1:02d}"
            item = Item(
                id=item_id,
                doc_id=doc_id,
                page=page_num,
                type="figure",
                section_path=current_section.copy(),
                bbox=[float(bbox.x0), float(bbox.y0), float(bbox.x1), float(bbox.y1)],
                text=caption_text,
                raw_text=caption_text,
                caption=None,
                figure_id=fig_id,
                neighbors=[],
                parents=[],
                diagram_json=str(diag_json_path),
                sha256=sha,
                source_pdf=str(pdf_path),
            )
            if prev_item_id:
                item.neighbors.append(prev_item_id)
                items[-1].neighbors.append(item_id)
            items.append(item)
            page_item_indices.append(len(items) - 1)
            prev_item_id = item_id

        # neighbor graph: link figures to nearest body chunk and heading
        page_bodies = [items[i] for i in page_item_indices if items[i].type == "body"]
        page_heads = [items[i] for i in page_item_indices if items[i].type == "heading"]
        for idx in [i for i in page_item_indices if items[i].type == "figure"]:
            fig = items[idx]
            if fig.bbox and page_bodies:
                fy = (fig.bbox[1] + fig.bbox[3]) / 2
                body = min(
                    page_bodies,
                    key=lambda b: abs(fy - ((b.bbox[1] + b.bbox[3]) / 2) if b.bbox else fy),
                )
                fig.neighbors.append(body.id)
                body.neighbors.append(fig.id)
            if page_heads:
                cand = None
                for head in reversed(page_heads):
                    if head.bbox and head.bbox[1] <= fig.bbox[1]:
                        cand = head
                        break
                cand = cand or page_heads[-1]
                fig.parents.append(cand.id)

        page_items = [items[i] for i in page_item_indices]
        to_write = [it for it in page_items if (it.text or "").strip()]
        pending.extend(to_write)
        if debug:
            print(
                f"[debug] page {page_num}: chars={page_text_chars} ocr_lines={ocr_line_count} items={len(to_write)}"
            )
        if items_path and write_every > 0 and ((page_num - page_start + 1) % write_every == 0):
            mode = "w" if first_write else "a"
            with items_path.open(mode, encoding="utf-8") as f:
                for it in pending:
                    f.write(it.to_json() + "\n")
            first_write = False
            pending = []

    if items_path and pending:
        mode = "w" if first_write else "a"
        with items_path.open(mode, encoding="utf-8") as f:
            for it in pending:
                f.write(it.to_json() + "\n")

    return items


# ---------------------------------------------------------------------------
# Embedding and indexing utilities
# ---------------------------------------------------------------------------

def embed_items(items: List[Item], model: str, dimensions: int, batch_size: int = 64) -> np.ndarray:
    """Embed all items using OpenAI."""
    if not HAVE_NUMPY:
        raise SystemExit("numpy is required for embedding stage")
    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover
        raise SystemExit("openai package is required for embedding stage") from exc
    client = OpenAI()
    if not items:
        raise SystemExit(
            "No items to embed (did the extractor only produce 'ocr' items that are being filtered out?)."
        )
    texts = [it.text for it in items]
    vectors: List[List[float]] = []
    for start in tqdm(range(0, len(texts), batch_size), desc="embedding"):
        batch = texts[start:start + batch_size]
        for attempt in range(6):
            try:
                resp = client.embeddings.create(model=model, input=batch, dimensions=dimensions)
                vectors.extend([d.embedding for d in resp.data])
                break
            except Exception as exc:
                import time
                sleep = 2 ** attempt + random.random()
                print(f"[warn] embedding failed ({exc}); retrying in {sleep:.1f}s")
                time.sleep(sleep)
        else:
            raise RuntimeError("embedding failed after several retries")
    arr = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    arr = arr / np.clip(norms, 1e-12, None)
    return arr


def build_faiss(embeddings: np.ndarray, out_path: Path):
    if not HAVE_FAISS:
        print("[warn] faiss not installed; skipping index")
        return
    dim = embeddings.shape[1]
    index = faiss.IndexHNSWFlat(dim, 64)
    index.hnsw.efConstruction = 200
    index.hnsw.efSearch = 128
    index.add(embeddings)
    faiss.write_index(index, str(out_path))


def build_sqlite(items: List[Item], db_path: Path):
    """Persist item metadata alongside an FTS5 table compatible with the retriever."""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        # Keep a wide, plain table for debugging/inspection purposes.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS items_raw (
                id TEXT PRIMARY KEY,
                page INTEGER,
                type TEXT,
                text TEXT,
                doc_id TEXT,
                section_path TEXT,
                bbox TEXT,
                caption TEXT,
                figure_id TEXT,
                diagram_json TEXT,
                sha256 TEXT,
                source_pdf TEXT
            )
            """
        )
        cur.execute("DELETE FROM items_raw")
        raw_rows = [
            (
                it.id,
                it.page,
                it.type,
                it.text,
                it.doc_id,
                json.dumps(it.section_path),
                json.dumps(it.bbox),
                it.caption,
                it.figure_id,
                it.diagram_json,
                it.sha256,
                it.source_pdf,
            )
            for it in items
        ]
        cur.executemany(
            "INSERT OR REPLACE INTO items_raw VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            raw_rows,
        )
        conn.commit()

        try:  # pragma: no cover - some SQLite builds may lack FTS5
            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS items USING fts5(
                    id,
                    doc_id,
                    page UNINDEXED,
                    type,
                    section_path,
                    text,
                    figure_id,
                    tokenize='porter'
                )
                """
            )
            # Drop legacy auxiliary FTS tables produced by older versions.
            cur.execute("DROP TABLE IF EXISTS fts")
            conn.commit()
            cur.execute("DELETE FROM items")
            fts_rows = [
                (
                    it.id,
                    it.doc_id,
                    it.page,
                    it.type,
                    " > ".join(it.section_path or []),
                    it.text or "",
                    it.figure_id,
                )
                for it in items
            ]
            cur.executemany(
                "INSERT INTO items VALUES (?,?,?,?,?,?,?)",
                fts_rows,
            )
            conn.commit()
        except sqlite3.OperationalError as exc:  # pragma: no cover
            # Surface the failure so index generation can alert the caller.
            conn.rollback()
            raise RuntimeError("SQLite build lacks FTS5 support required for lexical search") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Layout-aware multimodal embedder")
    ap.add_argument("--pdf", help="Source PDF path")
    ap.add_argument("--img", help="Source image path (.png/.jpg/.jpeg)")
    ap.add_argument("--doc_id", required=True, help="Document identifier/slug")
    ap.add_argument("--out_dir", default="index", help="Output directory root")
    ap.add_argument("--model", default="text-embedding-3-large")
    ap.add_argument("--dimensions", type=int, default=3072)
    ap.add_argument("--token_limit", type=int, default=1000)
    ap.add_argument("--overlap_tokens", type=int, default=150)
    ap.add_argument("--min_figure_area_ratio", type=float, default=0.01)
    ap.add_argument("--caption_model", default="Salesforce/blip2-opt-2.7b")
    ap.add_argument("--no_embed", action="store_true", help="Skip embedding/FAISS stage")
    ap.add_argument("--page_start", type=int, default=1)
    ap.add_argument("--page_end", type=int, default=0)
    ap.add_argument("--write_every", type=int, default=0)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--no_ocr", action="store_true")
    args = ap.parse_args()
    if not args.pdf and not args.img:
        ap.error("either --pdf or --img must be provided")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ocr_enabled = HAVE_TESS and not args.no_ocr
    if args.img and args.caption_model == ap.get_default("caption_model"):
        args.caption_model = ""

    if args.debug:
        ver = None
        if HAVE_TESS:
            try:
                ver = pytesseract.get_tesseract_version()
            except Exception as exc:  # pragma: no cover
                ver = f"error: {exc}"
        print(f"[debug] pytesseract={ver} ocr_enabled={ocr_enabled}")

    if args.img:
        img_path = Path(args.img)
        print(f"[info] extracting {img_path}")
        items = extract_from_image(
            img_path,
            args.doc_id,
            out_dir,
            token_limit=args.token_limit,
            overlap_tokens=args.overlap_tokens,
            do_ocr=ocr_enabled,
        )
        items = [it for it in items if (it.text or "").strip()]
        if not items:
            print("[warn] no items extracted; exiting")
            return
        items_path = out_dir / "items.jsonl"
        with items_path.open("w", encoding="utf-8") as f:
            for it in items:
                f.write(it.to_json() + "\n")
        print(f"[info] wrote {items_path}")
    else:
        pdf_path = Path(args.pdf)
        print(f"[info] extracting {pdf_path}")
        items = extract_document(
            pdf_path,
            args.doc_id,
            out_dir,
            token_limit=args.token_limit,
            overlap_tokens=args.overlap_tokens,
            min_figure_area_ratio=args.min_figure_area_ratio,
            caption_model=args.caption_model,
            page_start=args.page_start,
            page_end=(args.page_end or None),
            write_every=args.write_every,
            items_path=out_dir / "items.jsonl",
            debug=args.debug,
            do_ocr=ocr_enabled,
        )
        items = [it for it in items if (it.text or "").strip()]
        if not items:
            print("[warn] no items extracted; exiting")
            return
        items_path = out_dir / "items.jsonl"
        if items_path.exists():
            with items_path.open("r", encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
            if line_count == 0:
                print("[warn] items.jsonl contains 0 lines; exiting")
                return
        print(f"[info] wrote {items_path}")

    if args.no_embed:
        print("[info] skipping embedding as requested")
        return

    print("[info] embedding items...")
    embeddings = embed_items(items, args.model, args.dimensions)
    assert len(items) == embeddings.shape[0], "items and embeddings misaligned"
    if HAVE_NUMPY:
        emb_path = out_dir / "embeddings.npy"
        np.save(emb_path, embeddings)
        print(f"[info] wrote {emb_path}")

    faiss_path = out_dir / "faiss.index"
    build_faiss(embeddings, faiss_path)

    db_path = out_dir / "sqlite.db"
    build_sqlite(items, db_path)
    print(f"[info] wrote {db_path}")

    from collections import Counter

    counts = Counter(it.type for it in items)
    tokenizer_name = getattr(ENCODER, "name", type(ENCODER).__name__)
    fitz_version = getattr(fitz, "__doc__", None) if HAVE_FITZ else None
    try:
        import transformers  # pragma: no cover
        transformers_version = transformers.__version__
    except Exception:
        transformers_version = None
    if args.img:
        src_path = Path(args.img)
    else:
        src_path = Path(args.pdf)
    doc_hash = hashlib.sha256(src_path.read_bytes()).hexdigest()
    meta = {
        "source_pdf": str(src_path),
        "source_pdf_sha256": doc_hash,
        "model": args.model,
        "dimensions": int(embeddings.shape[1]),
        "num_items": len(items),
        "counts_by_type": counts,
        "page_count": (1 if args.img else max((it.page for it in items), default=0)),
        "has_faiss": HAVE_FAISS,
        "has_ocr": ocr_enabled,
        "caption_model": args.caption_model if HAVE_VISION and args.caption_model else None,
        "tokenizer": tokenizer_name,
        "token_limit": args.token_limit,
        "overlap_tokens": args.overlap_tokens,
        "min_figure_area_ratio": args.min_figure_area_ratio,
        "fitz_version": fitz_version,
        "torch_version": getattr(torch, "__version__", None) if HAVE_VISION else None,
        "transformers_version": transformers_version,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print("[done] all artifacts written to", out_dir)


if __name__ == "__main__":  # pragma: no cover
    main()
