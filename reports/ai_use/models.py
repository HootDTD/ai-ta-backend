"""ORM model + typed repository for AI-use reports (``app.ai_usage_reports``).

Replaces the legacy anon-key PostgREST CRUD (``vendors.supabase_client``) with
a typed SQLAlchemy repository using the caller's own request-scoped DB
session -- no anon REST fallback. ``user_id``/``course_id`` are NEVER
accepted from client input: the caller (``reports/ai_use/routes.py``)
derives ``user_id`` from the authenticated context and ``course_id`` from the
chat session that owns the report (the ``app.learning_activities`` row for
``chat_id``).

DDL authority: supabase/migrations/20260717035041_create_app_schema_v1.sql
(~line 519, ``app.ai_usage_reports``). Per repo convention (see
apollo/persistence/models.py's GradingRun/LearnerState/MasteryEvent et al.),
the ORM declares no CHECK constraints -- SQL is the CHECK authority, verified
against real Postgres in tests/database/test_app_schema_v1.py. ``user_id``
declares no ORM FK (``auth.users`` is Supabase-managed, absent from
``Base.metadata``); the migration SQL owns that FK + ON DELETE CASCADE.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, TIMESTAMP, BigInteger, Column, ForeignKey, Index, Text, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Base

# JSONB on Postgres, JSON on SQLite (route/unit tests use in-memory SQLite;
# mirrors the same with_variant convention in apollo/persistence/models.py).
_JSONType = JSONB().with_variant(JSON(), "sqlite")
_TextArrayType = ARRAY(Text).with_variant(JSON(), "sqlite")


class AIUsageReport(Base):
    """One AI-use report row (``app.ai_usage_reports``).

    Append-only from the app's perspective: no update path exists in code.
    ``chat_id`` FKs the chat's *external* identifier
    (``app.learning_activities.external_id``), not the internal bigint id --
    this replaces the retired ``ai_use_reports`` table's bare ``chat_id``
    text column with a real FK into the ``learning_activities`` world.
    """

    __tablename__ = "ai_usage_reports"
    __table_args__ = (
        Index("ai_usage_reports__user_course__idx", "user_id", "course_id"),
        Index("ai_usage_reports__chat_id__idx", "chat_id"),
        {"schema": "app"},
    )

    id = Column(UUID(as_uuid=False), primary_key=True)
    # No ORM FK on user_id (auth.users is Supabase-managed, not in
    # Base.metadata); the migration SQL declares the FK + ON DELETE CASCADE.
    user_id = Column(UUID(as_uuid=False), nullable=False)
    course_id = Column(
        BigInteger, ForeignKey("app.courses.id", ondelete="CASCADE"), nullable=False
    )
    chat_id = Column(
        Text,
        ForeignKey("app.learning_activities.external_id", ondelete="CASCADE"),
        nullable=False,
    )
    style = Column(Text, nullable=True)
    length = Column(Text, nullable=True)
    markdown = Column(Text, nullable=True)
    jsonld = Column(_JSONType, nullable=True)
    model_fingerprint = Column(Text, nullable=True)
    tool_calls = Column(_JSONType, nullable=True)
    prompt_hashes = Column(_TextArrayType, nullable=False, default=list)
    created_at = Column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


# -------------------- Repository --------------------
# Typed SQLAlchemy repository replacing the legacy PostgREST TABLE constant +
# create_report/get_report anon-REST CRUD. Every function takes the caller's
# own AsyncSession (request-scoped) -- there is no module-level Supabase
# client and no anon-key fallback path.


async def create_report(
    db: AsyncSession,
    *,
    user_id: str,
    course_id: int,
    chat_id: str,
    style: str | None = None,
    length: str | None = None,
    markdown: str | None = None,
    jsonld: Any | None = None,
    model_fingerprint: str | None = None,
    tool_calls: Any | None = None,
    prompt_hashes: list[str] | None = None,
) -> AIUsageReport:
    """Insert a new AI-use report and return the persisted row.

    ``user_id`` and ``course_id`` must come from the authenticated context and
    the owning chat session -- callers must never pass client-supplied values
    for either (see ``reports/ai_use/routes.py::_load_owned_chat_session``).
    """
    report = AIUsageReport(
        id=str(uuid_pkg.uuid4()),
        user_id=user_id,
        course_id=course_id,
        chat_id=chat_id,
        style=style,
        length=length,
        markdown=markdown,
        jsonld=jsonld,
        model_fingerprint=model_fingerprint,
        tool_calls=tool_calls,
        prompt_hashes=list(prompt_hashes or []),
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report


async def get_report_for_user(
    db: AsyncSession, *, report_id: str, user_id: str
) -> AIUsageReport | None:
    """Fetch a single report by id, scoped to its owner.

    Returns ``None`` both when the report doesn't exist AND when it belongs
    to a different user -- cross-user reads are refused, and deliberately
    indistinguishable from a plain 404 (no existence leak to non-owners).
    """
    stmt = select(AIUsageReport).where(
        AIUsageReport.id == report_id, AIUsageReport.user_id == user_id
    )
    result = await db.execute(stmt)
    return result.scalars().first()
