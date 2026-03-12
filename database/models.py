"""SQLAlchemy ORM models for AI-TA's pgvector-backed retrieval system."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, TIMESTAMP, Column, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
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


class CourseMembership(Base):
    """User membership to a course search space."""

    __tablename__ = "course_memberships"

    user_id = Column(UUID(as_uuid=False), primary_key=True)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        index=True,
    )
    role = Column(String(20), nullable=False, default="student")
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )


class TeacherCourse(Base):
    """Per-course teacher settings (week + retrieval weights)."""

    __tablename__ = "teacher_courses"

    id = Column(BigInteger, primary_key=True, index=True)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    current_week = Column(Integer, nullable=False, default=1)
    weights = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    weight_bounds = Column(
        JSONB,
        nullable=False,
        default=lambda: {"min": 0.0, "max": 1.0},
        server_default=text('\'{"min":0.0,"max":1.0}\'::jsonb'),
    )
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )


class TeacherUpload(Base):
    """Teacher-uploaded weekly notes/slides metadata."""

    __tablename__ = "teacher_uploads"

    id = Column(BigInteger, primary_key=True, index=True)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    week = Column(Integer, nullable=False, index=True)
    kind = Column(String(20), nullable=False, index=True)
    title = Column(String, nullable=False)
    source_name = Column(String, nullable=True)
    doc_id = Column(
        Integer,
        ForeignKey("aita_documents.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = Column(String(20), nullable=False, default="ready", index=True)
    storage_key = Column(String, nullable=True)
    artifact_manifest = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    ocr_provider = Column(String(50), nullable=True)
    ocr_summary = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    warning_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    error_message = Column(Text, nullable=True)
    started_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)
    completed_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    page_count = Column(Integer, nullable=True)
    is_latest = Column(Boolean, nullable=False, default=True, index=True)
    uploaded_by = Column(UUID(as_uuid=False), nullable=True, index=True)
    metadata_ = Column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    uploaded_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )


class TeacherUploadJob(Base):
    """Durable background work queue for teacher upload ingestion."""

    __tablename__ = "teacher_upload_jobs"

    id = Column(BigInteger, primary_key=True, index=True)
    upload_id = Column(
        BigInteger,
        ForeignKey("teacher_uploads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    state = Column(String(20), nullable=False, default="queued", index=True)
    lease_owner = Column(String(200), nullable=True, index=True)
    lease_expires_at = Column(TIMESTAMP(timezone=True), nullable=True, index=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error = Column(Text, nullable=True)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )


class ChatSession(Base):
    """Persisted chat session for memory + reporting."""

    __tablename__ = "chat_sessions"

    id = Column(BigInteger, primary_key=True, index=True)
    chat_id = Column(String(200), nullable=False, unique=True, index=True)
    user_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    meta = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    memory_summary = Column(Text, nullable=False, default="", server_default=text("''"))
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )

    turns = relationship(
        "ChatTurn",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatTurn.turn_index",
    )


class ChatTurn(Base):
    """Chat turn rows for a session."""

    __tablename__ = "chat_turns"

    id = Column(BigInteger, primary_key=True, index=True)
    chat_session_id = Column(
        BigInteger,
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_index = Column(Integer, nullable=False, index=True)
    turn_id = Column(String(100), nullable=False, index=True)
    role = Column(String(20), nullable=False, index=True)
    content = Column(Text, nullable=False, default="", server_default=text("''"))
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        index=True,
    )
    model = Column(String(100), nullable=True)
    tool_name = Column(String(100), nullable=True)
    tool_inputs = Column(JSONB, nullable=True)
    attachments = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))

    session = relationship("ChatSession", back_populates="turns")


__all__ = [
    "Base",
    "SearchSpace",
    "AITADocument",
    "AITAChunk",
    "CourseMembership",
    "TeacherCourse",
    "TeacherUpload",
    "ChatSession",
    "ChatTurn",
    "DocumentType",
    "DocumentStatus",
    "EMBEDDING_DIM",
]
