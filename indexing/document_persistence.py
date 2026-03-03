from __future__ import annotations

"""DB persistence helpers for the AI-TA indexing pipeline.

Ported from SurfSense's indexing_pipeline/document_persistence.py
with imports updated to use AI-TA's models.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import object_session
from sqlalchemy.orm.attributes import set_committed_value

from ..database.models import AITADocument, DocumentStatus


async def rollback_and_persist_failure(
    session: AsyncSession,
    document: AITADocument,
    message: str,
) -> None:
    """Roll back the current transaction and best-effort persist a failed status.

    Called exclusively from except blocks — must never raise, or the new exception
    would chain with the original and mask it entirely.
    """
    try:
        await session.rollback()
    except Exception:
        return
    try:
        await session.refresh(document)
        document.updated_at = datetime.now(UTC)
        document.status = DocumentStatus.failed(message)
        await session.commit()
    except Exception:
        pass  # Best-effort; document will be retried on next upload.


def attach_chunks_to_document(document: AITADocument, chunks: list) -> None:
    """Assign chunks to a document without triggering SQLAlchemy async lazy loading."""
    set_committed_value(document, "chunks", chunks)
    session = object_session(document)
    if session is not None:
        if document.id is not None:
            for chunk in chunks:
                chunk.document_id = document.id
        session.add_all(chunks)
