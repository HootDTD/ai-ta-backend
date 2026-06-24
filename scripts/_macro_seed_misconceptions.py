"""Seed the runtime misconception bank (apollo_misconceptions) for the macro
concepts, from each concept's authored misconceptions.json.

WHY: the graph-grading Done chain loads misconceptions via
``apollo.overseer.misconception_bank.load_for_concept`` — which reads the
``apollo_misconceptions`` TABLE, NOT the ``apollo_kg_entities`` rows the
learner-model seeder mints. So without this, the macro weak variations have no
misconception candidates to resolve to, and the soundness/contradiction
dimension is vacuously 1.0. ``description_embedding`` is left NULL
(load_for_concept doesn't use it; only the embedding-retrieval channel does).

Idempotent (skips an existing (concept_id, code)). LOCAL only. Run from
ai-ta-backend/, project venv:
    .venv/Scripts/python.exe scripts/_macro_seed_misconceptions.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

SUBJECT_SLUG = "macroeconomics"
CONCEPTS_DIR = ROOT / "apollo" / "subjects" / SUBJECT_SLUG / "concepts"

DB_URL = os.environ.get("SUPABASE_DB_URL", "")
if not DB_URL:
    sys.exit("SUPABASE_DB_URL not set — check .env / .env.local")
_target = DB_URL.split("@")[-1]
if "127.0.0.1" not in _target and "localhost" not in _target:
    sys.exit(f"REFUSING: DB target {_target!r} is not local")


async def seed() -> int:
    from apollo.persistence.models import Concept, Misconception, Subject

    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    inserted = 0
    try:
        async with factory() as s:
            subj = (await s.execute(
                select(Subject).where(Subject.slug == SUBJECT_SLUG)
            )).scalar_one_or_none()
            if subj is None:
                sys.exit(f"no '{SUBJECT_SLUG}' subject — run the registry seed first")

            for concept_dir in sorted(p for p in CONCEPTS_DIR.iterdir() if p.is_dir()):
                misc_file = concept_dir / "misconceptions.json"
                if not misc_file.is_file():
                    continue
                concept = (await s.execute(
                    select(Concept).where(
                        Concept.subject_id == subj.id, Concept.slug == concept_dir.name
                    )
                )).scalar_one_or_none()
                if concept is None:
                    print(f"  skip {concept_dir.name}: concept not seeded")
                    continue
                data = json.loads(misc_file.read_text(encoding="utf-8"))
                for entry in data.get("misconceptions", []):
                    code = entry["key"]  # 'misc.<name>' — the load-bearing prefix
                    exists = (await s.execute(
                        select(Misconception.id).where(
                            Misconception.concept_id == concept.id,
                            Misconception.code == code,
                        )
                    )).scalar_one_or_none()
                    if exists is not None:
                        print(f"  {concept_dir.name}/{code}: already present")
                        continue
                    s.add(Misconception(
                        concept_id=concept.id,
                        code=code,
                        description=entry.get("description", entry.get("display_name", code)),
                        trigger_phrases=list(entry.get("trigger_phrases", [])),
                        probe_question=entry.get("probe_question", ""),
                        rt_steps=[],
                    ))
                    inserted += 1
                    print(f"  + {concept_dir.name}/{code} "
                          f"({len(entry.get('trigger_phrases', []))} triggers)")
            await s.commit()
    finally:
        await engine.dispose()
    print(f"\nseeded {inserted} macro misconceptions into apollo_misconceptions")
    return inserted


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(seed()) is not None else 1)
