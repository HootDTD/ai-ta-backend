"""SQLAlchemy models for Apollo persistence (Postgres only).

V3: KG entries moved to Neo4j (apollo.persistence.neo4j_client +
apollo.knowledge_graph.store). Postgres now owns only:
  apollo_sessions, apollo_messages, apollo_problem_attempts,
  apollo_student_progress.

The legacy `apollo_kg_entries` table is dropped — see migration notes in
docs. No production data, so no backfill.
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


class Subject(Base):
    """Top-level curriculum domain. See migration 018."""

    __tablename__ = "apollo_subjects"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    slug = Column(Text, nullable=False, unique=True)
    display_name = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class Concept(Base):
    """Per-concept curriculum row. Replaces the apollo/subjects/<s>/concepts/<c>/
    filesystem registry — runtime loads concept_id, never subject/concept slugs.
    See migration 018."""

    __tablename__ = "apollo_concepts"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    subject_id = Column(BigInteger, ForeignKey("apollo_subjects.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    slug = Column(Text, nullable=False)
    display_name = Column(Text, nullable=False)
    canonical_symbols = Column(_JSONType, nullable=False, default=dict)
    normalization_map = Column(_JSONType, nullable=False, default=dict)
    parser_prompt_template = Column(Text, nullable=False, default="")
    solver_hints = Column(_JSONType, nullable=False, default=dict)
    forbidden_named_laws = Column(_JSONType, nullable=False, default=dict)
    concept_dag = Column(_JSONType, nullable=False, default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class ConceptProblem(Base):
    """Per-concept problem bank. See migration 018."""

    __tablename__ = "apollo_concept_problems"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    concept_id = Column(BigInteger, ForeignKey("apollo_concepts.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    problem_code = Column(Text, nullable=False)
    difficulty = Column(Text, nullable=False)
    payload = Column(_JSONType, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class Misconception(Base):
    """Per-concept misconception bank backing the misconception-inference
    channel. See migration 019. The description_embedding column is the
    pgvector field used at retrieval; SQLAlchemy stores it as the raw
    Postgres vector type — the runtime queries it via raw SQL because
    pgvector's SQLAlchemy adapter isn't a hard dep here."""

    __tablename__ = "apollo_misconceptions"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    concept_id = Column(BigInteger, ForeignKey("apollo_concepts.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    code = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    # description_embedding is vector(3072) on Postgres. SQLAlchemy doesn't
    # have a built-in adapter; the seeder + runtime use raw SQL for
    # this column. Modeling it here keeps the metadata table-mapping
    # complete for tests that introspect the schema.
    confusion_pair_a = Column(Text, nullable=True)
    confusion_pair_b = Column(Text, nullable=True)
    trigger_phrases = Column(_JSONType, nullable=False, default=list)
    probe_question = Column(Text, nullable=False)
    rt_steps = Column(_JSONType, nullable=False, default=list)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class ApolloSession(Base):
    __tablename__ = "apollo_sessions"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    student_id = Column(Text, nullable=False, index=True)
    # Legacy (TEXT, hardcoded mapping in problem_selector). Kept during the
    # migration window for backward compatibility while seeded sessions
    # exist; new sessions populate concept_id and ignore this column.
    # Migration 022 will drop it once the cutover completes.
    concept_cluster_id = Column(Text, nullable=True)
    # FK into apollo_concepts (migration 018). All new code paths read
    # concept_id from the session row and pass it as the only concept
    # signal — no subject/concept slug strings appear in handler code.
    concept_id = Column(
        BigInteger,
        ForeignKey("apollo_concepts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status = Column(Text, nullable=False, default=SessionStatus.active.value)
    phase = Column(Text, nullable=False, default=SessionPhase.INIT.value)
    current_problem_id = Column(Text, nullable=True)
    # Item #5: pending intent awaiting student confirmation. One of the
    # `Intent` values from `apollo.handlers.intent` (e.g. "done"); cleared
    # on the next turn whether or not the student confirmed.
    pending_intent = Column(Text, nullable=True)
    # Item #2: rolling summary of older conversation turns so chat context
    # stays bounded. Refreshed when the unwindowed-tail count crosses K
    # turns; null until the session is long enough to warrant one.
    history_summary = Column(Text, nullable=True)
    history_summary_up_to_turn = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    last_touched_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")
    problem_attempts = relationship("ProblemAttempt", back_populates="session", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "apollo_messages"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    session_id = Column(BigInteger, ForeignKey("apollo_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    role = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    turn_index = Column(Integer, nullable=False)
    # Migration 020: per-turn signals (misconception, sufficiency snapshot,
    # intent classification) persisted alongside the message. Readers must
    # tolerate missing keys — other writers may add fields here over time.
    message_metadata = Column("metadata", _JSONType, nullable=True)
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
    sqlite_where=(ApolloSession.status == "active"),
)


class StudentProgress(Base):
    __tablename__ = "apollo_student_progress"

    student_id = Column(Text, primary_key=True)
    xp_total = Column(Integer, nullable=False, default=0)
    level = Column(Integer, nullable=False, default=1)
    last_level_up_at = Column(TIMESTAMP(timezone=True), nullable=True)


class KGNegotiation(Base):
    """Audit log for Negotiable OLM moves on a KG entry. See migration 021.

    One row per move. The latest row per (attempt_id, entry_id) is the
    operative move when the Done-gate evaluates whether a flagged entry has
    been touched. The Neo4j node_id stored as `entry_id` is the cross-store
    bridge — we deliberately do NOT FK to it because Neo4j is the canonical
    KG store; orphan rows are cleaned up by the attempt_id ON DELETE CASCADE.
    """

    __tablename__ = "apollo_kg_negotiations"

    id = Column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entry_id = Column(Text, nullable=False)
    actor = Column(Text, nullable=False)  # 'student' | 'parser' | 'system'
    move = Column(Text, nullable=False)   # 'challenge' | 'paraphrase' | 'skip'
    payload = Column(_JSONType, nullable=False, default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


Index(
    "apollo_kg_negotiations_attempt_entry_idx",
    KGNegotiation.attempt_id,
    KGNegotiation.entry_id,
)
