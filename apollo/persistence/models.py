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
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL, UUID
from sqlalchemy.orm import relationship, validates

from database.models import Base

# JSONB on Postgres, fall back to JSON on SQLite (tests use in-memory SQLite).
_JSONType = JSONB().with_variant(JSON(), "sqlite")

# REAL[] on Postgres for the belief vectors; SQLite has no array type, so the
# fast metadata/insert tests degrade to JSON. The behavioral array-CHECK tests
# (array_length(belief,1)=3) run ONLY on real Postgres (see
# tests/database/test_apollo_learner_model_migration.py), so the SQLite variant
# is exercised by shape/round-trip tests, never for array semantics.
# NOTE: the SQLite variant is a bare JSON() — _JSONType is itself a variant and
# SQLAlchemy forbids nesting variants in with_variant().
_RealArrayType = ARRAY(REAL).with_variant(JSON(), "sqlite")

# App-layer mirrors of migration 026's enum sets (spec §2/§6.3). Following the
# ATTEMPT_RESULTS precedent: tuples the runtime reasons about, kept in sync with
# the migration. ENTITY_KINDS is the one tied to a SQL CHECK (on
# apollo_kg_entities.kind) and is asserted equal to the migration's set by
# apollo/persistence/tests/test_learner_model_allowlists.py. The other two are
# OPEN enums (no SQL CHECK) — documentation tuples, NOT asserted against the SQL.
ENTITY_KINDS = (
    "concept",
    "equation",
    "condition",
    "definition",
    "procedure",
    "variable",
    "misconception",
)
# event_kind is an OPEN enum per spec §2 (no SQL CHECK) — documented set only:
MASTERY_EVENT_KINDS = ("covered", "missing", "partial", "misconception", "corrected")
FINDING_KINDS = (
    "covered_node",
    "missing_node",
    "matched_edge",
    "missing_edge",
    "unsupported_extra",
    "contradiction",
    "unresolved",
    "alternative_path",
    "covered_by_contraction",
    "not_demonstrated",
)

# Allowed values for apollo_problem_attempts.result. The DB CHECK constraint is
# the schema authority (migration 009 created it, 025 widened it) — following
# this repo's convention, the ORM declares no CHECK constraints. This tuple is
# the APP-LAYER mirror of that allowlist; it MUST stay equal to the migration's
# set (asserted by apollo/persistence/tests/test_attempt_result_values.py) so
# code that reasons about result values can't drift from what the DB accepts.
#
#   solved/stuck/skipped/returned_to_hoot — legacy solver-era outcomes (kept so
#     historical rows stay valid and re-attempt detection still sees them).
#   abandoned — handle_next: prior attempt when the student switches problems.
#   graded    — handle_done: a completed diff+rubric grade (post commit 21b42e1).
ATTEMPT_RESULTS = (
    "solved",
    "stuck",
    "skipped",
    "returned_to_hoot",
    "abandoned",
    "graded",
)

# Subset of ATTEMPT_RESULTS that counts as a completed (graded) attempt for
# re-attempt detection. 'abandoned' is a mid-problem switch, not a grade, so it
# is excluded. See apollo.persistence.attempt_history.has_prior_graded_attempt.
GRADED_ATTEMPT_RESULTS = tuple(r for r in ATTEMPT_RESULTS if r != "abandoned")


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

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    slug = Column(Text, nullable=False, unique=True)
    display_name = Column(Text, nullable=False)
    # Course-scoping (migration 026 / isolation invariant §1.4). NOT NULL by
    # design: every subject belongs to exactly one course, and the whole
    # curriculum chain inherits course ownership through this column.
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class Concept(Base):
    """Per-concept curriculum row. Replaces the apollo/subjects/<s>/concepts/<c>/
    filesystem registry — runtime loads concept_id, never subject/concept slugs.
    See migration 018."""

    __tablename__ = "apollo_concepts"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    subject_id = Column(
        BigInteger, ForeignKey("apollo_subjects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slug = Column(Text, nullable=False)
    display_name = Column(Text, nullable=False)
    # Migration 038 (reversed provisioning): the closed-list concept-matcher
    # prompt line is "slug — display_name: description". Empty for legacy rows.
    description = Column(Text, nullable=False, default="")
    canonical_symbols = Column(_JSONType, nullable=False, default=dict)
    normalization_map = Column(_JSONType, nullable=False, default=dict)
    parser_prompt_template = Column(Text, nullable=False, default="")
    solver_hints = Column(_JSONType, nullable=False, default=dict)
    forbidden_named_laws = Column(_JSONType, nullable=False, default=dict)
    concept_dag = Column(_JSONType, nullable=False, default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class ConceptProblem(Base):
    """Per-concept problem bank. See migration 018 (base table) + migration 030
    (WU-3B2a §8B auto-provisioning columns). Following the repo convention these
    declare NO DB CHECK constraints — the migration SQL is the authority for
    tier/solution_source; the ORM is the typed Python access layer."""

    __tablename__ = "apollo_concept_problems"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    concept_id = Column(
        BigInteger, ForeignKey("apollo_concepts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    problem_code = Column(Text, nullable=False)
    difficulty = Column(Text, nullable=False)
    payload = Column(_JSONType, nullable=False)
    # WU-3B2a / migration 030 (§8B auto-provisioning). tier gates selectability:
    # 1 = inventory (auto-scraped, NOT teachable), 2 = teachable. The DB CHECK
    # (tier IN (1,2)) is the authority; the ORM declares none (repo convention).
    # server_default so raw-SQL/migration/legacy inserts see 1; default=2 for ORM
    # (the seed-helper/runtime reality is that authored curriculum is teachable —
    # mirroring migration 030's backfill of existing §8-seeded rows to tier=2).
    tier = Column(SmallInteger, nullable=False, server_default=text("1"), default=2)
    solution_source = Column(  # extracted|generated|authored|llm_paired (CHECK in SQL)
        Text, nullable=True
    )
    provenance = Column(_JSONType, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    quarantined_at = Column(TIMESTAMP(timezone=True), nullable=True)  # WU-3B2h filter; NULL = live
    # Denormalized course scope (migration 030). NULLABLE in v1 (backfilled
    # in-migration; a later two-step migration tightens it). No ORM FK declared
    # to keep create_all/SQLite parity simple — the migration SQL is the FK
    # authority where it exists.
    search_space_id = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    @staticmethod
    def _validate_solution_provenance(*, provenance: dict | None, solution_source: str | None):
        """Keep machine-generated problems out of teacher-grounded source lanes."""
        if (provenance or {}).get("source") == "generated" and solution_source in {
            "extracted",
            "authored",
            "llm_paired",
        }:
            raise ValueError(
                "machine-generated problem provenance requires "
                "solution_source='generated' (or unset pending generation)"
            )

    @validates("solution_source")
    def _validate_solution_source(self, _key: str, value: str | None):
        self._validate_solution_provenance(
            provenance=self.provenance,
            solution_source=value,
        )
        return value

    @validates("provenance")
    def _validate_provenance(self, _key: str, value: dict | None):
        self._validate_solution_provenance(
            provenance=value,
            solution_source=self.solution_source,
        )
        return value


class AuthoredSet(Base):
    """Problem-doc plus solution-doc pairing for authored-set provisioning
    (WU-AAS). result_summary holds the bounded per-problem outcome list."""

    __tablename__ = "apollo_authored_sets"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    set_index = Column(Integer, nullable=False)
    problem_document_id = Column(BigInteger, nullable=True)
    solution_document_id = Column(BigInteger, nullable=True)
    status = Column(Text, nullable=False, server_default=text("'pending'"), default="pending")
    result_summary = Column(
        _JSONType,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("search_space_id", "set_index", name="uq_authored_set_per_space"),
    )


class GenerationRun(Base):
    """Teacher-initiated problem-generation batch and its bounded result ledger."""

    __tablename__ = "apollo_generation_runs"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("apollo_concepts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(Text, nullable=False, server_default=text("'pending'"), default="pending")
    result_summary = Column(
        _JSONType,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("apollo_ingest_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class Misconception(Base):
    """Per-concept misconception bank backing the misconception-inference
    channel. See migration 019. description_embedding (vector(3072) on
    Postgres) is NOT declared as a Column here — SQLAlchemy has no built-in
    pgvector adapter, so the seeder + runtime read/write it via raw SQL
    instead; this model intentionally omits it."""

    __tablename__ = "apollo_misconceptions"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    concept_id = Column(
        BigInteger, ForeignKey("apollo_concepts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code = Column(Text, nullable=False)
    description = Column(Text, nullable=False)
    confusion_pair_a = Column(Text, nullable=True)
    confusion_pair_b = Column(Text, nullable=True)
    trigger_phrases = Column(_JSONType, nullable=False, default=list)
    probe_question = Column(Text, nullable=False)
    rt_steps = Column(_JSONType, nullable=False, default=list)
    # F-struct (migration 038): canonical entity_key of the reference node this
    # misconception opposes (e.g. "def.real_basis"), or NULL. Read by the
    # structural co-key gate path.
    opposes = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


# Mirrors the SQL CHECK in migration 032. Asserted equal by the allowlist test.
CLARIFICATION_STATES: tuple[str, ...] = ("asked_waiting", "confirmed", "refuted", "vague")


class Clarification(Base):
    """One probed ambiguous student idea (Apollo clarification loop, migration
    032). State machine asked_waiting -> {confirmed|refuted|vague}; UNIQUE on
    (attempt_id, node_id) enforces one follow-up per idea. ``user_id`` declares
    NO ORM FK (auth.users is Supabase-managed, absent from Base.metadata),
    mirroring GraphComparisonRun."""

    __tablename__ = "apollo_clarifications"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(
        BigInteger,
        ForeignKey("apollo_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("apollo_concepts.id", ondelete="SET NULL"),
        nullable=True,
    )
    node_id = Column(Text, nullable=False)
    candidate_key = Column(Text, nullable=False)
    state = Column(
        Text, nullable=False, server_default=text("'asked_waiting'"), default="asked_waiting"
    )
    probe_question = Column(Text, nullable=False)
    original_statement = Column(Text, nullable=False)
    clarification_text = Column(Text, nullable=True)
    asked_turn = Column(Integer, nullable=False)
    answered_turn = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("attempt_id", "node_id", name="apollo_clarifications_attempt_node_uniq"),
    )


class ReferenceQuestionOpportunity(Base):
    """Latest question state for one authored reference node per attempt."""

    __tablename__ = "apollo_reference_question_opportunities"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(
        BigInteger,
        ForeignKey("apollo_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reference_node_id = Column(Text, nullable=False)
    state = Column(Text, nullable=False, server_default=text("'asked_waiting'"))
    question = Column(Text, nullable=False)
    asked_turn = Column(Integer, nullable=False)
    answered_turn = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "reference_node_id",
            name="apollo_reference_question_opportunities_attempt_node_uniq",
        ),
    )


class ApolloSession(Base):
    __tablename__ = "apollo_sessions"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    # Phase-1 auth retrofit: real Supabase identity + course scoping (the
    # classroom-isolation invariant). Mirrors course_memberships' types.
    user_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # FK into apollo_concepts (migration 018). All code paths read concept_id
    # from the session row and pass it as the only concept signal — no
    # subject/concept slug strings appear in handler code. The legacy
    # concept_cluster_id (TEXT) column was dropped in migration 027 (WU-3D §8A
    # cutover) once the runtime stopped reading/writing it.
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
    last_touched_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")
    problem_attempts = relationship(
        "ProblemAttempt", back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "apollo_messages"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    session_id = Column(
        BigInteger, ForeignKey("apollo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
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

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    session_id = Column(
        BigInteger, ForeignKey("apollo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    problem_id = Column(Text, nullable=False)
    difficulty = Column(Text, nullable=False)
    # DB CHECK (migration 009 + 025) restricts this to ATTEMPT_RESULTS or NULL.
    result = Column(Text, nullable=True)
    solver_trace = Column(_JSONType, nullable=True)
    diagnostic_report = Column(_JSONType, nullable=True)
    # §6/§7 cross-store retry flag (migration 026): true while a Done-time
    # learner-model update is pending retry. server_default so raw-SQL/legacy
    # inserts see false; default=False for ORM inserts. The grade is never
    # voided by grading-pipeline failure — this flag covers the cross-store window.
    learner_update_pending = Column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    # WU-5B3a janitor (migration 028): dead-letter + exponential-backoff
    # bookkeeping for the learner_update retry. WU-5B3a-0 maps these columns;
    # the drain state machine (WU-5B3a-1) reads/writes them. server_default so
    # raw-SQL/legacy/migration inserts see the default; default=... for ORM.
    learner_update_attempts = Column(Integer, nullable=False, server_default=text("0"), default=0)
    learner_update_failed_at = Column(TIMESTAMP(timezone=True), nullable=True)
    learner_update_last_error = Column(Text, nullable=True)
    learner_update_next_attempt_at = Column(TIMESTAMP(timezone=True), nullable=True)
    learner_update_failed_permanently = Column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    session = relationship("ApolloSession", back_populates="problem_attempts")


Index(
    "ix_apollo_sessions_unique_active_per_user",
    ApolloSession.user_id,
    unique=True,
    postgresql_where=(ApolloSession.status == "active"),
    sqlite_where=(ApolloSession.status == "active"),
)


class StudentProgress(Base):
    __tablename__ = "apollo_student_progress"

    user_id = Column(UUID(as_uuid=False), primary_key=True)
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

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entry_id = Column(Text, nullable=False)
    actor = Column(Text, nullable=False)  # 'student' | 'parser' | 'system'
    move = Column(Text, nullable=False)  # 'challenge' | 'paraphrase' | 'skip'
    payload = Column(_JSONType, nullable=False, default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


Index(
    "apollo_kg_negotiations_attempt_entry_idx",
    KGNegotiation.attempt_id,
    KGNegotiation.entry_id,
)


# ===========================================================================
# WU-3A learner model (migration 026). Layer-1 skill inventory + Layer-3
# learner model + grading-core audit tables. Following the repo convention,
# these declare NO DB CHECK constraints — the migration SQL is the authority;
# the ORM is the typed Python access layer. The schema EXISTS as of WU-3A but
# nothing reads/writes these tables yet (resolver = WU-3C, seed = WU-3B, §3
# update math + §6 grading core + §8A cutover = later units).
# ===========================================================================


class KGEntity(Base):
    """Layer-1 course-scoped skill inventory (spec §2). Course ownership is
    inherited via concept_id -> apollo_concepts -> apollo_subjects.search_space_id.
    canonical_key is unique PER CONCEPT, not global (§1.4)."""

    __tablename__ = "apollo_kg_entities"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("apollo_concepts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    canonical_key = Column(Text, nullable=False)
    # kind is constrained by a SQL CHECK (mirror: ENTITY_KINDS). TEXT here.
    kind = Column(Text, nullable=False)
    display_name = Column(Text, nullable=False)
    payload = Column(_JSONType, nullable=False, default=dict)
    aliases = Column(_JSONType, nullable=False, default=list)
    # WU-3B2a / migration 030: the dedup ladder's embedding SOURCE (authored at
    # mint from display_name + canonical symbols; embedded ON THE FLY by 3B2c).
    # Nullable; NO persisted vector column (no pgvector-on-entities migration).
    scope_summary = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("concept_id", "canonical_key", name="apollo_kg_entities_concept_key_uniq"),
    )


class EntityPrereq(Base):
    """Prerequisite DAG edges between Layer-1 entities (spec §2).

    LEGACY LAYER-1 EXCEPTION: ``from_entity_id`` depends on ``to_entity_id``
    (dependent -> prerequisite). Do not conflate these persistence rows with KG
    or canonical DEPENDS_ON edges, whose direction is prerequisite -> dependent.
    Migrating existing Layer-1 rows is explicitly outside DAG-0 scope.

    Composite PK (from_entity_id, to_entity_id).
    """

    __tablename__ = "apollo_entity_prereqs"

    from_entity_id = Column(
        BigInteger,
        ForeignKey("apollo_kg_entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    to_entity_id = Column(
        BigInteger,
        ForeignKey("apollo_kg_entities.id", ondelete="CASCADE"),
        primary_key=True,
    )


class LearnerState(Base):
    """Layer-3 current per-(user, course, entity) belief snapshot, updated in
    place at Done (spec §3). Composite PK enforces one row per learner per entity
    per course. The belief array CHECK (length 3) lives in the migration SQL."""

    __tablename__ = "apollo_learner_state"

    # user_id FKs auth.users(id) ON DELETE CASCADE in the migration SQL. The ORM
    # declares no FK here because auth.users (Supabase-managed) is not in
    # Base.metadata — same convention as ApolloSession.user_id.
    user_id = Column(UUID(as_uuid=False), primary_key=True)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    entity_id = Column(
        BigInteger,
        ForeignKey("apollo_kg_entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    belief = Column(_RealArrayType, nullable=False)
    mastery = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    misconception_code = Column(Text, nullable=True)
    evidence_count = Column(Integer, nullable=False, default=0)
    last_evidence_at = Column(TIMESTAMP(timezone=True), nullable=True)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


# Campaign-plan Task B3 / migration 035: the composite PK's leading column is
# user_id, so a classroom-wide "every learner in this course" scan (the
# mastery-heatmap query) cannot use it. This mirrors
# ix_grading_artifacts_space_concept_time's shape for apollo_grading_artifacts.
Index(
    "ix_apollo_learner_state_space_entity",
    LearnerState.search_space_id,
    LearnerState.entity_id,
)


class MasteryEvent(Base):
    """Layer-3 append-only longitudinal evidence log AND refit corpus (spec
    §2/§3). attempt_id is ON DELETE SET NULL: the log outlives a deleted attempt.
    The (attempt_id, entity_id, event_kind) UNIQUE uses NULLS NOT DISTINCT so a
    retry with a NULL attempt_id cannot double-insert (PG15+)."""

    __tablename__ = "apollo_mastery_events"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    # No ORM FK on user_id (auth.users is Supabase-managed, not in Base.metadata);
    # the migration SQL declares the FK + ON DELETE CASCADE.
    user_id = Column(UUID(as_uuid=False), nullable=False)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_id = Column(
        BigInteger,
        ForeignKey("apollo_kg_entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    concept_problem_id = Column(
        BigInteger,
        ForeignKey("apollo_concept_problems.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_kind = Column(Text, nullable=False)
    score = Column(Float, nullable=True)
    misconception_code = Column(Text, nullable=True)
    parser_confidence = Column(Float, nullable=True)
    grader_confidence = Column(Float, nullable=True)
    negotiation_move = Column(Text, nullable=True)
    reference_step_id = Column(Text, nullable=True)
    prior_belief = Column(_RealArrayType, nullable=False)
    posterior_belief = Column(_RealArrayType, nullable=False)
    mastery_after = Column(Float, nullable=False)
    dt_days_since_last = Column(Float, nullable=True)
    evidence_node_ids = Column(_JSONType, nullable=False, default=list)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "entity_id",
            "event_kind",
            name="apollo_mastery_events_attempt_entity_kind_uniq",
            postgresql_nulls_not_distinct=True,
        ),
    )


class GraphComparisonRun(Base):
    """One row per Done-time comparison; makes the grader auditable (spec §2/§6).
    UNIQUE (attempt_id, comparison_version) lets a re-run at the same version be a
    supersede (delete-then-reinsert), not a crash."""

    __tablename__ = "apollo_graph_comparison_runs"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # No ORM FK on user_id (auth.users is Supabase-managed, not in Base.metadata);
    # the migration SQL declares the FK + ON DELETE CASCADE.
    user_id = Column(UUID(as_uuid=False), nullable=False)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    coverage_score = Column(Float, nullable=False)
    soundness_score = Column(Float, nullable=False)
    bisimilarity_score = Column(Float, nullable=False)
    # D5/D6: False iff the misconception bank was empty/absent for this concept.
    # Then soundness_score / bisimilarity_score hold the COVERAGE-ONLY fallback
    # (NOT NULL invariant preserved, bisimilarity.py:5-6) and contradiction_score
    # is NULL. Readers MUST consult this before trusting/averaging soundness.
    soundness_applicable = Column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    node_coverage_score = Column(Float, nullable=True)
    edge_coverage_score = Column(Float, nullable=True)
    scoping_score = Column(Float, nullable=True)
    usage_score = Column(Float, nullable=True)
    procedure_order_score = Column(Float, nullable=True)
    dependency_score = Column(Float, nullable=True)
    contradiction_score = Column(Float, nullable=True)
    normalization_confidence = Column(Float, nullable=False)
    abstained = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    abstention_reasons = Column(_JSONType, nullable=False, default=list)
    comparison_version = Column(Text, nullable=False)
    reference_graph_hash = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "comparison_version",
            name="apollo_graph_comparison_runs_attempt_version_uniq",
        ),
    )


class GraphComparisonFinding(Base):
    """Structured evidence behind every comparison score; diagnostics read THIS,
    not the transcript (spec §2/§6). entity_id ON DELETE SET NULL: a pruned entity
    must not delete the audit finding."""

    __tablename__ = "apollo_graph_comparison_findings"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    run_id = Column(
        BigInteger,
        ForeignKey("apollo_graph_comparison_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_id = Column(
        BigInteger,
        ForeignKey("apollo_kg_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    finding_kind = Column(Text, nullable=False)
    score = Column(Float, nullable=True)
    confidence = Column(Float, nullable=True)
    student_node_ids = Column(_JSONType, nullable=False, default=list)
    reference_node_ids = Column(_JSONType, nullable=False, default=list)
    student_edge_ids = Column(_JSONType, nullable=False, default=list)
    reference_edge_ids = Column(_JSONType, nullable=False, default=list)
    evidence_spans = Column(_JSONType, nullable=False, default=list)
    message = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


# ===========================================================================
# WU-3B2a auto-provisioning substrate (migration 030). §8B materials->Apollo
# observability + work-queue tables. Following the repo convention these declare
# NO DB CHECK constraints — the migration SQL is the authority (status/state/
# method/verdict CHECKs live in SQL only); the ORM is the typed Python access
# layer. The schema EXISTS as of WU-3B2a but the pipeline that reads/writes these
# tables lands in 3B2b-3B2h (jobs drained by 3B2f; runs/rejected/dedup/errors
# written by 3B2g). The course-isolation invariant (§1.4) is carried by the
# NOT NULL search_space_id FK on every table (a course delete cascades its rows).
# ===========================================================================


class IngestRun(Base):
    """Per-document auto-provisioning run: scrape/promote/reject/merge counts plus
    LLM call/token/cost aggregates (migration 030, §8B) and the mint-time
    dedup-pressure gauge (migration 043). ``content_hash`` lets an unchanged
    re-upload short-circuit (3B2g). ``status`` vocabulary is
    queued/running/succeeded/failed — DISTINCT from the job ``state`` vocabulary
    (the CHECK is in SQL only; the ORM declares none, repo convention)."""

    __tablename__ = "apollo_ingest_runs"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable since WU-AAS observability (migration 036): the authored-set path
    # opens the run BEFORE indexing so an OCR/indexing failure (bad PDF, no chunks
    # produced) still leaves a run row — at that point no document has been minted
    # yet, so document_id is stamped only once problem indexing succeeds. The queue
    # worker path always sets it.
    document_id = Column(BigInteger, nullable=True)
    content_hash = Column(Text, nullable=True)
    status = Column(Text, nullable=False, server_default=text("'queued'"), default="queued")
    n_pages = Column(Integer, nullable=False, server_default=text("0"), default=0)
    n_questions_scraped = Column(Integer, nullable=False, server_default=text("0"), default=0)
    n_promoted = Column(Integer, nullable=False, server_default=text("0"), default=0)
    n_rejected = Column(Integer, nullable=False, server_default=text("0"), default=0)
    n_dedup_merged = Column(Integer, nullable=False, server_default=text("0"), default=0)
    dedup_pressure = Column(
        _JSONType,
        nullable=False,
        # server_default stays '{}' — a richer JSON literal here would need its
        # colons escaped for text() (":0" parses as a bind param and renders
        # DEFAULT '..."total_candidates"NULL...'); readers .get() every key.
        server_default=text("'{}'::jsonb"),
        default=lambda: {
            "total_candidates": 0,
            "exact_merges": 0,
            "embedding_merges": 0,
            "embedding_distinct": 0,
            "embedding_merge_ratio": 0.0,
            "per_concept": {},
        },
    )
    llm_calls = Column(Integer, nullable=False, server_default=text("0"), default=0)
    llm_tokens_in = Column(BigInteger, nullable=False, server_default=text("0"), default=0)
    llm_tokens_out = Column(BigInteger, nullable=False, server_default=text("0"), default=0)
    llm_cost_usd = Column(Numeric(12, 6), nullable=False, server_default=text("0"), default=0)
    started_at = Column(TIMESTAMP(timezone=True), nullable=True)
    finished_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class ProvisioningJob(Base):
    """The SKIP-LOCKED auto-provisioning work queue (migration 030, §8B), mirroring
    the TeacherUploadJob lease shape. ``state`` vocabulary is
    pending/running/completed/failed — DISTINCT from the run ``status`` (CHECK in
    SQL only). The partial-unique-index that collapses two OPEN jobs per document
    is migration-only (NOT declared in the ORM, same as 028's partial index)."""

    __tablename__ = "apollo_provisioning_jobs"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id = Column(BigInteger, nullable=False)
    state = Column(Text, nullable=False, server_default=text("'pending'"), default="pending")
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("apollo_ingest_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    lease_owner = Column(Text, nullable=True)
    lease_expires_at = Column(TIMESTAMP(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, server_default=text("0"), default=0)
    last_error = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class RejectedProblem(Base):
    """A gate failure + diagnostic + the rejected payload (migration 030, §8B).
    ``concept_id`` ON DELETE SET NULL: a pruned concept must not delete the audit
    row. ``rejected_stage`` / ``failed_gate`` vocabularies are open (no SQL CHECK);
    the ORM declares no DB CHECK (repo convention)."""

    __tablename__ = "apollo_rejected_problems"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("apollo_ingest_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("apollo_concepts.id", ondelete="SET NULL"),
        nullable=True,
    )
    failed_gate = Column(SmallInteger, nullable=True)
    rejected_stage = Column(Text, nullable=False)
    diagnostic = Column(Text, nullable=False)
    payload = Column(_JSONType, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class DedupDecision(Base):
    """method + similarity + verdict for every dedup resolution (migration 030,
    §8B). ``similarity`` is NULL for the slug exact-match tier. ``method`` /
    ``verdict`` CHECKs live in SQL only; the ORM declares none (repo convention)."""

    __tablename__ = "apollo_dedup_decisions"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("apollo_ingest_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("apollo_concepts.id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_key = Column(Text, nullable=False)
    method = Column(Text, nullable=False)
    similarity = Column(Float, nullable=True)  # REAL on Postgres
    verdict = Column(Text, nullable=False)
    matched_entity_id = Column(
        BigInteger,
        ForeignKey("apollo_kg_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class IngestError(Base):
    """stage + class + context for any non-terminal pipeline error (migration 030,
    §8B). ``stage`` / ``error_class`` vocabularies are open (no SQL CHECK); the ORM
    declares no DB CHECK (repo convention)."""

    __tablename__ = "apollo_ingest_errors"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("apollo_ingest_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage = Column(Text, nullable=False)
    error_class = Column(Text, nullable=False)
    context = Column(_JSONType, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class IngestPageEvidence(Base):
    """Per-page OCR evidence for one authored-set ingest run (migration 036,
    WU-AAS observability). The audit-facing projection of the transient OCR pass:
    one row per source page per run, carrying the recognized ``ocr_text`` + the
    self-reported ``ocr_confidence`` + ``extraction_mode`` so the S2 ingestion
    audit reads REAL inputs instead of thin/absent ones. ``role`` is problem |
    solution (an authored set pairs two documents). ``verify_path_fired`` records
    that this page tripped the low-confidence threshold — the signal the S2
    contract reads to know an OCR-suspect page WOULD route through the extra
    generated-vs-extracted verification. Append-only; rows CASCADE from the run.
    ``document_id`` declares no ORM FK (aita_documents is owned by database.models
    and the ON DELETE CASCADE on aita_chunks/document teardown is handled at the
    authored-set delete path)."""

    __tablename__ = "apollo_ingest_page_evidence"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("apollo_ingest_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable: a page captured before the failure path minted a document (e.g. a
    # page-bearing-but-chunkless PDF that raised "no chunks produced") still gets
    # its OCR evidence persisted, with no document id to attribute it to.
    document_id = Column(BigInteger, nullable=True)
    role = Column(Text, nullable=False)  # 'problem' | 'solution' (open enum, no SQL CHECK)
    page_number = Column(Integer, nullable=True)
    ocr_text = Column(Text, nullable=False, server_default=text("''"), default="")
    ocr_confidence = Column(Float, nullable=True)  # REAL on Postgres; self-reported [0,1]
    extraction_mode = Column(Text, nullable=True)
    verify_path_fired = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class GradingArtifact(Base):
    """Canonical grading artifact (spec 2026-07-01 §1) — one immutable row per
    Done-click per grader role. ``role='canonical'`` is the record of the grade
    the student was served; ``role='pair'`` is the other grader's artifact
    captured on the same input (paired-artifact contract, spec §5). Append-only:
    no update path exists in code; retuning weights affects future artifacts
    only. ``user_id`` declares NO ORM FK (auth.users is Supabase-managed,
    absent from Base.metadata), mirroring Clarification/GraphComparisonRun."""

    __tablename__ = "apollo_grading_artifacts"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(Text, nullable=False)
    grader_used = Column(Text, nullable=False)
    user_id = Column(UUID(as_uuid=False), nullable=False)
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("apollo_concepts.id", ondelete="SET NULL"),
        nullable=True,
    )
    problem_id = Column(Text, nullable=False)
    versions = Column(_JSONType, nullable=False)
    node_ledger = Column(_JSONType, nullable=False)
    edge_ledger = Column(_JSONType, nullable=False)
    misconceptions = Column(_JSONType, nullable=False, default=list)
    clarification_trace = Column(_JSONType, nullable=False, default=list)
    scores = Column(_JSONType, nullable=False)
    abstention = Column(_JSONType, nullable=True)
    grading_latency_ms = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "role",
            name="uq_grading_artifact_attempt_role",
        ),
    )


class MisconceptionObservation(Base):
    """Emergent misconception store — append-only observation ledger (design
    memo 2026-07-05, increment 1; migration 037). ONE row per
    ``(attempt_id, signature)`` derived from a ``role='canonical'`` grading
    artifact's asserted misconceptions when the ``APOLLO_EMERGENT_MISCONCEPTIONS``
    flag is ON. The ledger is the source of truth; trust is derived on read
    (``apollo.emergent.store``), never stored.

    ``signature`` is the artifact misconception's ``canonical_key`` (the strong,
    promotable case), or the per-concept ``unkeyed:<concept_id>`` bucket for a
    free-form (``opposes=None``, no key) misconception, which accumulates but
    NEVER promotes (memo §4 / OQ1). ``UNIQUE(attempt_id, signature)`` mirrors
    ``MasteryEvent``'s idempotency: a re-grade / retry of the same attempt cannot
    double-count. ``user_id`` declares NO ORM FK (auth.users is Supabase-managed,
    absent from Base.metadata), mirroring GradingArtifact/GraphComparisonRun."""

    __tablename__ = "apollo_misconception_observations"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    search_space_id = Column(
        Integer,
        ForeignKey("aita_search_spaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("apollo_concepts.id", ondelete="SET NULL"),
        nullable=True,
    )
    signature = Column(Text, nullable=False)
    user_id = Column(UUID(as_uuid=False), nullable=False)
    attempt_id = Column(
        BigInteger,
        ForeignKey("apollo_problem_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    confidence = Column(Float, nullable=True)
    opposes = Column(Text, nullable=True)
    evidence_span = Column(Text, nullable=True)
    # 'grading_artifact' (role=canonical Done-grade ledger feed, unchanged) |
    # 'detector_unkeyed' (emergent-map birth signal — judge confidently wrong
    # at a keyed reference node it cannot name) | 'clarification_refuted'
    # (emergent-map upgrade signal — a clarification rescore confirms the
    # misconception). Domain enforced by ck_misconception_obs_source
    # (migration 040; emergent-map design 2026-07-10 §5.3/§5.4).
    source = Column(
        Text, nullable=False, server_default=text("'grading_artifact'"), default="grading_artifact"
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "signature",
            name="uq_misconception_observation_attempt_signature",
        ),
    )


# Read path (apollo.emergent.store) scans the ledger for one class+concept and
# groups by signature. The PK leads with id, so a (search_space_id, concept_id)
# scan cannot use it — this index supports it, mirroring
# ix_grading_artifacts_space_concept_time's shape.
Index(
    "ix_apollo_misconception_obs_space_concept",
    MisconceptionObservation.search_space_id,
    MisconceptionObservation.concept_id,
)
