"""Simple CLI for retrieval-based Q&A."""
import argparse
import json
import os
from pathlib import Path
from typing import Dict

from dataclasses import asdict

from .retriever import (
    load_assets,
    load_assets_all,
    search,
    pack_context,
    answer,
    render_citations,
    research,
)
from .orchestrator import Orchestrator
from .main_ai import parse_question

DEFAULT_INDEX = str(Path(__file__).resolve().parent / "text-embeder/my_book_index_aero")


def cmd_search(args: argparse.Namespace) -> None:
    indexes = args.index or [DEFAULT_INDEX]
    try:
        if len(indexes) > 1:
            _, skipped = load_assets_all([Path(p) for p in indexes])
            if skipped:
                print(f"Skipped {len(skipped)} indexes")
        else:
            load_assets(Path(indexes[0]))
    except (FileNotFoundError, RuntimeError) as e:
        print(str(e))
        return
    hits, _ = search(args.query)
    for h in hits[:12]:
        print(f"{h.id}\t{h.score_fused:.3f}\t{h.score_sem:.3f}\t{h.score_lex:.3f}")


def cmd_ask(args: argparse.Namespace) -> None:
    indexes = args.index or [DEFAULT_INDEX]
    try:
        if len(indexes) > 1:
            _, skipped = load_assets_all([Path(p) for p in indexes])
            if skipped:
                print(f"Skipped {len(skipped)} indexes")
        else:
            load_assets(Path(indexes[0]))
    except (FileNotFoundError, RuntimeError) as e:
        print(str(e))
        return
    hits, _ = search(args.question)
    ctx = pack_context(hits)
    ans = answer(args.question, ctx)
    print("=== Answer ===\n" + ans.text + "\n")
    print("Citations:", render_citations(ans))
    proof = {
        "question": args.question,
        "used_ids": ctx.used_ids,
        "snippets": [s.__dict__ for s in ctx.snippets],
    }
    with open("proof.json", "w", encoding="utf-8") as f:
        json.dump(proof, f, ensure_ascii=False, indent=2)


def cmd_solve(args: argparse.Namespace) -> None:
    orch = Orchestrator()
    task = {
        "user_query": args.question,
        "doc_sets": args.index or [DEFAULT_INDEX],
        "k_sem": args.k_sem,
        "k_lex": args.k_lex,
        "token_budget": args.token_budget,
    }
    try:
        final = orch.run(task)
    except RuntimeError as exc:
        print(f"{exc}")
        return
    print(final.text)


def cmd_research(args: argparse.Namespace) -> None:
    parsed = parse_question(args.query)
    opts = {
        "doc_sets": args.index or [DEFAULT_INDEX],
        "k_sem": args.k_sem,
        "k_lex": args.k_lex,
        "token_budget": args.token_budget,
    }
    try:
        bundle = research(parsed, opts)
    except RuntimeError as exc:
        print(f"{exc}")
        return
    if args.full:
        print(json.dumps(asdict(bundle), indent=2, ensure_ascii=False))
        return
    markers = [sn.citation_marker for sn in bundle.snippets[:5]]
    counts: Dict[str, int] = {
        "loaded": len(getattr(bundle.metadata, "loaded_indexes", [])),
        "skipped": len(getattr(bundle.metadata, "skipped_indexes", [])),
    }
    meta = asdict(bundle.metadata)
    meta["index_counts"] = counts
    skeleton = {
        "metadata": meta,
        "snippets": markers,
        "coverage_gaps": bundle.coverage_gaps,
    }
    print(json.dumps(skeleton, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Hybrid search QA")
    sub = parser.add_subparsers(dest="cmd")

    p_search = sub.add_parser("search", help="Search index")
    p_search.add_argument("query")
    p_search.add_argument(
        "--index", action="append", help="Path to index; may be used multiple times"
    )
    p_search.set_defaults(func=cmd_search)

    p_ask = sub.add_parser("ask", help="Legacy open-book answer")
    p_ask.add_argument("question")
    p_ask.add_argument(
        "--index", action="append", help="Path to index; may be used multiple times"
    )
    p_ask.set_defaults(func=cmd_ask)

    p_solve = sub.add_parser("solve", help="Closed-book solve with citations")
    p_solve.add_argument("question")
    p_solve.add_argument("--index", action="append", help="Path to index; may be used multiple times")
    p_solve.add_argument("--k-sem", type=int, default=30, help="Semantic top-K")
    p_solve.add_argument("--k-lex", type=int, default=30, help="Lexical top-K")
    p_solve.add_argument("--token-budget", type=int, default=6000, help="Token budget for context packing")
    p_solve.set_defaults(func=cmd_solve)

    p_research = sub.add_parser("research", help="Retrieve only and print bundle")
    p_research.add_argument("query")
    p_research.add_argument("--index", action="append", help="Path to index; may be used multiple times")
    p_research.add_argument("--k-sem", type=int, default=30, help="Semantic top-K")
    p_research.add_argument("--k-lex", type=int, default=30, help="Lexical top-K")
    p_research.add_argument("--token-budget", type=int, default=6000, help="Token budget for context packing")
    p_research.add_argument("--full", action="store_true", help="Dump full bundle")
    p_research.set_defaults(func=cmd_research)

    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required")

    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
