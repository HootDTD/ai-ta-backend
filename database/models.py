"""SQLAlchemy ORM models for AI-TA's pgvector-backed retrieval system."""
from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    TIMESTAMP,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
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
    """Typed document processing states stored in ``app.documents.status``."""

    READY = "ready"
    PENDING = "queued"
    PROCESSING = "processing"
    FAILED = "failed"

    @staticmethod
    def ready() -> str:
        return DocumentStatus.READY

    @staticmethod
    def pending() -> str:
        return DocumentStatus.PENDING

    @staticmethod
    def processing() -> str:
        return DocumentStatus.PROCESSING

    @staticmethod
    def failed(reason: str) -> str:
        del reason
        return DocumentStatus.FAILED

    @staticmethod
    def get_state(status: str | None) -> str | None:
        return status

    @staticmethod
    def is_state(status: str | None, state: str) -> bool:
        return DocumentStatus.get_state(status) == state

    @staticmethod
    def get_failure_reason(status: str | None, failure_reason: str | None = None) -> str | None:
        return failure_reason if status == DocumentStatus.FAILED else None


class Course(BaseModel, TimestampMixin):
    """One AI-TA class/course (e.g. 'AAE 33300')."""

    __tablename__ = "courses"
    __table_args__ = (
        CheckConstraint("current_week BETWEEN 1 AND 16"),
        CheckConstraint(
            "0 <= retrieval_weight_min AND retrieval_weight_min <= retrieval_weight_max "
            "AND retrieval_weight_max <= 1"
        ),
        {"schema": "app"},
    )

    id = Column(BigInteger, primary_key=True, index=True)

    name = Column(String(200), nullable=False, index=True)
    slug = Column(String(200), nullable=False, index=True, unique=True)
    subject_name = Column(String(200), nullable=False)

    current_week = Column(SmallInteger, nullable=False, default=1, server_default=text("1"))
    retrieval_weights = Column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    retrieval_weight_min = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    retrieval_weight_max = Column(Float, nullable=False, default=1.0, server_default=text("1"))
    metadata_ = Column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))

    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )

    documents = relationship(
        "Document",
        back_populates="course",
        order_by="Document.id",
        cascade="all, delete-orphan",
    )


class Document(BaseModel, TimestampMixin):
    """A single educational document indexed for retrieval."""

    __tablename__ = "documents"
    __table_args__ = {"schema": "app"}

    id = Column(BigInteger, primary_key=True, index=True)

    title = Column(String, nullable=False, index=True)
    document_type = Column(String, nullable=False, default=DocumentType.EDUCATIONAL_FILE)
    material_kind = Column(String(50), nullable=False, default="other", index=True)
    content = Column(Text, nullable=False)
    source_markdown = Column(Text, nullable=True)
    content_hash = Column(String, nullable=False, index=True, unique=True)
    unique_identifier_hash = Column(String, nullable=True, index=True, unique=True)
    embedding = Column(Vector(EMBEDDING_DIM))
    metadata_ = Column("metadata", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    page_count = Column(Integer, nullable=True)
    week = Column(Integer, nullable=True, index=True)

    status = Column(
        String(20),
        nullable=False,
        default=DocumentStatus.ready,
        server_default=text("'ready'"),
        index=True,
    )
    failure_reason = Column(Text, nullable=True)

    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        index=True,
    )

    course_id = Column(
        BigInteger, ForeignKey("app.courses.id", ondelete="CASCADE"), nullable=False, index=True
    )

    course = relationship("Course", back_populates="documents")
    chunks = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
    )


class DocumentChunk(BaseModel, TimestampMixin):
    """A single layout-aware chunk from a document."""

    __tablename__ = "document_chunks"
    __table_args__ = {"schema": "internal"}

    id = Column(BigInteger, primary_key=True, index=True)

    course_id = Column(
        BigInteger, ForeignKey("app.courses.id", ondelete="CASCADE"), nullable=False, index=True
    )

    content = Column(Text, nullable=False)
    embedding = Column(Vector(EMBEDDING_DIM))
    page_number = Column(Integer, nullable=True, index=True)
    section_path = Column(Text, nullable=True)
    chunk_type = Column(String(20), nullable=False, default="body")
    figure_id = Column(String, nullable=True)

    document_id = Column(
        BigInteger, ForeignKey("app.documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document = relationship("Document", back_populates="chunks")


class CourseMembership(Base):
    """User membership to a course search space."""

    __tablename__ = "course_memberships"
    __table_args__ = {"schema": "app"}

    user_id = Column(UUID(as_uuid=False), primary_key=True)
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
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


class CourseInvite(Base):
    """Shareable invite link for course enrollment."""

    __tablename__ = "course_invites"
    __table_args__ = {"schema": "app"}

    id = Column(BigInteger, primary_key=True, index=True)
    code = Column(String, nullable=False, unique=True, index=True)
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(20), nullable=False)
    created_by = Column(UUID(as_uuid=False), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    max_uses = Column(Integer, nullable=True)
    use_count = Column(Integer, nullable=False, default=0)
    expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
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


class Upload(Base):
    """Teacher-uploaded weekly notes/slides metadata."""

    __tablename__ = "uploads"
    __table_args__ = {"schema": "app"}

    id = Column(BigInteger, primary_key=True, index=True)
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    week = Column(Integer, nullable=False, index=True)
    kind = Column(String(20), nullable=False, index=True)
    title = Column(String, nullable=False)
    source_name = Column(String, nullable=True)
    document_id = Column(
        BigInteger,
        ForeignKey("app.documents.id", ondelete="SET NULL"),
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
    ocr_details = Column(
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


class UploadJob(Base):
    """Durable background work queue for teacher upload ingestion."""

    __tablename__ = "upload_jobs"
    __table_args__ = {"schema": "internal"}

    id = Column(BigInteger, primary_key=True, index=True)
    upload_id = Column(
        BigInteger,
        ForeignKey("app.uploads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
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
        ForeignKey("app.courses.id", ondelete="CASCADE"),
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
    topic_centroid_vector = Column(Vector(3072), nullable=True)

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
    citations = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    # §10 RQ5 hedge (WU-5B4): write-only <=8 concept terms from
    # extract_and_filter_keywords, persisted for offline class-level backfill.
    # No read path in v1. Mirrors the attachments/citations JSONB-array shape.
    keywords = Column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))

    session = relationship("ChatSession", back_populates="turns")


class ChatSessionSnippet(Base):
    """Cached snippet citations across turns of one chat session.

    Enables retrieval-mode NONE / AUGMENT to skip pgvector when prior
    cited material already covers the follow-up question.
    """

    __tablename__ = "chat_session_snippets"

    chat_session_id = Column(
        BigInteger, ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True
    )
    chunk_id = Column(
        BigInteger, ForeignKey("internal.document_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    original_score = Column(Float, nullable=False)
    first_seen_turn = Column(Integer, nullable=False)
    last_used_turn = Column(Integer, nullable=False)
    snippet_payload = Column(JSONB, nullable=False)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_css_session_lru", "chat_session_id", "last_used_turn"),
    )


class ChatRouterDecision(Base):
    """One row per /ask invocation.

    Telemetry for tuning thresholds and eventually retiring the LLM stage
    of the router.
    """

    __tablename__ = "chat_router_decisions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    turn_id = Column(Text, nullable=False)
    chat_session_id = Column(
        BigInteger,
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    query = Column(Text, nullable=False)
    stage1_top1 = Column(Float, nullable=True)
    stage1_margin = Column(Float, nullable=True)
    stage1_route = Column(String(50), nullable=True)
    stage2_invoked = Column(Boolean, nullable=False, default=False)
    stage2_route = Column(String(50), nullable=True)
    stage2_confidence = Column(Float, nullable=True)
    retrieval_mode = Column(String(20), nullable=False)
    retrieval_top_score = Column(Float, nullable=True)
    final_route = Column(String(50), nullable=False)
    was_clarified = Column(Boolean, nullable=False, default=False)
    clarify_cause = Column(String(50), nullable=True)
    latency_router_ms = Column(Integer, nullable=True)
    latency_retrieval_ms = Column(Integer, nullable=True)
    latency_answer_ms = Column(Integer, nullable=True)
    created_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )


__all__ = [
    "Base",
    "Course",
    "Document",
    "DocumentChunk",
    "CourseMembership",
    "CourseInvite",
    "Upload",
    "UploadJob",
    "ChatSession",
    "ChatTurn",
    "ChatSessionSnippet",
    "ChatRouterDecision",
    "DocumentType",
    "DocumentStatus",
    "EMBEDDING_DIM",
]
