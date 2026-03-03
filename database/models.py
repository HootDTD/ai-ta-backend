"""SQLAlchemy ORM models for AI-TA's pgvector-backed retrieval system."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    TIMESTAMP,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, relationship

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "3072"))


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


class DocumentType(StrEnum):
    EDUCATIONAL_FILE = "EDUCATIONAL_FILE"


class DocumentStatus:
    """Helper for document processing status stored as JSONB."""

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


class SearchSpace(BaseModel, TimestampMixin):
    """One AI-TA class/course (e.g. 'AAE 33300')."""

    __tablename__ = "aita_search_spaces"

    name = Column(String(200), nullable=False, index=True)
    slug = Column(String(200), nullable=False, index=True, unique=True)
    subject_name = Column(String(200), nullable=False)

    weight_overrides = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
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
    """A single educational document indexed for retrieval."""

    __tablename__ = "aita_documents"

    title = Column(String, nullable=False, index=True)
    document_type = Column(String, nullable=False, default=DocumentType.EDUCATIONAL_FILE)
    material_kind = Column(String(50), nullable=False, default="other", index=True)
    content = Column(Text, nullable=False)
    source_markdown = Column(Text, nullable=True)
    content_hash = Column(String, nullable=False, index=True, unique=True)
    unique_identifier_hash = Column(String, nullable=True, index=True, unique=True)
    embedding = Column(Vector(EMBEDDING_DIM))
    document_metadata = Column(JSONB, nullable=True)
    page_count = Column(Integer, nullable=True)
    week = Column(Integer, nullable=True, index=True)

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
    """A single layout-aware chunk from a document."""

    __tablename__ = "aita_chunks"

    content = Column(Text, nullable=False)
    embedding = Column(Vector(EMBEDDING_DIM))
    page_number = Column(Integer, nullable=True, index=True)
    section_path = Column(Text, nullable=True)
    chunk_type = Column(String(20), nullable=False, default="body")
    figure_id = Column(String, nullable=True)

    document_id = Column(
        Integer, ForeignKey("aita_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document = relationship("AITADocument", back_populates="chunks")


__all__ = [
    "Base",
    "SearchSpace",
    "AITADocument",
    "AITAChunk",
    "DocumentType",
    "DocumentStatus",
    "EMBEDDING_DIM",
]
