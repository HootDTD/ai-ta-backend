
#!/usr/bin/env python3
"""
main_ai.py — Closed-book Q&A over a local embedding index.

Requirements (installed already from your embedder step):
  pip install openai numpy

ENV:
  OPENAI_API_KEY   : your key
  MODEL            : (optional) default "gpt-4o-mini"
  INDEX_DIR        : (optional) default "my_book_index"

Run:
  python main_ai.py --q "Where does the wolf meet Little Red?"
  # or interactive:
  python main_ai.py --chat
"""

import os, json, re, argparse
from pathlib import Path
from typing import List, Dict, Tuple
import numpy as np
from openai import OpenAI

# ---------------- Config ----------------
DEFAULT_MODEL   = os.getenv("MODEL", "gpt-4o-mini")
INDEX_DIR       = Path(os.getenv("INDEX_DIR", "my_book_index"))
TOP_K           = 8
MAX_CONTEXT_CHARS = 12000  # guard to keep prompts reasonable

STRICT_SYSTEM_PROMPT = """You are a careful study assistant.
You may ONLY use the passages in CONTEXT to answer.
- If the answer is not fully supported by CONTEXT, reply exactly: "Not in the book."
- Do NOT use any prior knowledge or outside facts.
- Every paragraph of your answer must include page citations like [p. 12-13] that match the pages of the supporting chunks in CONTEXT.
- Be concise but logically thorough. If the question has multiple parts, answer each, citing pages for each part."""

USER_PROMPT_TEMPLATE = """Answer the question using ONLY the following CONTEXT.
Cite pages like [p. start-end] in every paragraph.

QUESTION:
{question}

CONTEXT:
{context}
"""

# --------------- Index I/O ---------------
def load_index(index_dir: Path):
    embs = np.load(index_dir / "embeddings.npy")
    chunks = []
    with open(index_dir / "chunks.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            chunks.append(json.loads(line))
    with open(index_dir / "meta.json", "r", encoding="utf-8") as f:
        meta = json.load(f)
    return embs, chunks, meta

def embed_query(client: OpenAI, q: str, embed_model: str, dim: int) -> np.ndarray:
    v = client.embeddings.create(model=embed_model, input=[q], dimensions=dim).data[0].embedding
    v = np.asarray(v, dtype=np.float32)
    v /= max(np.linalg.norm(v), 1e-12)  # L2-normalize to match stored vectors
    return v

def top_k(query_vec: np.ndarray, emb_mat: np.ndarray, chunks: List[Dict], k=TOP_K):
    sims = emb_mat @ query_vec  # cosine because vectors are normalized
    idx = sims.argsort()[-k:][::-1]
    out = []
    for i in idx:
        c = chunks[i].copy()
        c["_score"] = float(sims[i])
        out.append(c)
    return out

def build_context(passages: List[Dict]) -> str:
    # Format each chunk with its id and page range, limited by MAX_CONTEXT_CHARS
    parts = []
    total = 0
    for c in passages:
        head = f"[{c['id']}] pages {c['page_start']}-{c['page_end']}"
        body = c["text"]
        block = head + "\n" + body
        if total + len(block) > MAX_CONTEXT_CHARS and parts:
            break
        parts.append(block)
        total += len(block)
    return "\n\n---\n\n".join(parts)

# --------------- Model call ---------------
def ask_closed_book(question: str, model: str = DEFAULT_MODEL):
    client = OpenAI()
    emb_mat, chunks, meta = load_index(INDEX_DIR)

    # Embed query with the SAME embedding model & dimensions used in the index
    dim = int(meta["dimensions"])
    embed_model = meta["model"]  # "text-embedding-3-large"
    qv = embed_query(client, question, embed_model, dim)

    # Retrieve and build context
    passages = top_k(qv, emb_mat, chunks, k=TOP_K)
    context = build_context(passages)

    messages = [
        {"role": "system", "content": STRICT_SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT_TEMPLATE.format(question=question, context=context)}
    ]

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
        max_tokens=800
    )
    answer = resp.choices[0].message.content.strip()
    verify_note = verify_citations(answer, passages)
    return answer, passages, verify_note

# --------------- Simple attribution check ---------------
_CIT_RE = re.compile(r"\[p\.\s*(\d+)(?:\s*-\s*(\d+))?\]")

def verify_citations(answer: str, passages: List[Dict]) -> str:
    """
    Light-weight checker:
    - Ensures at least one [p. x-y] citation exists per paragraph.
    - Ensures cited page numbers fall within any retrieved passage ranges.
    """
    if not answer:
        return "No answer."

    # Build allowed page ranges from retrieved chunks
    ranges = [(c["page_start"], c["page_end"]) for c in passages]

    def page_in_ranges(p: int) -> bool:
        for a, b in ranges:
            if a <= p <= b:
                return True
        return False

    paras = [p for p in (answer.split("\n\n")) if p.strip()]
    issues = []

    for idx, para in enumerate(paras, start=1):
        cites = list(_CIT_RE.finditer(para))
        if not cites:
            issues.append(f"Paragraph {idx}: missing [p. #] citation.")
            continue
        for m in cites:
            p1 = int(m.group(1))
            p2 = int(m.group(2)) if m.group(2) else p1
            if not (page_in_ranges(p1) or page_in_ranges(p2)):
                issues.append(f"Paragraph {idx}: cited page [{p1}-{p2}] not in retrieved ranges.")

    if not issues:
        return "Citations present and within retrieved page ranges."
    return " | ".join(issues)

# --------------- CLI ---------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", help="Ask one question and exit")
    ap.add_argument("--chat", action="store_true", help="Interactive loop")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Model (default: {DEFAULT_MODEL})")
    args = ap.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY first.")

    if args.q:
        ans, passages, note = ask_closed_book(args.q, model=args.model)
        print("\n=== Answer ===\n" + ans)
        print("\n=== Retrieved (top-k) ===")
        for c in passages:
            print(f"- {c['id']}  p.{c['page_start']}-{c['page_end']}  score={c['_score']:.3f}")
        print("\n=== Verification ===\n" + note)
        return

    if args.chat:
        print("Closed-book study chat. Type 'exit' to quit.\n")
        while True:
            try:
                q = input("Q> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q or q.lower() in {"exit","quit"}:
                break
            ans, passages, note = ask_closed_book(q, model=args.model)
            print("\nA>\n" + ans + "\n")
            print("Retrieved: " + ", ".join(f"{c['id']}[p.{c['page_start']}-{c['page_end']}]" for c in passages))
            print("Verify: " + note + "\n")
    else:
        print("Nothing to do. Use --q \"...\" or --chat.")

if __name__ == "__main__":
    main()
