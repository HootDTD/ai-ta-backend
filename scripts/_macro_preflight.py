"""Read-only preflight for the macro graph-grading probe run.

Loads the LOCAL env (.env then .env.local override), refuses a non-local DB,
and reports the state the live run depends on: search spaces, whether the
Apollo + indexing tables exist, whether the macroeconomics subject is already
seeded, and whether any Ch.6 textbook chunks are already embedded.

Run (from ai-ta-backend/, project venv):
    .venv/Scripts/python.exe scripts/_macro_preflight.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

DB_URL = os.environ.get("SUPABASE_DB_URL", "")
if not DB_URL:
    sys.exit("SUPABASE_DB_URL not set — check .env / .env.local")
_target = DB_URL.split("@")[-1]
if "127.0.0.1" not in _target and "localhost" not in _target:
    sys.exit(f"REFUSING: DB target {_target!r} is not local")

_TABLES = [
    "aita_search_spaces", "aita_documents", "aita_chunks",
    "apollo_subjects", "apollo_concepts", "apollo_concept_problems",
    "apollo_kg_entities", "apollo_entity_prereqs", "apollo_misconceptions",
    "apollo_graph_comparison_runs",
]


async def main() -> int:
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    async with engine.connect() as conn:
        print(f"[preflight] local DB: {_target}\n")

        print("== tables (to_regclass) ==")
        for t in _TABLES:
            present = (await conn.execute(text("SELECT to_regclass(:t)"), {"t": t})).scalar()
            print(f"  {'OK ' if present else 'MISSING'} {t}")

        print("\n== aita_search_spaces ==")
        try:
            rows = (await conn.execute(text(
                "SELECT id, name, slug, subject_name FROM aita_search_spaces ORDER BY id"
            ))).all()
            for r in rows:
                print(f"  id={r.id}  slug={r.slug!r}  name={r.name!r}  subject={r.subject_name!r}")
            if not rows:
                print("  (none)")
        except Exception as e:  # noqa: BLE001
            print(f"  (error: {e})")

        print("\n== apollo subjects / concepts ==")
        try:
            rows = (await conn.execute(text(
                "SELECT s.slug AS subject, c.slug AS concept, c.id AS concept_id, "
                "s.search_space_id "
                "FROM apollo_subjects s LEFT JOIN apollo_concepts c ON c.subject_id = s.id "
                "ORDER BY s.slug, c.slug"
            ))).all()
            for r in rows:
                print(f"  subject={r.subject!r} concept={r.concept!r} "
                      f"concept_id={r.concept_id} search_space_id={r.search_space_id}")
            if not rows:
                print("  (none)")
        except Exception as e:  # noqa: BLE001
            print(f"  (error: {e})")

        print("\n== macro problems in question bank ==")
        try:
            rows = (await conn.execute(text(
                "SELECT p.problem_code, p.difficulty, p.tier, p.quarantined_at "
                "FROM apollo_concept_problems p "
                "JOIN apollo_concepts c ON c.id = p.concept_id "
                "JOIN apollo_subjects s ON s.id = c.subject_id "
                "WHERE s.slug = 'macroeconomics' ORDER BY p.problem_code"
            ))).all()
            for r in rows:
                print(f"  {r.problem_code}  difficulty={r.difficulty} tier={r.tier} "
                      f"quarantined={r.quarantined_at}")
            if not rows:
                print("  (none — macro not seeded yet)")
        except Exception as e:  # noqa: BLE001
            print(f"  (error: {e})")

        print("\n== embedded documents (corpus) ==")
        try:
            rows = (await conn.execute(text(
                "SELECT id, title, material_kind, page_count, search_space_id, "
                "status->>'state' AS state FROM aita_documents ORDER BY id DESC LIMIT 20"
            ))).all()
            for r in rows:
                cnt = (await conn.execute(text(
                    "SELECT count(*) FROM aita_chunks WHERE document_id = :d"
                ), {"d": r.id})).scalar()
                print(f"  doc={r.id} kind={r.material_kind} state={r.state} "
                      f"space={r.search_space_id} chunks={cnt} title={r.title!r}")
            if not rows:
                print("  (none embedded)")
        except Exception as e:  # noqa: BLE001
            print(f"  (error: {e})")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
