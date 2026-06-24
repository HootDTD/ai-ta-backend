"""Idempotent course setup for the macro graph-grading probe.

Creates a DEDICATED macroeconomics SearchSpace (so the test stays isolated from
the fluids smoke courses) and, once the macro subject is seeded, pins
apollo_subjects.search_space_id to it (overriding the registry seeder's
MIN(aita_search_spaces.id) backfill).

Run order:
  1. before embedding/seeding:  creates the space, prints MACRO_SPACE_ID
  2. after the registry seed:    re-run to align apollo_subjects.search_space_id

LOCAL only. Run from ai-ta-backend/, project venv.
    .venv/Scripts/python.exe scripts/_macro_setup_course.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

COURSE_SLUG = os.getenv("MACRO_COURSE_SLUG", "macro-econ")
COURSE_NAME = os.getenv("MACRO_COURSE_NAME", "Macroeconomics 101")
SUBJECT_NAME = os.getenv("MACRO_SUBJECT_NAME", "Macroeconomics")
SUBJECT_SLUG = "macroeconomics"  # the on-disk apollo/subjects/<slug>

DB_URL = os.environ.get("SUPABASE_DB_URL", "")
if not DB_URL:
    sys.exit("SUPABASE_DB_URL not set — check .env / .env.local")
_target = DB_URL.split("@")[-1]
if "127.0.0.1" not in _target and "localhost" not in _target:
    sys.exit(f"REFUSING: DB target {_target!r} is not local")


async def setup() -> int:
    from apollo.persistence.models import Subject
    from database.models import SearchSpace

    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            space = (
                await session.execute(select(SearchSpace).where(SearchSpace.slug == COURSE_SLUG))
            ).scalar_one_or_none()
            if space is None:
                space = SearchSpace(name=COURSE_NAME, slug=COURSE_SLUG, subject_name=SUBJECT_NAME)
                session.add(space)
                await session.flush()
                print(f"  created course {COURSE_SLUG!r} (search_space_id={space.id})")
            else:
                print(f"  course {COURSE_SLUG!r} already exists (search_space_id={space.id})")
            space_id = space.id

            # Align the macro subject to this course if it's been seeded.
            subj = (
                await session.execute(select(Subject).where(Subject.slug == SUBJECT_SLUG))
            ).scalar_one_or_none()
            if subj is None:
                print(f"  subject {SUBJECT_SLUG!r} not seeded yet — re-run after the registry seed")
            elif subj.search_space_id != space_id:
                await session.execute(
                    update(Subject).where(Subject.slug == SUBJECT_SLUG).values(search_space_id=space_id)
                )
                print(f"  aligned subject {SUBJECT_SLUG!r}.search_space_id -> {space_id}")
            else:
                print(f"  subject {SUBJECT_SLUG!r} already aligned to {space_id}")

            await session.commit()
            print(f"\nMACRO_SPACE_ID={space_id}")
            return space_id
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(setup()) else 1)
