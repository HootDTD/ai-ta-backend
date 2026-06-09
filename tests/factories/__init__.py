"""Model factories for tests.

factory_boy's SQLAlchemy persistence is built around *sync* sessions, which
clashes with the project's async engine. To stay loop-safe we use plain
``factory.Factory`` builders (``.build()`` returns an unsaved ORM instance) and
persist explicitly via :func:`persist`, which any async test/fixture can await.

    space = await persist(db_session, SearchSpaceFactory.build())
    doc = await persist(db_session, AITADocumentFactory.build(search_space_id=space.id))

Phase 1 of docs/TESTING-CI-PLAN.md.
"""
from __future__ import annotations

from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories.models import (
    AITAChunkFactory,
    AITADocumentFactory,
    ChatSessionFactory,
    CourseMembershipFactory,
    SearchSpaceFactory,
    TeacherUploadFactory,
)

T = TypeVar("T")


async def persist(session: AsyncSession, instance: T) -> T:
    """Add ``instance`` to ``session`` and flush so it gets a primary key."""
    session.add(instance)
    await session.flush()
    return instance


__all__ = [
    "persist",
    "SearchSpaceFactory",
    "AITADocumentFactory",
    "AITAChunkFactory",
    "CourseMembershipFactory",
    "ChatSessionFactory",
    "TeacherUploadFactory",
]
