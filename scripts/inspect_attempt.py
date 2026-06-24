"""Read-only diagnosis of one attempt's resolution + graph-sim findings.

Answers: did the student's (pressure-cancelled) Bernoulli resolve to eq.bernoulli?
where does the USES edge point, and did BOTH endpoints resolve? what findings did
grade_attempt emit (matched_edge vs missing_edge vs unresolved)?

Run (from repo root or ai-ta-backend):
    .venv/Scripts/python.exe scripts/inspect_attempt.py 8      # strong
    .venv/Scripts/python.exe scripts/inspect_attempt.py 9      # weak

LOCAL only (127.0.0.1 Postgres :54322 / Neo4j :7687). Reads, never writes.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from neo4j import GraphDatabase  # noqa: E402

AID = int(sys.argv[1]) if len(sys.argv) > 1 else 8
DB_URL = os.getenv("SUPABASE_DB_URL", "")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "local_password123")


def neo_dump(aid: int) -> None:
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    with drv.session() as s:
        print(f"\n=== NEO4J NODES (attempt {aid}) ===")
        q = (
            "MATCH (n:_KGNode {attempt_id:$aid}) "
            "RETURN [l IN labels(n) WHERE l<>'_KGNode'][0] AS kind, n.node_id AS id, "
            "n.resolution AS res, n.resolved_key AS key, n.resolution_method AS method, "
            "coalesce(n.symbolic, n.applies_when, n.action, n.concept, n.term, n.label, '') AS surface "
            "ORDER BY kind, id"
        )
        for r in s.run(q, aid=aid):
            print(
                f"  [{(r['kind'] or '?'):14s}] {r['id']}  res={r['res']} "
                f"key={r['key']!r} method={r['method']}"
            )
            print(f"        surface: {r['surface']!r}")
        print(f"\n=== NEO4J EDGES (attempt {aid}) ===")
        qe = (
            "MATCH (a:_KGNode {attempt_id:$aid})-[e]->(b:_KGNode {attempt_id:$aid}) "
            "RETURN type(e) AS t, a.node_id AS frm, a.resolved_key AS fk, a.resolution AS fr, "
            "b.node_id AS too, b.resolved_key AS tk, b.resolution AS tr"
        )
        for r in s.run(qe, aid=aid):
            print(
                f"  ({r['frm']} {r['fr']}/{r['fk']!r}) -{r['t']}-> "
                f"({r['too']} {r['tr']}/{r['tk']!r})"
            )
    drv.close()


async def pg_dump(aid: int) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    eng = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(eng, expire_on_commit=False)
    try:
        async with factory() as s:
            run = (
                await s.execute(
                    text(
                        "SELECT id, abstained, abstention_reasons, edge_coverage_score, "
                        "usage_score, node_coverage_score FROM apollo_graph_comparison_runs "
                        "WHERE attempt_id=:a ORDER BY id DESC LIMIT 1"
                    ),
                    {"a": aid},
                )
            ).mappings().first()
            if not run:
                print(f"\n(no comparison_run for attempt {aid})")
                return
            print(f"\n=== RUN (attempt {aid}) ===")
            print(
                f"  run_id={run['id']} abstained={run['abstained']} "
                f"reasons={run['abstention_reasons']}"
            )
            print(
                f"  edge_coverage={run['edge_coverage_score']} usage={run['usage_score']} "
                f"node_cov={run['node_coverage_score']}"
            )
            rows = (
                await s.execute(
                    text(
                        "SELECT finding_kind, student_node_ids, reference_node_ids, message "
                        "FROM apollo_graph_comparison_findings WHERE run_id=:r "
                        "ORDER BY finding_kind"
                    ),
                    {"r": run["id"]},
                )
            ).mappings().all()
            print(f"\n=== FINDINGS (run {run['id']}, {len(rows)} rows) ===")
            for r in rows:
                print(
                    f"  [{r['finding_kind']}] stu={r['student_node_ids']} "
                    f"ref={r['reference_node_ids']}"
                )
                if r["message"]:
                    print(f"        {r['message']}")
    finally:
        await eng.dispose()


if __name__ == "__main__":
    neo_dump(AID)
    asyncio.run(pg_dump(AID))
