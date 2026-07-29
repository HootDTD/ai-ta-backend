"""Migration 002 — Seed SearchSpace + Document shells from existing Supabase records.

This is a one-time script that reads the existing knowledge_subjects and
knowledge_stores tables from Supabase REST API and creates corresponding
aita_search_spaces and aita_documents (shell records, status=pending).

Chunks are NOT created here — run 003_reindex_existing.py after this to
embed all items and populate aita_chunks.

Run:
    python -m backend.migrations.002_seed_from_supabase

Requirements:
  - SUPABASE_URL + SUPABASE_API_KEY (for REST reads)
  - SUPABASE_DB_URL (for asyncpg writes)
"""

raise SystemExit(
    "Legacy Python migrations are retired. Use the timestamped Supabase chain via "
    "`node scripts/db/reset-local.mjs`; never run database/migrations/*.py."
)  # pragma: no cover - intentional command-line guard

import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = PROJECT_ROOT / ".env"


def _load_env_file() -> None:
    """Load .env with fallback when python-dotenv is not installed."""
    try:
        from dotenv import load_dotenv

        # Local .env should win over stale shell vars for one-off migration scripts.
        load_dotenv(ENV_PATH, override=True)
        return
    except ImportError:
        pass

    if not ENV_PATH.exists():
        return

    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip("'").strip('"')


_load_env_file()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import supabase_client as sb

DATABASE_URL = os.environ["SUPABASE_DB_URL"]


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def _content_hash(search_space_id: int, source: str) -> str:
    return hashlib.sha256(f"{search_space_id}:{source}".encode()).hexdigest()


def _unique_id_hash(doc_type: str, unique_id: str, search_space_id: int) -> str:
    return hashlib.sha256(f"{doc_type}:{unique_id}:{search_space_id}".encode()).hexdigest()


async def run():
    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # 1. Load existing subjects from Supabase
    try:
        subjects = sb.select("knowledge_subjects")
    except Exception as e:
        print(f"Could not read knowledge_subjects from Supabase: {e}")
        print("Skipping - you can run this script again once Supabase is configured.")
        await engine.dispose()
        return

    if not subjects:
        print("No subjects found in Supabase knowledge_subjects. Nothing to seed.")
        await engine.dispose()
        return

    async with session_factory() as session:
        for subj in subjects:
            subj_id = subj.get("id")
            name = subj.get("name") or subj.get("subject") or str(subj_id)
            slug = subj.get("slug") or _slugify(name)
            subject_name = subj.get("subject") or name

            # Check if SearchSpace already exists
            result = await session.execute(
                text("SELECT id FROM aita_search_spaces WHERE slug = :slug"),
                {"slug": slug},
            )
            existing = result.fetchone()
            if existing:
                space_db_id = existing[0]
                print(f"  SearchSpace '{name}' already exists (id={space_db_id}), skipping.")
            else:
                result = await session.execute(
                    text("""
                        INSERT INTO aita_search_spaces (name, slug, subject_name, weight_overrides, metadata)
                        VALUES (:name, :slug, :subject_name, :weight_overrides, :metadata)
                        RETURNING id
                    """),
                    {
                        "name": name,
                        "slug": slug,
                        "subject_name": subject_name,
                        "weight_overrides": json.dumps({}),
                        "metadata": json.dumps({"legacy_supabase_id": str(subj_id)}),
                    },
                )
                space_db_id = result.fetchone()[0]
                print(f"  Created SearchSpace '{name}' (id={space_db_id})")

            # 2. Load knowledge_stores for this subject
            try:
                stores = sb.select(
                    "knowledge_stores",
                    {"subject_id": f"eq.{subj_id}", "order": "priority.desc"},
                )
            except Exception as e:
                print(f"    Could not read stores for subject {subj_id}: {e}")
                continue

            for store in stores:
                store_id = store.get("id")
                title = store.get("title") or f"Material {store_id}"
                kind = store.get("kind") or "other"
                index_path = store.get("index_path") or ""
                priority = store.get("priority") or 50

                unique_id = str(index_path) or str(store_id)
                uid_hash = _unique_id_hash("EDUCATIONAL_FILE", unique_id, space_db_id)
                # Use a placeholder content hash - real content loaded in migration 003
                c_hash = _content_hash(space_db_id, unique_id)

                # Check duplicate
                result = await session.execute(
                    text("SELECT id FROM aita_documents WHERE unique_identifier_hash = :h"),
                    {"h": uid_hash},
                )
                if result.fetchone():
                    print(f"    Document '{title}' already exists, skipping.")
                    continue

                await session.execute(
                    text("""
                        INSERT INTO aita_documents
                            (title, document_type, material_kind, content, source_markdown,
                             content_hash, unique_identifier_hash, document_metadata,
                             status, search_space_id)
                        VALUES
                            (:title, 'EDUCATIONAL_FILE', :kind, '', NULL,
                             :c_hash, :uid_hash, :meta,
                             '{"state": "pending"}'::jsonb, :space_id)
                    """),
                    {
                        "title": title,
                        "kind": kind,
                        "c_hash": c_hash,
                        "uid_hash": uid_hash,
                        "meta": json.dumps({
                            "legacy_store_id": str(store_id),
                            "index_path": str(index_path),
                            "priority": priority,
                        }),
                        "space_id": space_db_id,
                    },
                )
                print(f"    Created Document shell '{title}' (kind={kind})")

        await session.commit()

    await engine.dispose()
    print("Seed migration complete. Run 003_reindex_existing.py to populate chunks.")


if __name__ == "__main__":
    asyncio.run(run())
