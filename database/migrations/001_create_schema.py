"""Migration 001 - Create AI-TA pgvector schema.

Run once against your Supabase/PostgreSQL database:

    python -m backend.migrations.001_create_schema

Requirements:
  - SUPABASE_DB_URL env var set (asyncpg connection string)
    e.g. postgresql+asyncpg://postgres:<password>@db.<project>.supabase.co:5432/postgres
  - pgvector extension already enabled in Supabase (it is by default)
"""

raise SystemExit(
    "Legacy Python migrations are retired. Use the timestamped Supabase chain via "
    "`node scripts/db/reset-local.mjs`; never run database/migrations/*.py."
)  # pragma: no cover - intentional command-line guard

import asyncio
import os
import sys
from pathlib import Path

# Allow running as a script from the project root
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


def _split_sql_statements(sql: str) -> list[str]:
    """Split SQL by semicolon while respecting quoted strings."""
    statements: list[str] = []
    in_single = False
    in_double = False
    current: list[str] = []

    for ch in sql:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double

        if ch == ";" and not in_single and not in_double:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
        else:
            current.append(ch)

    tail = "".join(current).strip()
    if tail:
        statements.append(tail)

    return statements


_load_env_file()

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ["SUPABASE_DB_URL"]
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "3072"))
ENABLE_HNSW = EMBEDDING_DIM <= 2000


DDL = f"""
-- Enable pgvector if not already enabled
CREATE EXTENSION IF NOT EXISTS vector;

-- -----------------------------------------------------------------------
-- aita_search_spaces  (one row per class / course)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aita_search_spaces (
    id               SERIAL PRIMARY KEY,
    name             VARCHAR(200) NOT NULL,
    slug             VARCHAR(200) NOT NULL UNIQUE,
    subject_name     VARCHAR(200) NOT NULL,
    weight_overrides JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
    metadata         JSONB,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aita_search_spaces_name ON aita_search_spaces (name);
CREATE INDEX IF NOT EXISTS idx_aita_search_spaces_slug ON aita_search_spaces (slug);

-- -----------------------------------------------------------------------
-- aita_documents  (one row per uploaded PDF / material)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aita_documents (
    id                      SERIAL PRIMARY KEY,
    title                   VARCHAR        NOT NULL,
    document_type           VARCHAR        NOT NULL DEFAULT 'EDUCATIONAL_FILE',
    material_kind           VARCHAR(50)    NOT NULL DEFAULT 'other',
    content                 TEXT           NOT NULL,
    source_markdown         TEXT,
    content_hash            VARCHAR        NOT NULL UNIQUE,
    unique_identifier_hash  VARCHAR        UNIQUE,
    embedding               vector({EMBEDDING_DIM}),
    document_metadata       JSONB,
    page_count              INTEGER,
    week                    INTEGER,
    status                  JSONB          NOT NULL DEFAULT '{{"state": "ready"}}'::jsonb,
    search_space_id         INTEGER        NOT NULL REFERENCES aita_search_spaces(id) ON DELETE CASCADE,
    created_at              TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aita_documents_search_space ON aita_documents (search_space_id);
CREATE INDEX IF NOT EXISTS idx_aita_documents_material_kind ON aita_documents (material_kind);
CREATE INDEX IF NOT EXISTS idx_aita_documents_status ON aita_documents USING gin (status);
CREATE INDEX IF NOT EXISTS idx_aita_documents_content_hash ON aita_documents (content_hash);

-- -----------------------------------------------------------------------
-- aita_chunks  (one row per layout-aware Item / paragraph / heading / figure)
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aita_chunks (
    id           SERIAL PRIMARY KEY,
    content      TEXT           NOT NULL,
    embedding    vector({EMBEDDING_DIM}),
    page_number  INTEGER,
    section_path TEXT,
    chunk_type   VARCHAR(20)    NOT NULL DEFAULT 'body',
    figure_id    VARCHAR,
    document_id  INTEGER        NOT NULL REFERENCES aita_documents(id) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_aita_chunks_document ON aita_chunks (document_id);
CREATE INDEX IF NOT EXISTS idx_aita_chunks_page ON aita_chunks (page_number);

-- Full-text search index (GIN on tsvector of chunk content)
CREATE INDEX IF NOT EXISTS idx_aita_chunks_fts
    ON aita_chunks USING gin (to_tsvector('english', content));
"""

if ENABLE_HNSW:
    DDL += """
-- Document-level vector index (HNSW for fast ANN search)
CREATE INDEX IF NOT EXISTS idx_aita_documents_embedding
    ON aita_documents USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Chunk-level vector index (HNSW - primary retrieval index)
CREATE INDEX IF NOT EXISTS idx_aita_chunks_embedding
    ON aita_chunks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""


async def run() -> None:
    if not ENABLE_HNSW:
        print(
            f"EMBEDDING_DIM={EMBEDDING_DIM} is greater than 2000; "
            "skipping HNSW index creation for vector columns."
        )

    engine = create_async_engine(DATABASE_URL, echo=True)
    async with engine.begin() as conn:
        for stmt in _split_sql_statements(DDL):
            await conn.execute(text(stmt + ";"))
    await engine.dispose()
    print("Schema migration complete.")


if __name__ == "__main__":
    asyncio.run(run())
