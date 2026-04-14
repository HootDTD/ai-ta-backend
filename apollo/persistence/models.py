"""SQLAlchemy models for Apollo v2 Slice 0 persistence.

Adds four tables to Supabase Postgres: apollo_sessions, apollo_kg_entries,
apollo_messages, apollo_problem_attempts. Shares the Base declarative base
from database/models.py.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    BigInteger,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from database.models import Base

# JSONB on Postgres, fall back to JSON on SQLite (tests use in-memory SQLite).
_JSONType = JSONB().with_variant(JSON(), "sqlite")


class SessionPhase(StrEnum):
    INIT = "INIT"
    TEACHING = "TEACHING"
    PROBLEM_REVEAL = "PROBLEM_REVEAL"
    SOLVING = "SOLVING"
    REPORT = "REPORT"
    BETWEEN = "BETWEEN"


class SessionStatus(StrEnum):
    active = "active"
    paused = "paused"
    ended = "ended"


class ApolloSession(Base):
    __tablename__ = "apollo_sessions"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    student_id = Column(Text, nullable=False, index=True)
    concept_cluster_id = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default=SessionStatus.active.value)
    phase = Column(Text, nullable=False, default=SessionPhase.INIT.value)
    current_problem_id = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    last_touched_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    kg_entries = relationship("KGEntry", back_populates="session", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")
    problem_attempts = relationship("ProblemAttempt", back_populates="session", cascade="all, delete-orphan")


class KGEntry(Base):
    __tablename__ = "apollo_kg_entries"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    session_id = Column(BigInteger, ForeignKey("apollo_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(Text, nullable=False)
    content = Column(_JSONType, nullable=False)
    source = Column(Text, nullable=False, default="parser")
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    session = relationship("ApolloSession", back_populates="kg_entries")

    def __init__(self, **kwargs):
        kwargs.setdefault("source", "parser")
        super().__init__(**kwargs)


class Message(Base):
    __tablename__ = "apollo_messages"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    session_id = Column(BigInteger, ForeignKey("apollo_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    turn_index = Column(Integer, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    session = relationship("ApolloSession", back_populates="messages")


class ProblemAttempt(Base):
    __tablename__ = "apollo_problem_attempts"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    session_id = Column(BigInteger, ForeignKey("apollo_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    problem_id = Column(Text, nullable=False)
    difficulty = Column(Text, nullable=False)
    result = Column(Text, nullable=True)  # solved | stuck | skipped | returned_to_hoot
    solver_trace = Column(_JSONType, nullable=True)
    diagnostic_report = Column(_JSONType, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    session = relationship("ApolloSession", back_populates="problem_attempts")


Index(
    "ix_apollo_sessions_unique_active_per_student",
    ApolloSession.student_id,
    unique=True,
    postgresql_where=(ApolloSession.status == "active"),
)
