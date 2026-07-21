"""SQLAlchemy models for Apollo persistence (Postgres only).

V3: KG entries moved to Neo4j (apollo.persistence.neo4j_client +
apollo.knowledge_graph.store). Postgres now owns only:
  app.learning_activities, app.tutoring_messages, app.problem_attempts,
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
from sqlalchemy.orm import relationship, synonym, validates

from database.models import Base, LearningActivity

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
_TextArrayType = ARRAY(Text).with_variant(JSON(), "sqlite")

# App-layer mirror of the app.learner_entities.kind CHECK in
# supabase/migrations/20260717035041_create_app_schema_v1.sql (the DDL is the
# schema authority; this tuple is asserted equal to it by
# apollo/persistence/tests/test_learner_model_allowlists.py). 'misconception' is
# NOT in the target CHECK (dropped per DB-13/A6 cleanup) — misconceptions are
# tracked via MasteryEvent.event_kind, not as an entity kind. The other two enum
# tuples below are OPEN (no SQL CHECK) — documentation tuples, NOT asserted
# against SQL.
ENTITY_KINDS = (
    "concept",
    "equation",
    "condition",
    "definition",
    "procedure",
    "variable",
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


class Concept(Base):
    """Course-scoped curriculum concept with its subject label folded in."""

    __tablename__ = "concepts"
    __table_args__ = (
        UniqueConstraint(
            "course_id", "subject_slug", "slug", name="concepts_course_subject_slug_key"
        ),
        {"schema": "app"},
    )

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subject_slug = Column(Text, nullable=False)
    subject_display_name = Column(Text, nullable=False)
    slug = Column(Text, nullable=False)
    display_name = Column(Text, nullable=False)
    # Migration 038 (reversed provisioning): the closed-list concept-matcher
    # prompt line is "slug — display_name: description". Empty for legacy rows.
    description = Column(Text, nullable=False, default="")
    canonical_symbols = Column(_TextArrayType, nullable=False, default=list)
    symbol_metadata = Column(_JSONType, nullable=False, default=dict)
    normalization_map = Column(_JSONType, nullable=False, default=dict)
    parser_prompt_template = Column(Text, nullable=False, default="")
    solver_config = Column(_JSONType, nullable=False, default=dict)
    parser_config = Column(_JSONType, nullable=False, default=dict)
    forbidden_named_laws = Column(_TextArrayType, nullable=False, default=list)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class Problem(Base):
    """Typed problem-bank row with stable Pydantic fields promoted from JSON."""

    __tablename__ = "problems"
    __table_args__ = (
        UniqueConstraint("concept_id", "problem_code", name="problems_concept_code_key"),
        {"schema": "app"},
    )

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("app.concepts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    problem_code = Column(Text, nullable=False)
    difficulty = Column(Text, nullable=False)
    problem_text = Column(Text, nullable=False)
    given_values = Column(_JSONType, nullable=False, default=dict)
    target_unknown = Column(Text, nullable=False, default="")
    reference_solution = Column(_JSONType, nullable=False)
    payload_extra = Column(_JSONType, nullable=False, default=dict)
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
    provenance = Column(_JSONType, nullable=False, server_default=text("'{}'"), default=dict)
    quarantined_at = Column(TIMESTAMP(timezone=True), nullable=True)  # WU-3B2h filter; NULL = live
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    @classmethod
    def from_pydantic_payload(
        cls,
        payload: dict,
        *,
        course_id: int,
        concept_id: int,
        **typed_columns,
    ) -> Problem:
        """Validate a public problem payload and split it into target columns."""
        from apollo.schemas.problem import Problem as ProblemSchema

        validated = ProblemSchema.model_validate(payload)
        known = set(ProblemSchema.model_fields) | {"database_id"}
        return cls(
            course_id=course_id,
            concept_id=concept_id,
            problem_code=validated.id,
            difficulty=validated.difficulty,
            problem_text=validated.problem_text,
            given_values=dict(validated.given_values),
            target_unknown=validated.target_unknown,
            reference_solution={
                "version": 1,
                "steps": [step.model_dump(mode="json") for step in validated.reference_solution],
            },
            payload_extra={key: value for key, value in payload.items() if key not in known},
            **typed_columns,
        )

    @classmethod
    def from_inventory_payload(
        cls,
        payload: dict,
        *,
        course_id: int,
        concept_id: int,
        **typed_columns,
    ) -> Problem:
        """Split a tier-1 draft, which intentionally has no solution steps yet."""
        known = {
            "id",
            "concept_id",
            "difficulty",
            "problem_text",
            "given_values",
            "target_unknown",
            "reference_solution",
        }
        reference_solution = payload.get("reference_solution") or []
        if isinstance(reference_solution, dict):
            document = dict(reference_solution)
        else:
            document = {"version": 1, "steps": list(reference_solution)}
        return cls(
            course_id=course_id,
            concept_id=concept_id,
            problem_code=str(payload["id"]),
            difficulty=str(payload["difficulty"]),
            problem_text=str(payload["problem_text"]),
            given_values=dict(payload.get("given_values") or {}),
            target_unknown=str(payload.get("target_unknown") or ""),
            reference_solution=document,
            payload_extra={key: value for key, value in payload.items() if key not in known},
            **typed_columns,
        )

    def apply_pydantic_payload(self, payload: dict) -> None:
        """Validate and replace the public/payload portion of an existing row."""
        replacement = type(self).from_pydantic_payload(
            payload,
            course_id=int(self.course_id),
            concept_id=int(self.concept_id),
        )
        for field in (
            "problem_code",
            "difficulty",
            "problem_text",
            "given_values",
            "target_unknown",
            "reference_solution",
            "payload_extra",
        ):
            setattr(self, field, getattr(replacement, field))

    def apply_inventory_payload(self, payload: dict) -> None:
        """Replace draft fields without requiring a solution before promotion."""
        replacement = type(self).from_inventory_payload(
            payload,
            course_id=int(self.course_id),
            concept_id=int(self.concept_id),
        )
        for field in (
            "problem_code",
            "difficulty",
            "problem_text",
            "given_values",
            "target_unknown",
            "reference_solution",
            "payload_extra",
        ):
            setattr(self, field, getattr(replacement, field))

    def to_pydantic_payload(self, *, concept_slug: str) -> dict:
        """Reassemble the established public Pydantic shape without DB identities."""
        document = dict(self.reference_solution or {})
        return {
            **dict(self.payload_extra or {}),
            "id": str(self.problem_code),
            "concept_id": concept_slug,
            "difficulty": str(self.difficulty),
            "problem_text": str(self.problem_text),
            "given_values": dict(self.given_values or {}),
            "target_unknown": str(self.target_unknown or ""),
            "reference_solution": list(document.get("steps") or []),
        }

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


class ProvisioningRun(Base):
    """One teacher-gated authored-set or generation provisioning run.

    Callers use the kind-specific factories below instead of assembling a
    discriminator plus a nullable field bag themselves.
    """

    __tablename__ = "provisioning_runs"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    search_space_id = Column(
        "course_id",
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind = Column(Text, nullable=False)
    set_index = Column(Integer, nullable=True)
    problem_document_id = Column(
        BigInteger, ForeignKey("app.documents.id", ondelete="RESTRICT"), nullable=True
    )
    solution_document_id = Column(
        BigInteger, ForeignKey("app.documents.id", ondelete="RESTRICT"), nullable=True
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("app.concepts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("internal.content_ingest_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
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
        Index(
            "provisioning_runs__authored_course_set__uidx",
            "course_id",
            "set_index",
            unique=True,
            postgresql_where=text("kind = 'authored_set'"),
            sqlite_where=text("kind = 'authored_set'"),
        ),
        {"schema": "app"},
    )

    @classmethod
    def authored_set(
        cls,
        *,
        search_space_id: int,
        set_index: int,
        problem_document_id: int | None = None,
        solution_document_id: int | None = None,
        status: str = "pending",
        result_summary: dict | None = None,
    ) -> ProvisioningRun:
        if set_index < 0:
            raise ValueError("authored-set set_index must be non-negative")
        if status not in {"pending", "indexing", "provisioning", "done", "failed"}:
            raise ValueError(f"invalid authored-set status: {status}")
        return cls(
            kind="authored_set",
            search_space_id=search_space_id,
            set_index=set_index,
            problem_document_id=problem_document_id,
            solution_document_id=solution_document_id,
            status=status,
            result_summary={} if result_summary is None else result_summary,
        )

    @classmethod
    def generation(
        cls,
        *,
        search_space_id: int,
        concept_id: int,
        status: str = "pending",
        result_summary: dict | None = None,
        ingest_run_id: int | None = None,
    ) -> ProvisioningRun:
        if status not in {"pending", "running", "succeeded", "failed"}:
            raise ValueError(f"invalid generation status: {status}")
        return cls(
            kind="generation",
            search_space_id=search_space_id,
            concept_id=concept_id,
            status=status,
            result_summary={} if result_summary is None else result_summary,
            ingest_run_id=ingest_run_id,
        )


class QuestionOpportunity(Base):
    """Question and durable tally state for one reference node per attempt."""

    __tablename__ = "question_opportunities"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "reference_node_id",
            name="question_opportunities_attempt_id_reference_node_id_key",
        ),
        Index(
            "question_opportunities__course_session__idx",
            "course_id",
            "learning_activity_id",
        ),
        Index("question_opportunities__activity_id__idx", "learning_activity_id"),
        {"schema": "app"},
    )

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id = Column(
        "learning_activity_id",
        BigInteger,
        ForeignKey("app.learning_activities.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("app.problem_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    reference_node_id = Column(Text, nullable=False)
    state = Column(Text, nullable=False, server_default=text("'asked_waiting'"))
    question = Column(Text, nullable=False, server_default=text("''"), default="")
    asked_turn = Column(Integer, nullable=True)
    answered_turn = Column(Integer, nullable=True)
    evidence = Column(_JSONType, nullable=False, server_default=text("'[]'"), default=list)
    student_declined = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    times_asked = Column(Integer, nullable=False, server_default=text("0"), default=0)
    last_asked_turn = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class TutoringSession(LearningActivity):
    """Tutoring-modality view of app.learning_activities."""

    __mapper_args__ = {"polymorphic_identity": "tutoring"}

    # Preserve the established Apollo Python name while mapping the physical
    # target column course_id.
    search_space_id = synonym("course_id")

    messages = relationship(
        "TutoringMessage", back_populates="session", cascade="all, delete-orphan"
    )
    problem_attempts = relationship(
        "ProblemAttempt", back_populates="session", cascade="all, delete-orphan"
    )


def promote_tutoring_message_metadata(
    value: dict | None,
) -> tuple[dict, bool, str | None]:
    """Split stable legacy metadata signals into target typed columns."""
    metadata = dict(value or {})
    low_confidence_pattern = bool(metadata.pop("low_conf_pattern", False))
    raw_intent = metadata.pop("intent", None)
    intent = raw_intent if isinstance(raw_intent, str) else None
    return metadata, low_confidence_pattern, intent


class TutoringMessage(Base):
    __tablename__ = "tutoring_messages"
    __table_args__ = (
        UniqueConstraint("learning_activity_id", "turn_index"),
        {"schema": "app"},
    )

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id = Column(
        "learning_activity_id",
        BigInteger,
        ForeignKey("app.learning_activities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("app.problem_attempts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    role = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    turn_index = Column(Integer, nullable=False)
    low_confidence_pattern = Column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    intent = Column(Text, nullable=True)
    message_metadata = Column(
        "metadata", _JSONType, nullable=False, default=dict, server_default=text("'{}'")
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    session = relationship("TutoringSession", back_populates="messages")

    def __init__(self, **kwargs):
        metadata, low_confidence_pattern, intent = promote_tutoring_message_metadata(
            kwargs.pop("message_metadata", None)
        )
        kwargs["message_metadata"] = metadata
        kwargs.setdefault("low_confidence_pattern", low_confidence_pattern)
        kwargs.setdefault("intent", intent)
        super().__init__(**kwargs)


class ProblemAttempt(Base):
    __tablename__ = "problem_attempts"
    __table_args__ = {"schema": "app"}

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(UUID(as_uuid=False), nullable=False, index=True)
    session_id = Column(
        "learning_activity_id",
        BigInteger,
        ForeignKey("app.learning_activities.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    problem_id = Column(BigInteger, nullable=False)
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
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    session = relationship("TutoringSession", back_populates="problem_attempts")


Index(
    "learning_activities__active_tutoring_user_course__uidx",
    TutoringSession.user_id,
    TutoringSession.course_id,
    unique=True,
    postgresql_where=(
        (TutoringSession.status == "active") & (TutoringSession.modality == "tutoring")
    ),
    sqlite_where=(
        (TutoringSession.status == "active") & (TutoringSession.modality == "tutoring")
    ),
)


class StudentProgress(Base):
    __tablename__ = "student_progress"
    __table_args__ = {"schema": "app"}

    user_id = Column(UUID(as_uuid=False), primary_key=True)
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        primary_key=True,
    )
    xp_total = Column(Integer, nullable=False, default=0)
    level = Column(Integer, nullable=False, default=1)
    last_level_up_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


# ===========================================================================
# WU-3A learner model (migration 026 historically; target DDL now
# supabase/migrations/20260717035041_create_app_schema_v1.sql). Layer-1 skill
# inventory + Layer-3 learner model. Following the repo convention, these
# declare NO DB CHECK constraints — the migration SQL is the authority; the
# ORM is the typed Python access layer.
#
# DB-13/A6 (.planning/cleanup/db-plan-amendments-2026-07-20.md): the
# apollo_kg_negotiations audit table + KGNegotiation model are REMOVED from
# the target — the three negotiate KG mutations (challenge/paraphrase/skip)
# keep working via apollo.knowledge_graph.store.KGStore, only the Postgres
# audit rows are gone.
# ===========================================================================


class LearnerEntity(Base):
    """Layer-1 course-scoped skill inventory (spec §2, retargeted onto
    app.learner_entities by DB-13). Course ownership was previously inherited
    via ``concept_id -> app.concepts.course_id`` only; the target DDL adds a
    denormalized ``course_id`` column directly (initplan-safe RLS, matches the
    rest of the app schema). ``canonical_key`` is unique per concept, not
    global (§1.4)."""

    __tablename__ = "learner_entities"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("app.concepts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    canonical_key = Column(Text, nullable=False)
    # kind is constrained by a SQL CHECK (mirror: ENTITY_KINDS). TEXT here.
    kind = Column(Text, nullable=False)
    display_name = Column(Text, nullable=False)
    payload = Column(_JSONType, nullable=False, default=dict)
    # DB-13: TEXT[] on the target DDL (was JSONB pre-cleanup).
    aliases = Column(_TextArrayType, nullable=False, default=list)
    # WU-3B2a / migration 030: the dedup ladder's embedding SOURCE (authored at
    # mint from display_name + canonical symbols; embedded ON THE FLY by 3B2c).
    # Nullable; NO persisted vector column (no pgvector-on-entities migration).
    scope_summary = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint("concept_id", "canonical_key", name="learner_entities_concept_key_uniq"),
        {"schema": "app"},
    )


class EntityPrereq(Base):
    """Prerequisite DAG edges between Layer-1 entities (spec §2, retargeted
    onto internal.entity_prerequisites by DB-13).

    LEGACY LAYER-1 EXCEPTION: ``from_entity_id`` depends on ``to_entity_id``
    (dependent -> prerequisite). Do not conflate these persistence rows with KG
    or canonical DEPENDS_ON edges, whose direction is prerequisite -> dependent.
    Migrating existing Layer-1 rows is explicitly outside DAG-0 scope.

    Composite PK (from_entity_id, to_entity_id). The target DDL adds a
    denormalized ``course_id`` column (not previously in the ORM).
    """

    __tablename__ = "entity_prerequisites"

    course_id = Column(
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_entity_id = Column(
        BigInteger,
        ForeignKey("app.learner_entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    to_entity_id = Column(
        BigInteger,
        ForeignKey("app.learner_entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = ({"schema": "internal"},)


class LearnerState(Base):
    """Layer-3 current per-(user, course, entity) belief snapshot, updated in
    place at Done (spec §3). Composite PK enforces one row per learner per entity
    per course. The belief array CHECK (length 3) lives in the migration SQL.

    DB-13: retargeted onto app.learner_state; ``misconception_code`` is GONE
    from the target DDL (misconceptions are tracked via
    MasteryEvent.event_kind, not a mutable per-entity field)."""

    __tablename__ = "learner_state"

    # user_id FKs auth.users(id) ON DELETE CASCADE in the migration SQL. The ORM
    # declares no FK here because auth.users (Supabase-managed) is not in
    # Base.metadata — same convention as TutoringSession.user_id.
    user_id = Column(UUID(as_uuid=False), primary_key=True)
    # Python attribute name kept as `search_space_id` (pre-DB-13 name) mapped
    # onto the target DDL's `course_id` physical column — same trick as
    # DedupDecision/IngestError/IngestPageEvidence, minimizes caller churn
    # until the DB-13 caller-retarget sub-task lands.
    search_space_id = Column(
        "course_id",
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        primary_key=True,
    )
    entity_id = Column(
        BigInteger,
        ForeignKey("app.learner_entities.id", ondelete="CASCADE"),
        primary_key=True,
    )
    belief = Column(_RealArrayType, nullable=False)
    mastery = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    evidence_count = Column(Integer, nullable=False, default=0)
    last_evidence_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = ({"schema": "app"},)


# Campaign-plan Task B3 / migration 035: the composite PK's leading column is
# user_id, so a classroom-wide "every learner in this course" scan (the
# mastery-heatmap query) cannot use it. This mirrors
# ix_grading_artifacts_space_concept_time's shape for apollo_grading_artifacts.
Index(
    "ix_learner_state_space_entity",
    LearnerState.search_space_id,
    LearnerState.entity_id,
)


class MasteryEvent(Base):
    """Layer-3 append-only longitudinal evidence log AND refit corpus (spec
    §2/§3). attempt_id is ON DELETE SET NULL: the log outlives a deleted attempt.
    The (attempt_id, entity_id, event_kind) UNIQUE uses NULLS NOT DISTINCT so a
    retry with a NULL attempt_id cannot double-insert (PG15+).

    DB-13: retargeted onto app.mastery_events; ``misconception_code`` is GONE
    from the target DDL (tracked via ``event_kind`` instead);
    ``evidence_node_ids`` is TEXT[] (was JSONB pre-cleanup)."""

    __tablename__ = "mastery_events"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    # No ORM FK on user_id (auth.users is Supabase-managed, not in Base.metadata);
    # the migration SQL declares the FK + ON DELETE CASCADE.
    user_id = Column(UUID(as_uuid=False), nullable=False)
    # Python attribute name kept as `search_space_id` — see LearnerState.
    search_space_id = Column(
        "course_id",
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
    )
    entity_id = Column(
        BigInteger,
        ForeignKey("app.learner_entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("app.problem_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    concept_problem_id = Column(
        BigInteger,
        ForeignKey("app.problems.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_kind = Column(Text, nullable=False)
    score = Column(Float, nullable=True)
    parser_confidence = Column(Float, nullable=True)
    grader_confidence = Column(Float, nullable=True)
    negotiation_move = Column(Text, nullable=True)
    reference_step_id = Column(Text, nullable=True)
    prior_belief = Column(_RealArrayType, nullable=False)
    posterior_belief = Column(_RealArrayType, nullable=False)
    mastery_after = Column(Float, nullable=False)
    dt_days_since_last = Column(Float, nullable=True)
    evidence_node_ids = Column(_TextArrayType, nullable=False, default=list)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "entity_id",
            "event_kind",
            name="mastery_events__attempt_entity_kind__uniq",
            postgresql_nulls_not_distinct=True,
        ),
        {"schema": "app"},
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
        ForeignKey("app.problem_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # No ORM FK on user_id (auth.users is Supabase-managed, not in Base.metadata);
    # the migration SQL declares the FK + ON DELETE CASCADE.
    user_id = Column(UUID(as_uuid=False), nullable=False)
    search_space_id = Column(
        Integer,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
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
        ForeignKey("app.learner_entities.id", ondelete="SET NULL"),
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
# Authored-content ingest observability. Migration 030 originally also created
# an auto-provisioning queue and rejected-problem audit table; cleanup T-F
# removed their runtime/ORM models while retaining the synchronous authored-set
# ingest, dedup, and error records below.
# ===========================================================================


class IngestRun(Base):
    """Per-document auto-provisioning run: scrape/promote/reject/merge counts plus
    LLM call/token/cost aggregates (migration 030, §8B) and the mint-time
    dedup-pressure gauge (migration 043). ``content_hash`` lets an unchanged
    re-upload short-circuit (3B2g). ``status`` vocabulary is
    queued/running/succeeded/failed — DISTINCT from the job ``state`` vocabulary
    (the CHECK is in SQL only; the ORM declares none, repo convention)."""

    __tablename__ = "content_ingest_runs"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    search_space_id = Column(
        "course_id",
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable since WU-AAS observability (migration 036): the authored-set path
    # opens the run BEFORE indexing so an OCR/indexing failure (bad PDF, no chunks
    # produced) still leaves a run row — at that point no document has been minted
    # yet, so document_id is stamped only once problem indexing succeeds.
    document_id = Column(
        BigInteger, ForeignKey("app.documents.id", ondelete="SET NULL"), nullable=True
    )
    content_hash = Column(Text, nullable=True)
    status = Column(Text, nullable=False, server_default=text("'queued'"), default="queued")
    n_pages = Column(Integer, nullable=False, server_default=text("0"), default=0)
    n_questions_scraped = Column(Integer, nullable=False, server_default=text("0"), default=0)
    n_promoted = Column(Integer, nullable=False, server_default=text("0"), default=0)
    n_rejected = Column(Integer, nullable=False, server_default=text("0"), default=0)
    n_dedup_merged = Column(Integer, nullable=False, server_default=text("0"), default=0)
    dedup_total_candidates = Column(Integer, nullable=False, server_default=text("0"), default=0)
    dedup_exact_merges = Column(Integer, nullable=False, server_default=text("0"), default=0)
    dedup_embedding_merges = Column(Integer, nullable=False, server_default=text("0"), default=0)
    dedup_embedding_distinct = Column(Integer, nullable=False, server_default=text("0"), default=0)
    dedup_embedding_merge_ratio = Column(
        Float, nullable=False, server_default=text("0"), default=0.0
    )
    dedup_details = Column(
        _JSONType,
        nullable=False,
        # server_default stays '{}' — a richer JSON literal here would need its
        # colons escaped for text() (":0" parses as a bind param and renders
        # DEFAULT '..."total_candidates"NULL...'); readers .get() every key.
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    llm_calls = Column(Integer, nullable=False, server_default=text("0"), default=0)
    llm_tokens_in = Column(BigInteger, nullable=False, server_default=text("0"), default=0)
    llm_tokens_out = Column(BigInteger, nullable=False, server_default=text("0"), default=0)
    llm_cost_usd = Column(Numeric(12, 6), nullable=False, server_default=text("0"), default=0)
    started_at = Column(TIMESTAMP(timezone=True), nullable=True)
    finished_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = ({"schema": "internal"},)


class DedupDecision(Base):
    """method + similarity + verdict for every dedup resolution (migration 030,
    §8B). ``similarity`` is NULL for the slug exact-match tier. ``method`` /
    ``verdict`` CHECKs live in SQL only; the ORM declares none (repo convention)."""

    __tablename__ = "dedup_decisions"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("internal.content_ingest_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    search_space_id = Column(
        "course_id",
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("app.concepts.id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_key = Column(Text, nullable=False)
    method = Column(Text, nullable=False)
    similarity = Column(Float, nullable=True)  # REAL on Postgres
    verdict = Column(Text, nullable=False)
    matched_entity_id = Column(
        BigInteger,
        ForeignKey("app.learner_entities.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = ({"schema": "internal"},)


class IngestError(Base):
    """stage + class + context for any non-terminal pipeline error (migration 030,
    §8B). ``stage`` / ``error_class`` vocabularies are open (no SQL CHECK); the ORM
    declares no DB CHECK (repo convention)."""

    __tablename__ = "content_ingest_errors"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("internal.content_ingest_runs.id", ondelete="CASCADE"),
        nullable=True,
    )
    search_space_id = Column(
        "course_id",
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage = Column(Text, nullable=False)
    error_class = Column(Text, nullable=False)
    context = Column(_JSONType, nullable=False, server_default=text("'{}'::jsonb"), default=dict)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = ({"schema": "internal"},)


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
    ``document_id`` declares no ORM FK (app.documents is owned by database.models
    and the ON DELETE CASCADE on internal.document_chunks/document teardown is handled at the
    authored-set delete path)."""

    __tablename__ = "ingest_page_evidence"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    ingest_run_id = Column(
        BigInteger,
        ForeignKey("internal.content_ingest_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    search_space_id = Column(
        "course_id",
        BigInteger,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable: a page captured before the failure path minted a document (e.g. a
    # page-bearing-but-chunkless PDF that raised "no chunks produced") still gets
    # its OCR evidence persisted, with no document id to attribute it to.
    document_id = Column(
        BigInteger, ForeignKey("app.documents.id", ondelete="SET NULL"), nullable=True
    )
    role = Column(Text, nullable=False)  # 'problem' | 'solution' (open enum, no SQL CHECK)
    page_number = Column(Integer, nullable=True)
    ocr_text = Column(Text, nullable=False, server_default=text("''"), default="")
    ocr_confidence = Column(Float, nullable=True)  # REAL on Postgres; self-reported [0,1]
    extraction_mode = Column(Text, nullable=True)
    verify_path_fired = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    __table_args__ = ({"schema": "internal"},)


class GradingArtifact(Base):
    """Canonical grading artifact (spec 2026-07-01 §1) — one immutable row per
    Done-click per grader role. ``role='canonical'`` is the record of the grade
    the student was served; ``role='pair'`` is the other grader's artifact
    captured on the same input (paired-artifact contract, spec §5). Append-only:
    no update path exists in code; retuning weights affects future artifacts
    only. ``user_id`` declares NO ORM FK (auth.users is Supabase-managed,
    absent from Base.metadata), mirroring GraphComparisonRun."""

    __tablename__ = "apollo_grading_artifacts"

    id = Column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    attempt_id = Column(
        BigInteger,
        ForeignKey("app.problem_attempts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(Text, nullable=False)
    grader_used = Column(Text, nullable=False)
    user_id = Column(UUID(as_uuid=False), nullable=False)
    search_space_id = Column(
        Integer,
        ForeignKey("app.courses.id", ondelete="CASCADE"),
        nullable=False,
    )
    concept_id = Column(
        BigInteger,
        ForeignKey("app.concepts.id", ondelete="SET NULL"),
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
