"""Recall eval for the HNSW iterative-scan semantic arm (retrieval-tuning gate).

Compares the FINAL fused top-k of hybrid_search() between:
  * exact   — a TRUE brute-force scan (planner forced off the HNSW index via
              ``enable_indexscan/enable_bitmapscan = off``), giving the exact
              top-k by cosine distance.
  * relaxed — the production config (HNSW index under
              ``hnsw.iterative_scan = relaxed_order`` + ef_search), which is
              approximate.

NOTE: comparing against ``HNSW_ITERATIVE_SCAN=off`` is NOT a valid baseline —
once the semantic arm filters by a chunk-local ``document_id = ANY(array)`` the
planner engages the HNSW index even with the GUC off (just at the default
ef_search), so "off" is itself approximate. We force brute force by disabling
index scans for the exact arm instead.

Per the retrieval-tuning skill: >=3 query types (factual, conceptual,
equation-based), top-k chunk overlap, before/after scores logged.

Usage:
    python scripts/eval_iterative_scan_recall.py [--sid SEARCH_SPACE_ID]
        [--ef 300] [--top 20]

Reads SUPABASE_DB_URL (and OPENAI key for query embeddings) from .env.
The query embedding is computed once per query and reused for both arms so
the comparison is exact-same-vector.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

for line in (REPO / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("recall_eval")

QUERIES = {
    "factual": [
        "What is the speed of sound in air at standard conditions?",
        "What happens to stagnation pressure across a normal shock?",
        "What is the definition of Mach number?",
    ],
    "conceptual": [
        "Why does temperature increase across a shock wave?",
        "Explain the difference between subsonic and supersonic flow behavior in a converging nozzle.",
        "How does an oblique shock differ from a normal shock?",
    ],
    "equation": [
        "Derive the relation between stagnation temperature and static temperature for isentropic flow.",
        "What equation relates downstream Mach number to upstream Mach number across a normal shock?",
        "How is the area-Mach number relation used to size a nozzle throat?",
    ],
}


async def _pick_largest_sid(session) -> int:
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                "SELECT d.search_space_id, count(*) AS n FROM aita_chunks c "
                "JOIN aita_documents d ON c.document_id = d.id "
                "GROUP BY d.search_space_id ORDER BY n DESC LIMIT 1"
            )
        )
    ).first()
    log.info("auto-selected search_space_id=%s (%s chunks)", row[0], row[1])
    return int(row[0])


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", type=int, default=None)
    ap.add_argument("--ef", type=int, default=300)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    import retrieval.hybrid_search as hs
    from database.session import get_async_session

    # One embedding per query text, shared by both arms.
    raw_embed = hs.embed_text
    cache: dict[str, list[float]] = {}

    def cached_embed(q: str):
        if q not in cache:
            cache[q] = raw_embed(q)
        return cache[q]

    hs.embed_text = cached_embed

    os.environ["HNSW_EF_SEARCH"] = str(args.ef)
    worst = 1.0
    rows = []

    # The two arms swap what _iterative_scan_statements() emits:
    #   exact   -> force the planner OFF every index (true brute-force scan)
    #   relaxed -> the real production SET LOCALs (HNSW + iterative_scan)
    real_statements = hs._iterative_scan_statements
    arm_statements = {
        "exact": lambda: [
            "SET LOCAL enable_indexscan = off",
            "SET LOCAL enable_bitmapscan = off",
        ],
        "relaxed": lambda: (
            os.environ.__setitem__("HNSW_ITERATIVE_SCAN", "relaxed_order")
            or real_statements()
        ),
    }

    async with get_async_session() as session:
        sid = args.sid or await _pick_largest_sid(session)

        for qtype, queries in QUERIES.items():
            for q in queries:
                per_arm: dict[str, tuple[list[int], dict[int, float], float]] = {}
                for arm in ("exact", "relaxed"):
                    hs._iterative_scan_statements = arm_statements[arm]
                    try:
                        retriever = hs.AITAHybridSearchRetriever(session, sid)
                        t0 = time.perf_counter()
                        out = await retriever.hybrid_search(q)
                        dt = time.perf_counter() - t0
                    finally:
                        hs._iterative_scan_statements = real_statements
                    ids = [c["chunk_id"] for c in out[: args.top]]
                    scores = {c["chunk_id"]: c["score"] for c in out[: args.top]}
                    per_arm[arm] = (ids, scores, dt)

                e_ids, e_scores, e_dt = per_arm["exact"]
                r_ids, r_scores, r_dt = per_arm["relaxed"]
                inter = set(e_ids) & set(r_ids)
                overlap = len(inter) / max(1, len(e_ids))
                worst = min(worst, overlap)
                rows.append((qtype, q, overlap, e_dt, r_dt))
                log.info(
                    "[%s] overlap@%d=%.2f exact=%.2fs relaxed=%.2fs  %r",
                    qtype, args.top, overlap, e_dt, r_dt, q[:60],
                )
                missing = [i for i in e_ids if i not in inter]
                if missing:
                    log.info(
                        "  missing from relaxed (exact rank, chunk_id, rrf score): %s",
                        [(e_ids.index(m) + 1, m, round(e_scores[m], 5)) for m in missing],
                    )

    log.info("\n=== summary (sid=%s, ef_search=%d, top=%d) ===", sid, args.ef, args.top)
    for qtype in QUERIES:
        sub = [r for r in rows if r[0] == qtype]
        log.info(
            "%-11s mean_overlap=%.3f  mean_exact=%.2fs  mean_relaxed=%.2fs",
            qtype,
            sum(r[2] for r in sub) / len(sub),
            sum(r[3] for r in sub) / len(sub),
            sum(r[4] for r in sub) / len(sub),
        )
    log.info("worst-case overlap: %.3f", worst)
    ok = worst >= 0.95
    log.info("VERDICT: %s (gate: worst overlap >= 0.95)", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
