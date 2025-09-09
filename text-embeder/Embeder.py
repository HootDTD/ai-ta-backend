#!/usr/bin/env python3
"""
Embed a PDF into vectors using OpenAI text-embedding-3-large.

Outputs (in out_dir):
- chunks.jsonl     : one JSON per chunk (id, pages, text)
- embeddings.npy   : float32 L2-normalized matrix [num_chunks, dim]
- meta.json        : run metadata (model, dim, chunk/overlap sizes, source)
"""

import os, json, math, time, argparse
from pathlib import Path
from typing import List, Dict, Tuple
import numpy as np
from tqdm import tqdm

# PDF parsing
from pypdf import PdfReader

# Tokenization (for token-true chunk sizes)
import tiktoken

# OpenAI client (official)
from openai import OpenAI

# -------------------------
# Config defaults
# -------------------------
DEFAULT_MODEL = "text-embedding-3-large"
DEFAULT_DIM   = 3072          # can reduce via --dimensions if you want smaller vectors
CHUNK_TOKENS  = 1000          # safe, well below embedding input limits
OVERLAP_TOKENS = 150          # keeps continuity across chunks
BATCH_SIZE    = 64            # number of chunks per embeddings call

# -------------------------
# PDF → (page_num, text)
# -------------------------
def extract_pages(pdf_path: Path) -> List[Tuple[int, str]]:
    reader = PdfReader(str(pdf_path))
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        # normalize whitespace a bit
        text = " ".join(text.split())
        pages.append((i, text))
    return pages

# -------------------------
# Build a token stream across the whole book so chunks can span pages
# while tracking which pages each chunk covers.
# -------------------------
def chunk_book(pages: List[Tuple[int,str]], chunk_tokens=CHUNK_TOKENS, overlap_tokens=OVERLAP_TOKENS):
    enc = tiktoken.get_encoding("cl100k_base")
    # token stream: list of (token_id, page_num)
    stream = []
    for page_num, text in pages:
        ids = enc.encode(text)
        stream.extend([(tid, page_num) for tid in ids])

    i, n = 0, len(stream)
    chunk_id = 0
    while i < n:
        j = min(i + chunk_tokens, n)
        tok_ids = [tid for tid, _ in stream[i:j]]
        pages_covered = [p for _, p in stream[i:j]]
        if not pages_covered:
            break
        page_start, page_end = min(pages_covered), max(pages_covered)
        chunk_text = enc.decode(tok_ids)

        yield {
            "id": f"chunk-{chunk_id}",
            "page_start": page_start,
            "page_end": page_end,
            "text": chunk_text
        }
        chunk_id += 1

        if j >= n:
            break
        # Slide window with overlap
        i = max(0, j - overlap_tokens)

# -------------------------
# Embedding with retry/backoff
# -------------------------
def embed_texts(client: OpenAI, texts: List[str], model: str, dimensions: int, batch_size: int = BATCH_SIZE) -> np.ndarray:
    all_vecs = []
    for b in tqdm(range(0, len(texts), batch_size), desc="Embedding"):
        batch = texts[b:b+batch_size]
        # Basic retry loop
        for attempt in range(6):
            try:
                resp = client.embeddings.create(
                    model=model,
                    input=batch,
                    dimensions=dimensions
                )
                vecs = [d.embedding for d in resp.data]
                all_vecs.extend(vecs)
                break
            except Exception as e:
                # exponential backoff
                sleep = 2 ** attempt
                print(f"[warn] embedding batch {b}-{b+len(batch)} failed ({e}); retrying in {sleep}s...")
                time.sleep(sleep)
        else:
            raise RuntimeError("Failed to embed after several retries.")
    arr = np.array(all_vecs, dtype=np.float32)
    # L2-normalize so dot product == cosine similarity
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    arr = arr / np.clip(norms, 1e-12, None)
    return arr

# -------------------------
# Main
# -------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True, help="Path to the source PDF")
    ap.add_argument("--out_dir", default="embeddings_index", help="Where to write outputs")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Embedding model")
    ap.add_argument("--dimensions", type=int, default=DEFAULT_DIM, help="Embedding dimensions (e.g., 3072, 1024, 512)")
    ap.add_argument("--chunk_tokens", type=int, default=CHUNK_TOKENS)
    ap.add_argument("--overlap_tokens", type=int, default=OVERLAP_TOKENS)
    args = ap.parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENAI_API_KEY first.")

    pdf_path = Path(args.pdf)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[info] reading PDF: {pdf_path}")
    pages = extract_pages(pdf_path)

    print(f"[info] chunking...")
    chunks = list(chunk_book(pages, args.chunk_tokens, args.overlap_tokens))
    texts = [c["text"] for c in chunks]
    print(f"[info] created {len(chunks)} chunks")

    print(f"[info] embedding with {args.model} (dim={args.dimensions})")
    client = OpenAI()
    embeddings = embed_texts(client, texts, args.model, args.dimensions)

    # Save artifacts
    chunks_path = out_dir / "chunks.jsonl"
    embs_path   = out_dir / "embeddings.npy"
    meta_path   = out_dir / "meta.json"

    print(f"[info] writing {chunks_path}")
    with chunks_path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print(f"[info] writing {embs_path}  shape={embeddings.shape} dtype=float32")
    np.save(embs_path, embeddings)

    meta = {
        "source_pdf": str(pdf_path),
        "model": args.model,
        "dimensions": args.dimensions,
        "chunk_tokens": args.chunk_tokens,
        "overlap_tokens": args.overlap_tokens,
        "num_chunks": len(chunks)
    }
    print(f"[info] writing {meta_path}")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print("[done] All files written to:", str(out_dir.resolve()))

if __name__ == "__main__":
    main()
