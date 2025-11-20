"""Simple CLI for retrieval-based Q&A."""
import argparse
import json
import os
from pathlib import Path
from typing import Dict

from dataclasses import asdict

from .config import set_subject_name, get_subject_name
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
from .indexers.handwriting import ingest as ingest_handwriting, IngestOptions
from .knowledge import KnowledgeManager
from .main_ai import extract_keywords
from .core import _vision_transcribe
import base64
import re
import uuid


DATA_URL_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.DOTALL)

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
    hits, _ = search(args.query, raw_query=args.query)
    for h in hits[:12]:
        print(f"{h.id}\t{h.score_fused:.3f}\t{h.score_sem:.3f}\t{h.score_lex:.3f}")


def cmd_ask(args: argparse.Namespace) -> None:
    # Resolve full set of doc sets: prefer explicit --index; otherwise subject/workspace materials
    _apply_subject(getattr(args, "subject", None))
    indexes = args.index or None
    if not indexes:
        try:
            subject = get_subject_name()
        except Exception:
            subject = None
        if subject:
            try:
                km = KnowledgeManager()
                paths = km.resolve_doc_sets(subject)
            except Exception:
                paths = []
            if paths:
                indexes = [str(p) for p in paths]
    if not indexes:
        indexes = [DEFAULT_INDEX]
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
    # Decode optional attachments (data URLs or file paths) and augment question with image-derived keywords
    def _save_attachments(values: list[str] | None) -> list[str]:
        paths: list[str] = []
        if not values:
            return paths
        outdir = Path.cwd() / "tmp_uploads"
        outdir.mkdir(parents=True, exist_ok=True)
        for raw in values:
            if not raw:
                continue
            m = DATA_URL_RE.match(raw)
            if m:
                try:
                    b = base64.b64decode(m.group("data").encode("utf-8"), validate=True)
                except Exception:
                    b = base64.b64decode(m.group("data").encode("utf-8"))
                ext = ""
                mime = (m.group("mime") or "").lower()
                if "/" in mime:
                    ext = "." + mime.split("/")[-1].split(";")[0]
                fname = f"{uuid.uuid4().hex}_attach{ext}"
                path = outdir / fname
                path.write_bytes(b)
                paths.append(str(path))
                continue
            # treat as filesystem path
            p = Path(raw)
            if p.exists():
                paths.append(str(p.resolve()))
        return paths

    image_paths = _save_attachments(getattr(args, "attach", None))

    effective_question = args.question
    image_text = ""
    if image_paths:
        try:
            image_text = _vision_transcribe(image_paths) or ""
        except Exception:
            image_text = ""
    if image_text:
        try:
            image_context = extract_keywords(image_text) or ""
        except Exception:
            image_context = ""
        fallback_image_query = " ".join(image_text.split())[:500]
        image_query = image_context.strip() if image_context.strip() else fallback_image_query
        if effective_question and image_query:
            effective_question = effective_question.rstrip() + " \n" + image_query
        elif image_query:
            effective_question = image_query

    orch = Orchestrator()
    opts = {
        "doc_sets": indexes,
        "k_sem": args.k_sem,
        "k_lex": args.k_lex,
        "token_budget": args.token_budget,
    }
    bundle = orch._iterative_research(effective_question, opts)
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
    ans = answer(effective_question, ctx)
    print("=== Answer ===\n" + ans.text + "\n")
    print("Citations:", render_citations(ans))

    def _collect_snippet_citations() -> list[str]:
        entries: list[str] = []
        seen: set[tuple[str, str]] = set()
        for sn in bundle.snippets:
            marker = (getattr(sn, "citation_marker", "") or "").strip()
            if not marker:
                label = get_citation_label()
                page = getattr(sn, "page", None)
                page_val = f"{page}" if isinstance(page, int) and page > 0 else "?"
                marker = f"[{label}, p. {page_val}]"
            source = (
                (getattr(sn, "doc_short", "") or "").strip()
                or (getattr(sn, "doc_title", "") or "").strip()
                or (getattr(sn, "source_path", "") or "").strip()
            )
            if not source:
                source = getattr(sn, "id", "")
            key = (marker, source)
            if key in seen:
                continue
            seen.add(key)
            if source:
                entries.append(f"{marker} — {source}")
            else:
                entries.append(marker)
        return entries

    given_citations = _collect_snippet_citations()
    if given_citations:
        print("Given Citations:")
        for marker in given_citations:
            print(f"- {marker}")
        print()
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
    bundle = orch._iterative_research(args.query, opts)
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


def cmd_ingest_ocr(args: argparse.Namespace) -> None:
    """CLI entry: ingest a PDF into a target store and print JSON summary."""
    pdf_path = Path(args.pdf)
    out_dir = Path(args.out_dir)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}")
        return

    opts = IngestOptions(
        dpi=args.dpi,
        max_pages=args.max_pages,
        workers=max(1, int(args.workers or 4)),
        do_embed=not bool(getattr(args, "no_embed", False)),
    )

    try:
        items = ingest_handwriting(pdf_path, doc_id=pdf_path.stem, out_dir=out_dir, options=opts)
    except Exception as exc:
        print(f"ingestion failed: {exc}")
        return

    meta = {}
    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    item_count = 0
    items_path = out_dir / "items.jsonl"
    if items_path.exists():
        with items_path.open("r", encoding="utf-8") as fh:
            item_count = sum(1 for _ in fh)

    try:
        km = KnowledgeManager()
        store = km.register_store(
            args.subject,
            kind=args.kind,
            title=pdf_path.stem,
            index_path=out_dir,
        )
    except Exception:
        store = {
            "kind": args.kind,
            "title": pdf_path.stem,
            "index_path": str(out_dir.resolve()),
        }

    summary = {
        "subject": args.subject,
        "kind": store.get("kind", args.kind),
        "title": store.get("title", pdf_path.stem),
        "index_path": store.get("index_path", str(out_dir.resolve())),
        "items": item_count,
        "page_count": meta.get("page_count"),
        "average_confidence": meta.get("average_confidence"),
        "embeddings": bool((out_dir / "embeddings.npy").exists()),
        "faiss": bool((out_dir / "faiss.index").exists()),
        "sqlite": bool((out_dir / "sqlite.db").exists()),
    }
    print(json.dumps(summary, ensure_ascii=False))


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
    p_ask.add_argument(
        "--attach",
        action="append",
        help="Image attachment as file path or data URL; may be used multiple times",
    )
    p_ask.add_argument("--k-sem", type=int, default=30, help="Semantic top-K")
    p_ask.add_argument("--k-lex", type=int, default=30, help="Lexical top-K")
    p_ask.add_argument(
        "--token-budget", type=int, default=6000, help="Token budget for context packing"
    )
    p_ask.add_argument("--subject", help="Override subject for prompts")
    p_ask.set_defaults(func=cmd_ask)

    p_solve = sub.add_parser("solve", help="Closed-book solve with citations")
    p_solve.add_argument("question")
    p_solve.add_argument("--index", action="append", help="Path to index; may be used multiple times")
    p_solve.add_argument("--k-sem", type=int, default=30, help="Semantic top-K")
    p_solve.add_argument("--k-lex", type=int, default=30, help="Lexical top-K")
    p_solve.add_argument("--token-budget", type=int, default=6000, help="Token budget for context packing")
    p_solve.add_argument("--subject", help="Override subject for prompts")
    p_solve.set_defaults(func=cmd_solve)

    p_research = sub.add_parser("research", help="Retrieve only and print bundle")
    p_research.add_argument("query")
    p_research.add_argument("--index", action="append", help="Path to index; may be used multiple times")
    p_research.add_argument("--k-sem", type=int, default=30, help="Semantic top-K")
    p_research.add_argument("--k-lex", type=int, default=30, help="Lexical top-K")
    p_research.add_argument("--token-budget", type=int, default=6000, help="Token budget for context packing")
    p_research.add_argument("--full", action="store_true", help="Dump full bundle")
    p_research.add_argument("--subject", help="Override subject for prompts")
    p_research.set_defaults(func=cmd_research)

    p_ing = sub.add_parser("ingest-ocr", help="Render PDF pages, OCR, and build store artifacts")
    p_ing.add_argument("subject", help="Subject name for manifest registration")
    p_ing.add_argument("kind", help="Store kind: textbook|slides|homework|exams|other")
    p_ing.add_argument("pdf", help="Path to PDF to ingest")
    p_ing.add_argument("out_dir", help="Output directory for artifacts (store directory)")
    p_ing.add_argument("--dpi", type=int, default=None, help="Rendering DPI override (e.g., 200 or 300)")
    p_ing.add_argument("--max-pages", type=int, default=None, help="Optional page limit for ingestion")
    p_ing.add_argument("--no-embed", action="store_true", help="Skip embeddings/FAISS/SQLite (write items/meta only)")
    p_ing.add_argument("--workers", type=int, default=4, help="Parallel OCR workers")
    p_ing.set_defaults(func=cmd_ingest_ocr)

    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is required")

    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
