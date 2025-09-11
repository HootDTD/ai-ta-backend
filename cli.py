#!/usr/bin/env python3
"""Backend CLI hooking into retriever for single-shot and chat modes.

Refactored to delegate core Q&A to backend.core.answer_question while keeping
the same user-visible behavior.
"""
import argparse
import os
from pathlib import Path

from .core import answer_question

DEFAULT_INDEX = Path(
    os.getenv(
        "INDEX_DIR",
        Path(__file__).resolve().parent / "text-embeder/my_book_index_aero",
    )
)


def run_once(q: str, index: Path | None = None) -> None:
    # Delegate to core; stream if generator, else print once
    result = answer_question(q, doc_sets=[str(index)] if index else None)
    try:
        for chunk in result:  # type: ignore
            print(chunk, end="")
        print()
    except TypeError:
        print(result)  # type: ignore


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
            run_once(args.q, index=Path(args.index))
        except FileNotFoundError as e:
            print(
                f"Failed to load index at {args.index}: {e}. "
                "Did you run the embedder without --no_embed?"
            )
            return
        return

    if args.chat:
        while True:
            try:
                q = input("Q> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q or q.lower() in {"exit", "quit"}:
                break
            try:
                run_once(q, index=Path(args.index))
            except FileNotFoundError as e:
                print(
                    f"Failed to load index at {args.index}: {e}. "
                    "Did you run the embedder without --no_embed?"
                )
                break
        return

    parser.print_help()


if __name__ == "__main__":
    main()
