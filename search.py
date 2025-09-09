#!/usr/bin/env python3
"""Simple embedding search over a local index."""

import argparse
import json
import os
from pathlib import Path

import numpy as np
from openai import OpenAI

DEFAULT_MODEL = os.getenv("MODEL", "gpt-4o-mini")


def load_index(index_dir: Path):
    """Load embeddings and chunk metadata from an index directory."""
    embs = np.load(index_dir / "embeddings.npy")
    chunks = []
    with open(index_dir / "chunks.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            chunks.append(json.loads(line))
    with open(index_dir / "meta.json", "r", encoding="utf-8") as f:
        meta = json.load(f)
    return embs, chunks, meta


def embed_query(client: OpenAI, query: str, model: str, dim: int) -> np.ndarray:
    """Embed the query text using the specified model and dimensionality."""
    vec = client.embeddings.create(model=model, input=[query], dimensions=dim).data[0].embedding
    vec = np.asarray(vec, dtype=np.float32)
    vec /= max(np.linalg.norm(vec), 1e-12)
    return vec


def top_k(query_vec: np.ndarray, emb_mat: np.ndarray, k: int):
    sims = emb_mat @ query_vec
    idx = sims.argsort()[-k:][::-1]
    return idx, sims[idx]


def main() -> None:
    ap = argparse.ArgumentParser(description="Search a local embedding index")
    ap.add_argument("--q", required=True, help="Query text")
    ap.add_argument("--k", type=int, default=5, help="Number of results to show")
    ap.add_argument(
        "--index",
        help="Index directory; overrides INDEX_DIR env var",
    )
    args = ap.parse_args()

    default_index = Path(__file__).resolve().parent / "text-embeder/my_book_index"
    index_dir = Path(args.index or os.getenv("INDEX_DIR", str(default_index)))

    client = OpenAI()
    emb_mat, chunks, meta = load_index(index_dir)
    dim = int(meta["dimensions"])
    embed_model = meta["model"]
    qv = embed_query(client, args.q, embed_model, dim)
    idx, sims = top_k(qv, emb_mat, args.k)
    for i, score in zip(idx, sims):
        c = chunks[int(i)]
        print(f"{c['id']} p.{c['page_start']}-{c['page_end']} score={score:.3f}\n{c['text']}\n")


if __name__ == "__main__":
    main()
