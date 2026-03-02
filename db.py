from __future__ import annotations

"""SQLAlchemy ORM models for AI-TA's pgvector-backed retrieval system.

This module is only active when USE_PGVECTOR_RETRIEVAL=true.
The existing FAISS/SQLite stack in retriever.py continues to work when the flag is off.
"""

import os
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, relationship

DATABASE_URL = os.getenv("SUPABASE_DB_URL", "")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "3072"))


# ---------------------------------------------------------------------------
# Base classes
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class TimestampMixin:
    @declared_attr
    def created_at(cls):  # noqa: N805
        return Column(
            TIMESTAMP(timezone=True),
            nullable=False,
            default=lambda: datetime.now(UTC),
            index=True,
        )


class BaseModel(Base):
    __abstract__ = True
    __allow_unmapped__ = True

    id = Column(Integer, primary_key=True, index=True)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DocumentType(StrEnum):
    """Document type enum — only educational types needed for AI-TA."""
    EDUCATIONAL_FILE = "EDUCATIONAL_FILE"


class DocumentStatus:
    """Helper for document processing status stored as JSONB.

    Values:
      {"state": "ready"}
      {"state": "pending"}
      {"state": "processing"}
      {"state": "failed", "reason": "..."}
    """

    READY = "ready"
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"

    @staticmethod
    def ready() -> dict:
        return {"state": DocumentStatus.READY}

    @staticmethod
    def pending() -> dict:
        return {"state": DocumentStatus.PENDING}

    @staticmethod
    def processing() -> dict:
        return {"state": DocumentStatus.PROCESSING}

    @staticmethod
    def failed(reason: str) -> dict:
        return {"state": DocumentStatus.FAILED, "reason": reason[:500]}

    @staticmethod
    def get_state(status: dict | None) -> str | None:
        if status is None:
            return None
        return status.get("state") if isinstance(status, dict) else None

    @staticmethod
    def is_state(status: dict | None, state: str) -> bool:
        return DocumentStatus.get_state(status) == state

    @staticmethod
    def get_failure_reason(status: dict | None) -> str | None:
        if not isinstance(status, dict):
            return None
        if status.get("state") == DocumentStatus.FAILED:
            return status.get("reason")
        return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SearchSpace(BaseModel, TimestampMixin):
    """One AI-TA class/course (e.g. 'AAE 33300').

    Maps to AI-TA's existing ClassWorkspace concept but stored in Postgres.
    Replaces the manifest.json + Supabase knowledge_subjects approach.
    """

    __tablename__ = "aita_search_spaces"

    name = Column(String(200), nullable=False, index=True)
    slug = Column(String(200), nullable=False, index=True, unique=True)
    subject_name = Column(String(200), nullable=False)

    # Per-workspace material kind weight overrides (replaces workspace.weight_overrides dict)
    # Format: {"textbook": 0.15, "slides": 0.08, ...}
    weight_overrides = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))

    # Arbitrary metadata (legacy class_id, custom instructions, etc.)
    metadata_ = Column("metadata", JSONB, nullable=True)

    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )

    documents = relationship(
        "AITADocument",
        back_populates="search_space",
        order_by="AITADocument.id",
        cascade="all, delete-orphan",
    )


class AITADocument(BaseModel, TimestampMixin):
    """A single educational document (PDF, PPTX, DOCX, etc.) indexed for retrieval.

    Replaces the FAISS index directory + items.jsonl per material.
    """

    __tablename__ = "aita_documents"

    title = Column(String, nullable=False, index=True)

    # DocumentType enum (always EDUCATIONAL_FILE for AI-TA)
    document_type = Column(String, nullable=False, default=DocumentType.EDUCATIONAL_FILE)

    # Material kind within the class: textbook / slides / homework / exams / notes / other
    # First-class column (not in JSONB) for efficient store-bias lookups.
    material_kind = Column(String(50), nullable=False, default="other", index=True)

    # Full content (concatenated text for document-level embedding)
    content = Column(Text, nullable=False)

    # Original raw text from layout_multimodal_embedder (kept for re-indexing)
    source_markdown = Column(Text, nullable=True)

    # SHA-256 hashes for deduplication (ported from SurfSense)
    content_hash = Column(String, nullable=False, index=True, unique=True)
    unique_identifier_hash = Column(String, nullable=True, index=True, unique=True)

    # Document-level embedding (for coarse retrieval / document search)
    embedding = Column(Vector(EMBEDDING_DIM))

    # Metadata: source PDF name, page count, week number, OCR flags, etc.
    document_metadata = Column(JSONB, nullable=True)

    # Page count (preserved for citations and teacher UI display)
    page_count = Column(Integer, nullable=True)

    # Teacher weekly upload: week number this material belongs to (None = base material)
    week = Column(Integer, nullable=True, index=True)

    # Processing state (pending → processing → ready/failed)
    status = Column(
        JSONB,
        nullable=False,
        default=DocumentStatus.ready,
        server_default=text('\'{"state": "ready"}\'::jsonb'),
        index=True,
    )

    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )

    search_space_id = Column(
        Integer, ForeignKey("aita_search_spaces.id", ondelete="CASCADE"), nullable=False, index=True
    )

    search_space = relationship("SearchSpace", back_populates="documents")
    chunks = relationship(
        "AITAChunk",
        back_populates="document",
        cascade="all, delete-orphan",
    )


class AITAChunk(BaseModel, TimestampMixin):
    """A single layout-aware chunk from a document.

    Each chunk corresponds to one Item from layout_multimodal_embedder.py
    (a body paragraph, heading, or figure caption). The 1:1 mapping preserves
    page numbers and section hierarchy needed for citation markers.
    """

    __tablename__ = "aita_chunks"

    content = Column(Text, nullable=False)

    # Vector embedding for this chunk (pgvector cosine search)
    embedding = Column(Vector(EMBEDDING_DIM))

    # Page number from source PDF — required for "[Textbook, p. 42]" citations
    page_number = Column(Integer, nullable=True, index=True)

    # Section hierarchy path (e.g. "Chapter 3 > 3.2 Boundary Layer Theory")
    section_path = Column(Text, nullable=True)

    # Chunk type from layout extractor: body / heading / figure / ocr
    chunk_type = Column(String(20), nullable=False, default="body")

    # Figure ID (for figure chunks that reference a diagram)
    figure_id = Column(String, nullable=True)

    document_id = Column(
        Integer, ForeignKey("aita_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document = relationship("AITADocument", back_populates="chunks")


# ---------------------------------------------------------------------------
# Engine and session factory
# ---------------------------------------------------------------------------

_engine = None
_session_factory = None


def _get_engine():
    global _engine
    if _engine is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "SUPABASE_DB_URL is not set. "
                "Set USE_PGVECTOR_RETRIEVAL=false to use the FAISS path."
            )
        _engine = create_async_engine(
            DATABASE_URL,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )
    return _engine


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            _get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a DB session."""
    factory = _get_session_factory()
    async with factory() as session:
        yield session


from contextlib import asynccontextmanager


@asynccontextmanager
async def get_async_session():
    """Async context manager for a DB session (non-FastAPI callers)."""
    factory = _get_session_factory()
    async with factory() as session:
        yield session


__all__ = [
    "Base",
    "SearchSpace",
    "AITADocument",
    "AITAChunk",
    "DocumentType",
    "DocumentStatus",
    "EMBEDDING_DIM",
    "get_db_session",
    "get_async_session",
    "_get_session_factory",
]
