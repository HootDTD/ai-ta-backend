"""Standalone diagnostic script for inspecting the retrieval pipeline.

Runs a question through the SAME pipeline the server uses (Orchestrator →
solve_with_bundle → format_answer), captures data at key points, and writes
a JSON report:

  1. Keywords extracted by the orchestrator and which were found/not-found
  2. The bundle snippets selected by the orchestrator, each with its
     per-snippet mini-summary from the scoring AI
  3. The final assembled answer with structured citations

Usage:
    python -m tests.diagnostic_retrieval \
        --question "what is boundary layer thickness" \
        --subject "Fluid Mechanics" \
        --output tests/diagnostic_output.json
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
from contextlib import redirect_stdout
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Bootstrap: ensure repo root is on sys.path and .env is loaded
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / ".env")
except ImportError:
    pass  # .env loading is optional

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("diagnostic")
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Imports from the backend pipeline (same as server.py)
# ---------------------------------------------------------------------------
from backend.retriever import (  # noqa: E402
    RetrievalContext,
    load_assets,
    load_assets_all,
)
from backend.knowledge import KnowledgeManager  # noqa: E402
from backend.config import RequestConfig  # noqa: E402
from backend.orchestrator import Orchestrator  # noqa: E402
from backend.contracts import ParsedTask, ResearchBundle  # noqa: E402
from backend.main_ai import (  # noqa: E402
    parse_question,
    solve_with_bundle,
    format_answer,
)


def _text_preview(text: str, max_len: int = 300) -> str:
    """Return a truncated preview of a text string."""
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[:max_len] + "..."


def _safe_serialise(obj: Any) -> Any:
    """Make dataclass / set objects JSON-serialisable."""
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    return str(obj)


def run_diagnostic(question: str, subject: str, output_path: str) -> None:
    """Execute the full server pipeline and write the JSON report."""

    log.info("Question : %s", question)
    log.info("Subject  : %s", subject)
    log.info("Output   : %s", output_path)

    # -----------------------------------------------------------------------
    # Step 1: Build per-request config & load assets (mirrors server.py:817-834)
    # -----------------------------------------------------------------------
    log.info("Loading knowledge assets...")
    cfg = RequestConfig.from_env()
    cfg.set_subject(subject, "diagnostic")

    km = KnowledgeManager()
    doc_sets = km.resolve_doc_sets(subject)
    if not doc_sets:
        log.error("No doc_sets found for subject %r. Check KNOWLEDGE_BASE_DIR.", subject)
        sys.exit(1)
    log.info("Doc sets: %s", [str(p) for p in doc_sets])

    retrieval_opts: Dict[str, Any] = {
        "doc_sets": [str(p) for p in doc_sets],
        "k_sem": int(os.getenv("K_SEM", "30")),
        "k_lex": int(os.getenv("K_LEX", "30")),
        "token_budget": int(os.getenv("TOKEN_BUDGET", "6000")),
    }

    rctx = RetrievalContext(flags=retrieval_opts)
    paths = [Path(p).resolve() for p in doc_sets]
    if len(paths) > 1:
        load_assets_all(paths, ctx=rctx)
    elif paths:
        load_assets(paths[0], ctx=rctx)
    log.info("Assets loaded. Items: %d", len(rctx.items_df) if rctx.items_df is not None else 0)

    # -----------------------------------------------------------------------
    # Step 2: Run Orchestrator._iterative_research (mirrors server.py:837-844)
    #         This is the keyword extraction → batch_lookup_terms path.
    # -----------------------------------------------------------------------
    log.info("Running orchestrator keyword pipeline...")
    stdout_buffer = io.StringIO()
    with redirect_stdout(stdout_buffer):
        orch = Orchestrator(ctx=rctx, cfg=cfg)
        bundle: ResearchBundle = orch._iterative_research(question, retrieval_opts)

    wire_logs = [
        line.strip()
        for line in stdout_buffer.getvalue().splitlines()
        if line.strip().startswith("[Main AI") or line.strip().startswith("[Indexer AI")
    ]

    log.info("Bundle snippets: %d", len(bundle.snippets))
    log.info("Found terms: %s", bundle.found_terms)
    log.info("Not-found terms: %s", bundle.not_found_terms)
    log.info("Attempted terms: %s", bundle.attempted_terms)

    # -----------------------------------------------------------------------
    # Step 3: Capture the bundle snippets (section 1 of the report)
    # -----------------------------------------------------------------------
    section_1_snippets: List[Dict[str, Any]] = []
    for sn in bundle.snippets:
        section_1_snippets.append({
            "id": sn.id,
            "page": sn.page,
            "type": sn.type,
            "section": sn.section_path,
            "doc_short": sn.doc_short,
            "text_preview": _text_preview(sn.text, 300),
            "why": sn.why,
            "citation_marker": sn.citation_marker,
            "final_score": sn.final_score,
            "concept_terms": getattr(sn, "concept_terms", []),
        })

    # -----------------------------------------------------------------------
    # Step 4: Parse question (mirrors server.py:846-861)
    # -----------------------------------------------------------------------
    log.info("Parsing question...")
    parsed_task: Optional[ParsedTask] = None
    if question.strip():
        try:
            parsed_task = parse_question(question, subject=cfg.subject_name)
        except Exception:
            log.error("Question parsing failed", exc_info=True)
            parsed_task = None
    if parsed_task is None:
        fallback_problem = question.strip() or "Question"
        parsed_task = ParsedTask(
            problem_type=fallback_problem,
            asked_outputs=["answer"],
            asked_output_keys=["answer"],
        )
    log.info("Parsed task type: %s", parsed_task.problem_type)

    # -----------------------------------------------------------------------
    # Step 5: solve_with_bundle — this runs per-snippet mini-summaries AND
    #         assembles the final answer (mirrors server.py:863-865)
    # -----------------------------------------------------------------------
    log.info("Running solve_with_bundle (per-snippet scoring + final answer)...")
    solution = solve_with_bundle(parsed_task, bundle, subject=cfg.subject_name)

    # Extract per-snippet scoring results from bundle.provenance
    citation_rankings = bundle.provenance.get("citation_rankings", [])

    # Attach mini-summary scores back to section_1 snippets
    rankings_by_id: Dict[str, Dict[str, Any]] = {}
    for ranking in citation_rankings:
        sid = ranking.get("snippet_id", "")
        if sid:
            rankings_by_id[sid] = ranking

    for entry in section_1_snippets:
        ranking = rankings_by_id.get(entry["id"], {})
        entry["mini_summary"] = {
            "relevance": ranking.get("relevance"),
            "directness": ranking.get("directness"),
            "base_score": ranking.get("base_score"),
            "score": ranking.get("score"),
            "context": ranking.get("context", ""),
            "why": ranking.get("why", ""),
            "concept_term": ranking.get("concept_term", ""),
            "importance": ranking.get("importance"),
        } if ranking else None

    # Sort by mini-summary score descending
    section_1_snippets.sort(
        key=lambda x: -(
            (x.get("mini_summary") or {}).get("score") or 0
        )
    )

    # -----------------------------------------------------------------------
    # Step 6: format_answer (mirrors server.py:866-873)
    # -----------------------------------------------------------------------
    log.info("Formatting final answer...")
    final = format_answer(
        solution, bundle, include_background=False,
        subject=cfg.subject_name,
    )
    final_text = final.text
    final_citations = final.citations
    log.info("Answer generated (%d chars).", len(final_text))

    # -----------------------------------------------------------------------
    # Step 7: Write the diagnostic JSON
    # -----------------------------------------------------------------------
    metadata_dict = asdict(bundle.metadata)

    # Extract keyword iteration trace for diagnostics
    iteration_trace = metadata_dict.get("iteration_trace") or metadata_dict.get("keyword_iterations") or []

    report = {
        "question": question,
        "subject": subject,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "orchestrator_diagnostics": {
            "attempted_terms": bundle.attempted_terms,
            "found_terms": bundle.found_terms,
            "not_found_terms": bundle.not_found_terms,
            "allowed_markers": bundle.allowed_markers,
            "stats": bundle.stats,
            "iteration_trace": iteration_trace,
            "wire_logs": wire_logs,
        },
        "parsed_task": asdict(parsed_task),
        "section_1_bundle_snippets": section_1_snippets,
        "section_2_final_answer": {
            "text": final_text,
            "citations_used": final_citations,
            "solution_steps": solution.steps if hasattr(solution, "steps") else "",
            "equations_used": solution.equations_used if hasattr(solution, "equations_used") else [],
            "assumptions": solution.assumptions if hasattr(solution, "assumptions") else [],
        },
    }

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=_safe_serialise)
    log.info("Report written to %s", out)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnose retrieval pipeline for a given question."
    )
    parser.add_argument(
        "--question", "-q", required=True,
        help="The question to run through the pipeline.",
    )
    parser.add_argument(
        "--subject", "-s", default="Fluid Mechanics",
        help="Subject name (default: 'Fluid Mechanics').",
    )
    parser.add_argument(
        "--output", "-o", default="tests/diagnostic_output.json",
        help="Output JSON file path (default: tests/diagnostic_output.json).",
    )
    args = parser.parse_args()
    run_diagnostic(args.question, args.subject, args.output)


if __name__ == "__main__":
    main()
