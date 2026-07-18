#!/usr/bin/env python3
"""Quick smoke test for pgvector hybrid search using Gen 3 schema."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from database.session import run_async, get_async_session
from database.models import Course, Document, DocumentChunk
from sqlalchemy import select, func


async def _run():
    async with get_async_session() as session:
        # 1. List search spaces
        result = await session.execute(
            select(Course).order_by(Course.name).limit(5)
        )
        spaces = result.scalars().all()
        if not spaces:
            print("ERROR: No courses found in app.courses!")
            sys.exit(1)

        print("=== available search spaces ===")
        for s in spaces:
            print(f"  id={s.id}  slug={s.slug}  name={s.name}")
        space = spaces[0]
        print(f"  Using: id={space.id} ({space.slug})\n")

        # 2. List documents for this space
        result = await session.execute(
            select(Document)
            .where(Document.course_id == space.id)
            .limit(5)
        )
        docs = result.scalars().all()
        print(f"=== documents in {space.slug} ===")
        for d in docs:
            print(f"  id={d.id}  kind={d.material_kind}  title={d.title}")
        print(f"  -> {len(docs)} documents\n")

        if not docs:
            print("No documents found. Exiting.")
            return

        # 3. Count chunks
        doc_ids = [d.id for d in docs]
        result = await session.execute(
            select(func.count(DocumentChunk.id))
            .where(DocumentChunk.document_id.in_(doc_ids))
        )
        chunk_count = result.scalar()
        print(f"=== chunk count ===")
        print(f"  {chunk_count} chunks across {len(docs)} documents\n")

        # 4. Sample chunks
        result = await session.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id.in_(doc_ids))
            .limit(3)
        )
        chunks = result.scalars().all()
        print("=== sample chunks ===")
        for c in chunks:
            preview = (c.content or "")[:80].replace("\n", " ")
            print(f"  id={c.id}  doc={c.document_id}  page={c.page_number}  type={c.chunk_type}  {preview}")
        print()

    print("Done!")


run_async(_run())
