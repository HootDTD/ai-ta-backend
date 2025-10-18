"""Simple CLI for retrieval-based Q&A."""
import argparse
import json
import os
from pathlib import Path
from typing import Dict

from dataclasses import asdict

from .config import set_subject_name
from .retriever import (
    load_assets,
    load_assets_all,
    search,
    answer,
    render_citations,
    ContextPack,
    ContextSnippet,
)
from .orchestrator import Orchestrator
from .main_ai import _write_proof_citations, _write_citations_file

DEFAULT_INDEX = str(Path(__file__).resolve().parent / "text-embeder/my_book_index_aero")


def _apply_subject(subject_arg: str | None) -> None:
    if subject_arg:
        set_subject_name(subject_arg, "cli")
    else:
        env_subject = os.getenv("TEXTBOOK_SUBJECT")
        if env_subject:
            set_subject_name(env_subject, "env")


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
    _apply_subject(getattr(args, "subject", None))
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
    orch = Orchestrator()
    opts = {
        "doc_sets": indexes,
        "k_sem": args.k_sem,
        "k_lex": args.k_lex,
        "token_budget": args.token_budget,
    }
    bundle = orch._iterative_research(args.question, opts, args.max_iters)
    ctx_snippets = [
        ContextSnippet(
            id=sn.id,
            type=sn.type,
            page=sn.page,
            section_path=sn.section_path,
            text=sn.text,
            figure_id=sn.figure_id,
            why=sn.why,
            source_path=sn.source_path,
            doc_title=sn.doc_title,
            doc_short=sn.doc_short,
        )
        for sn in bundle.snippets
    ]
    ctx = ContextPack(snippets=ctx_snippets, used_ids=bundle.used_ids, stats=bundle.stats)
    ans = answer(args.question, ctx)
    print("=== Answer ===\n" + ans.text + "\n")
    print("Citations:", render_citations(ans))
    proof = {
        "question": args.question,
        "used_ids": bundle.used_ids,
        "snippets": [asdict(sn) for sn in bundle.snippets],
        "allowed_markers": list(getattr(bundle, "allowed_markers", [])),
        "subject": getattr(bundle, "subject", getattr(bundle.metadata, "subject", "")),
        "not_found_terms": list(
            getattr(bundle, "not_found_terms", getattr(bundle.metadata, "not_found_terms", []))
        ),
        "attempted_terms": list(
            getattr(bundle, "attempted_terms", getattr(bundle.metadata, "attempted_terms", []))
        ),
    }
    with open("proof.json", "w", encoding="utf-8") as f:
        json.dump(proof, f, ensure_ascii=False, indent=2)
    try:
        allowed_markers = list(proof.get("allowed_markers", []))
        used_markers = [
            c.get("marker")
            for c in ans.citations
            if isinstance(c, dict) and isinstance(c.get("marker"), str)
        ]
        if allowed_markers or used_markers:
            _write_proof_citations(bundle, allowed_markers, used_markers)
        if allowed_markers or used_markers:
            _write_citations_file(bundle, allowed_markers or used_markers, used_markers)
        else:
            snippet_markers = [
                getattr(sn, "citation_marker", "")
                for sn in bundle.snippets
                if getattr(sn, "citation_marker", None)
            ]
            _write_citations_file(bundle, snippet_markers, used_markers)
    except Exception:
        pass


def cmd_solve(args: argparse.Namespace) -> None:
    _apply_subject(getattr(args, "subject", None))
    orch = Orchestrator()
    task = {
        "user_query": args.question,
        "doc_sets": args.index or [DEFAULT_INDEX],
        "k_sem": args.k_sem,
        "k_lex": args.k_lex,
        "token_budget": args.token_budget,
        "max_iters": args.max_iters,
    }
    try:
        final = orch.run(task)
    except RuntimeError as exc:
        print(f"{exc}")
        return
    print(final.text)


def cmd_research(args: argparse.Namespace) -> None:
    indexes = args.index or [DEFAULT_INDEX]
    _apply_subject(getattr(args, "subject", None))
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
    orch = Orchestrator()
    opts = {
        "doc_sets": indexes,
        "k_sem": args.k_sem,
        "k_lex": args.k_lex,
        "token_budget": args.token_budget,
    }
    bundle = orch._iterative_research(args.query, opts, args.max_iters)
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
    p_ask.add_argument("--k-sem", type=int, default=30, help="Semantic top-K")
    p_ask.add_argument("--k-lex", type=int, default=30, help="Lexical top-K")
    p_ask.add_argument(
        "--token-budget", type=int, default=6000, help="Token budget for context packing"
    )
    p_ask.add_argument(
        "--max-iters", type=int, default=5, help="Maximum keyword lookup iterations"
    )
    p_ask.add_argument("--subject", help="Override subject for prompts")
    p_ask.set_defaults(func=cmd_ask)

    p_solve = sub.add_parser("solve", help="Closed-book solve with citations")
    p_solve.add_argument("question")
    p_solve.add_argument("--index", action="append", help="Path to index; may be used multiple times")
    p_solve.add_argument("--k-sem", type=int, default=30, help="Semantic top-K")
    p_solve.add_argument("--k-lex", type=int, default=30, help="Lexical top-K")
    p_solve.add_argument("--token-budget", type=int, default=6000, help="Token budget for context packing")
    p_solve.add_argument(
        "--max-iters", type=int, default=5, help="Maximum keyword lookup iterations"
    )
    p_solve.add_argument("--subject", help="Override subject for prompts")
    p_solve.set_defaults(func=cmd_solve)

    p_research = sub.add_parser("research", help="Retrieve only and print bundle")
    p_research.add_argument("query")
    p_research.add_argument("--index", action="append", help="Path to index; may be used multiple times")
    p_research.add_argument("--k-sem", type=int, default=30, help="Semantic top-K")
    p_research.add_argument("--k-lex", type=int, default=30, help="Lexical top-K")
    p_research.add_argument("--token-budget", type=int, default=6000, help="Token budget for context packing")
    p_research.add_argument(
        "--max-iters", type=int, default=5, help="Maximum keyword lookup iterations"
    )
    p_research.add_argument("--full", action="store_true", help="Dump full bundle")
    p_research.add_argument("--subject", help="Override subject for prompts")
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
