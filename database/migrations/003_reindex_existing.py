"""Migration 003 — Re-embed existing FAISS materials into pgvector.

For each aita_document with status=pending, this script:
1. Reads the legacy items.jsonl file from the index_path stored in document_metadata
2. Re-embeds each Item's text via OpenAI (same model = same vector space)
3. Inserts Chunk rows into aita_chunks
4. Updates the Document status to ready

This is a one-time migration. After it completes, the full pgvector path is live.

Run:
    python -m backend.migrations.003_reindex_existing

Requirements:
  - SUPABASE_DB_URL, OPENAI_API_KEY
  - The FAISS index directories must still be accessible on disk
"""

raise SystemExit(
    "Legacy Python migrations are retired. Use the timestamped Supabase chain via "
    "`node scripts/db/reset-local.mjs`; never run database/migrations/*.py."
)  # pragma: no cover - intentional command-line guard

import asyncio
import json
import logging
import os
import re
import sys
import time
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

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DATABASE_URL = os.environ["SUPABASE_DB_URL"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "3072"))
BATCH_SIZE = int(os.getenv("REINDEX_BATCH_SIZE", "50"))  # chunks per OpenAI batch call


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def _clean_text(value: str | None) -> str:
    """Remove control characters that PostgreSQL TEXT cannot safely store."""
    if not value:
        return ""
    return _CONTROL_CHARS_RE.sub("", value)


def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Call OpenAI embeddings API for a batch of texts."""
    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texts,
        dimensions=EMBEDDING_DIM,
    )
    return [item.embedding for item in resp.data]


def _load_items_jsonl(index_path: str) -> list[dict]:
    """Load items.jsonl from a legacy FAISS index directory."""
    p = Path(index_path) / "items.jsonl"
    if not p.exists():
        log.warning("items.jsonl not found at %s", p)
        return []
    items: list[dict] = []

    def _read_with_encoding(encoding: str) -> list[dict]:
        parsed: list[dict] = []
        with p.open(encoding=encoding) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return parsed

    try:
        return _read_with_encoding("utf-8")
    except UnicodeDecodeError:
        log.warning("items.jsonl at %s is not valid UTF-8; retrying with cp1252.", p)

    try:
        return _read_with_encoding("cp1252")
    except UnicodeDecodeError:
        log.warning("items.jsonl at %s could not be decoded with cp1252; using replacement fallback.", p)

    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return items


async def run():
    engine = create_async_engine(DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with session_factory() as session:
        # Fetch all pending documents
        result = await session.execute(
            text("""
                SELECT id, title, material_kind, document_metadata, search_space_id
                FROM aita_documents
                WHERE status->>'state' IN ('pending', 'processing')
                ORDER BY id
            """)
        )
        pending_docs = result.fetchall()

    if not pending_docs:
        log.info("No pending or processing documents found. Nothing to reindex.")
        await engine.dispose()
        return

    log.info("Found %d pending/processing documents to reindex.", len(pending_docs))

    async with session_factory() as session:
        for doc_row in pending_docs:
            doc_id, title, kind, metadata, space_id = doc_row
            index_path = (metadata or {}).get("index_path") if metadata else None

            if not index_path:
                log.warning("Document %d '%s' has no index_path in metadata, skipping.", doc_id, title)
                await session.execute(
                    text("UPDATE aita_documents SET status = :s WHERE id = :id"),
                    {"s": json.dumps({"state": "failed", "reason": "no index_path in metadata"}), "id": doc_id},
                )
                await session.commit()
                continue

            # Mark as processing
            await session.execute(
                text("UPDATE aita_documents SET status = :s WHERE id = :id"),
                {"s": json.dumps({"state": "processing"}), "id": doc_id},
            )
            await session.commit()

            items = _load_items_jsonl(index_path)
            if not items:
                log.warning("Document %d '%s': no items found in %s", doc_id, title, index_path)
                await session.execute(
                    text("UPDATE aita_documents SET status = :s WHERE id = :id"),
                    {"s": json.dumps({"state": "failed", "reason": "items.jsonl empty or missing"}), "id": doc_id},
                )
                await session.commit()
                continue

            log.info("Document %d '%s': %d items, embedding in batches of %d...", doc_id, title, len(items), BATCH_SIZE)

            # Delete any existing chunks for this document (idempotent)
            await session.execute(
                text("DELETE FROM aita_chunks WHERE document_id = :id"), {"id": doc_id}
            )

            # Process in batches
            chunk_rows = []
            total_embedded = 0
            for i in range(0, len(items), BATCH_SIZE):
                batch = items[i : i + BATCH_SIZE]
                texts = [_clean_text(item.get("text") or item.get("raw_text") or "") for item in batch]
                texts = [t[:8000] for t in texts]  # Truncate to model limit

                try:
                    embeddings = _embed_batch(texts)
                except Exception as e:
                    log.error("Embedding batch failed for doc %d: %s", doc_id, e)
                    # Retry once after a short wait
                    time.sleep(5)
                    try:
                        embeddings = _embed_batch(texts)
                    except Exception as e2:
                        log.error("Retry failed for doc %d: %s", doc_id, e2)
                        break

                for item, emb in zip(batch, embeddings):
                    content = _clean_text(item.get("text") or item.get("raw_text") or "")
                    section_path = _clean_text(" > ".join(item.get("section_path") or []))
                    chunk_rows.append({
                        "content": content,
                        "embedding": emb,
                        "page_number": item.get("page"),
                        "section_path": section_path,
                        "chunk_type": item.get("type") or "body",
                        "figure_id": item.get("figure_id"),
                        "document_id": doc_id,
                    })
                    total_embedded += 1

            # Batch insert chunks
            for chunk in chunk_rows:
                await session.execute(
                    text("""
                        INSERT INTO aita_chunks
                            (content, embedding, page_number, section_path, chunk_type, figure_id, document_id)
                        VALUES
                            (:content, :embedding, :page_number, :section_path, :chunk_type, :figure_id, :document_id)
                    """),
                    {
                        "content": chunk["content"],
                        "embedding": str(chunk["embedding"]),
                        "page_number": chunk["page_number"],
                        "section_path": chunk["section_path"] or None,
                        "chunk_type": chunk["chunk_type"],
                        "figure_id": chunk["figure_id"],
                        "document_id": chunk["document_id"],
                    },
                )

            # Build document-level content (first 2000 chars of combined text)
            all_text = " ".join(
                _clean_text(item.get("text") or "").strip()
                for item in items
                if item.get("type") in ("body", "heading", "ocr")
            )
            doc_content = all_text[:2000] if all_text else _clean_text(title)

            # Embed document-level summary
            try:
                doc_emb = _embed_batch([doc_content])[0]
                await session.execute(
                    text("""
                        UPDATE aita_documents
                        SET content = :content, embedding = :embedding,
                            status = :status, page_count = :page_count
                        WHERE id = :id
                    """),
                    {
                        "content": doc_content,
                        "embedding": str(doc_emb),
                        "status": json.dumps({"state": "ready"}),
                        "page_count": max((item.get("page") or 0) for item in items),
                        "id": doc_id,
                    },
                )
            except Exception as e:
                log.error("Document embedding failed for doc %d: %s", doc_id, e)
                await session.execute(
                    text("UPDATE aita_documents SET status = :s WHERE id = :id"),
                    {"s": json.dumps({"state": "failed", "reason": str(e)[:500]}), "id": doc_id},
                )

            await session.commit()
            log.info("  Document %d: %d chunks indexed.", doc_id, total_embedded)

    await engine.dispose()
    log.info("Reindex migration complete.")


if __name__ == "__main__":
    asyncio.run(run())
