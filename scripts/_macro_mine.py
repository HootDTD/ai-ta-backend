"""RAG relevance-pathway test for the macro Ch.6 graph-grading probe.

This is the ``_run_mining`` hook the orchestrator (run_macro_probe.py) delegates
to. It does NOT re-author questions (we hand-authored them per the hybrid
sourcing decision) — instead it exercises the production RAG **relevance
pathway** against the embedded Ch.6 corpus, two ways:

  1. Hybrid retrieval — for each authored macro question, run the SAME
     ``AITAHybridSearchRetriever.hybrid_search`` (pgvector halfvec + FTS + RRF)
     that production QA uses, and report the top retrieved spans + a relevance
     hit/miss (does the top material mention the question's concept?).
  2. Provisioning grounding adapter — run ``make_course_retrieve_fn`` (the exact
     closure auto-provisioning uses to ground/faithfulness-check a solution) for
     one question, proving it returns non-empty GroundingSpans now that a real
     corpus exists (the handoff's "empty corpus → judge rejects everything" is
     the failure this guards against).

Writes ``scripts/macro_mining_report.json`` and prints a summary. LOCAL only.
Run (from ai-ta-backend/, project venv) — usually via the orchestrator:
    .venv/Scripts/python.exe scripts/_macro_mine.py --search-space-id <id>
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

log = logging.getLogger("macro_mine")

# Per-question relevance probe: any of these phrases in the top retrieved spans
# means the relevance pathway surfaced on-topic Ch.6 material for that question.
_RELEVANCE_TERMS: dict[str, tuple[str, ...]] = {
    "gdp_identity": ("consumption", "investment", "government", "net export", "c + i + g"),
    "net_exports_sign": ("export", "import", "trade deficit", "trade balance", "trade surplus"),
    "nnp_chain": ("gross national product", "national product", "depreciation", "gnp", "nnp"),
    "real_gdp_from_deflator": ("deflator", "price index", "real gdp", "nominal gdp"),
    "real_gdp_growth": ("growth", "percentage change", "real gdp", "percent"),
}

REPORT_PATH = ROOT / "scripts" / "macro_mining_report.json"


def _db_url() -> str:
    url = os.environ.get("SUPABASE_DB_URL", "")
    if not url:
        sys.exit("SUPABASE_DB_URL not set — check .env / .env.local")
    target = url.split("@")[-1]
    if "127.0.0.1" not in target and "localhost" not in target:
        sys.exit(f"REFUSING: DB target {target!r} is not local")
    return url


def _load_questions() -> list[dict]:
    """Load (id, concept, problem_text) for every authored macro problem."""
    out: list[dict] = []
    pattern = str(ROOT / "apollo" / "subjects" / "macroeconomics" / "concepts" / "*" / "problems" / "problem_*.json")
    for f in sorted(glob.glob(pattern)):
        p = json.loads(Path(f).read_text(encoding="utf-8"))
        out.append({"id": p["id"], "concept": p["concept_id"], "problem_text": p["problem_text"]})
    return out


def _is_relevant(problem_id: str, spans_text: str) -> bool:
    terms = _RELEVANCE_TERMS.get(problem_id, ())
    low = spans_text.lower()
    return any(t in low for t in terms)


async def _resolve_space_id(conn, override: int | None) -> int:
    if override is not None:
        return override
    sid = (await conn.execute(text(
        "SELECT search_space_id FROM apollo_subjects WHERE slug = 'macroeconomics'"
    ))).scalar_one_or_none()
    if sid is None:
        sys.exit("no 'macroeconomics' subject — run the registry seed + _macro_setup_course.py first")
    return int(sid)


async def run(search_space_id: int | None) -> int:
    from apollo.provisioning.retrieval_adapter import make_course_retrieve_fn
    from retrieval.hybrid_search import AITAHybridSearchRetriever

    questions = _load_questions()
    if not questions:
        sys.exit("no authored macro problems found on disk")

    engine = create_async_engine(_db_url(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    report: dict = {"search_space_id": None, "retrieval": [], "grounding_adapter": None}
    relevant_hits = 0
    try:
        async with factory() as session:
            space_id = await _resolve_space_id(session, search_space_id)
            report["search_space_id"] = space_id
            log.info("RAG relevance test over course search_space_id=%s (%d questions)",
                     space_id, len(questions))

            retriever = AITAHybridSearchRetriever(session, space_id)
            for q in questions:
                rows = await retriever.hybrid_search(q["problem_text"], top_k=5)
                spans_text = " \n".join(str(r.get("content", "")) for r in rows)
                relevant = _is_relevant(q["id"], spans_text)
                relevant_hits += int(relevant)
                top = [
                    {"page": r.get("page_number"), "score": round(float(r.get("score", 0.0)), 4),
                     "snippet": str(r.get("content", ""))[:160].replace("\n", " ")}
                    for r in rows[:3]
                ]
                report["retrieval"].append(
                    {"problem_id": q["id"], "concept": q["concept"], "n_spans": len(rows),
                     "relevant": relevant, "top_spans": top}
                )
                log.info("  [%s] %d spans, relevant=%s", q["id"], len(rows), relevant)

            # Provisioning grounding adapter (the exact closure validate_pair uses).
            retrieve_fn = make_course_retrieve_fn(session, search_space_id=space_id)
            q0 = questions[0]
            spans = await retrieve_fn(SimpleNamespace(problem_text=q0["problem_text"]))
            report["grounding_adapter"] = {
                "problem_id": q0["id"], "n_grounding_spans": len(spans),
                "first_span": (str(spans[0].text)[:200] if spans else None),
            }
            log.info("  grounding_adapter[%s]: %d GroundingSpans", q0["id"], len(spans))
    finally:
        await engine.dispose()

    report["relevant_hits"] = relevant_hits
    report["total_questions"] = len(questions)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info("\n==== RAG RELEVANCE: %d/%d questions retrieved on-topic Ch.6 material ====",
             relevant_hits, len(questions))
    log.info("grounding adapter returned %s spans for %s",
             report["grounding_adapter"]["n_grounding_spans"], q0["id"])
    log.info("report -> %s", REPORT_PATH)
    # Non-fatal signal: warn (rc=0) so the orchestrator still proceeds to the probe.
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[macro-mine] %(message)s")
    parser = argparse.ArgumentParser(description="RAG relevance-pathway test for the macro probe.")
    parser.add_argument("--search-space-id", type=int, default=None,
                        help="macro course id (default: resolve from apollo_subjects)")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.search_space_id))


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
