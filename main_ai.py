#!/usr/bin/env python3
"""Backend CLI hooking into retriever for single-shot and chat modes."""
import argparse
import os
from pathlib import Path

from retriever import load_assets, search, pack_context, answer, render_citations

DEFAULT_INDEX = Path(os.getenv("INDEX_DIR", "text-embeder/my_book_index_aero"))


def run_once(q: str) -> None:
    hits = search(q)
    ctx = pack_context(hits)
    ans = answer(q, ctx)
    print(ans.text)
    print(render_citations(ans))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backend QA interface")
    parser.add_argument("--q", help="Ask a single question and exit")
    parser.add_argument("--chat", action="store_true", help="Interactive chat mode")
    parser.add_argument(
        "--index", default=str(DEFAULT_INDEX), help="Path to index directory"
    )
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY first.")

    if args.q:
        try:
            load_assets(Path(args.index))
        except FileNotFoundError as e:
            print(
                f"Failed to load index at {args.index}: {e}. "
                "Did you run the embedder without --no_embed?"
            )
            return
        run_once(args.q)
        return

    if args.chat:
        try:
            load_assets(Path(args.index))
        except FileNotFoundError as e:
            print(
                f"Failed to load index at {args.index}: {e}. "
                "Did you run the embedder without --no_embed?"
            )
            return
        while True:
            try:
                q = input("Q> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q or q.lower() in {"exit", "quit"}:
                break
            run_once(q)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
