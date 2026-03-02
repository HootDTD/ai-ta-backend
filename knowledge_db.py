from __future__ import annotations

"""DB-backed knowledge manager using pgvector schema (aita_search_spaces / aita_documents).

Provides the same list_subjects / list_stores / register_store interface as
KnowledgeManager but reads from (and writes to) the aita_* tables instead of
Supabase REST API + FAISS files.

Used when USE_PGVECTOR_RETRIEVAL=true.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from .db import AITADocument, DocumentStatus, SearchSpace, get_async_session
from .store_weights import WEIGHT_KINDS

log = logging.getLogger(__name__)


class DBKnowledgeManager:
    """Knowledge manager backed by aita_search_spaces + aita_documents tables.

    Mirrors the public interface of KnowledgeManager used by server.py:
        list_subjects()      -> [{"id", "name", "slug", "stores": [...]}, ...]
        list_stores(subject) -> [{"id", "kind", "title", "document_id"}, ...]
        get_search_space_id(identifier) -> int
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_subjects(self) -> List[Dict[str, Any]]:
        return asyncio.run(self._list_subjects_async())

    def list_stores(self, subject: str) -> List[Dict[str, Any]]:
        return asyncio.run(self._list_stores_async(subject))

    def get_search_space_id(self, identifier: str) -> Optional[int]:
        """Return SearchSpace.id for slug, name, or str(id). Returns None if not found."""
        return asyncio.run(self._get_search_space_id_async(identifier))

    def register_search_space(
        self,
        name: str,
        slug: str,
        subject_name: str,
        weight_overrides: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Create or retrieve a SearchSpace row. Returns dict with id, name, slug."""
        return asyncio.run(
            self._register_search_space_async(name, slug, subject_name, weight_overrides)
        )

    # ------------------------------------------------------------------
    # Async implementations
    # ------------------------------------------------------------------

    async def _list_subjects_async(self) -> List[Dict[str, Any]]:
        async with get_async_session() as session:
            result = await session.execute(select(SearchSpace).order_by(SearchSpace.name))
            spaces = result.scalars().all()

            subjects: List[Dict[str, Any]] = []
            for space in spaces:
                stores = await self._list_stores_for_space(session, space.id)
                subjects.append(
                    {
                        "id": space.id,
                        "name": space.name,
                        "slug": space.slug,
                        "subject_name": space.subject_name,
                        "weight_overrides": space.weight_overrides or {},
                        "stores": stores,
                    }
                )
            return subjects

    async def _list_stores_async(self, subject: str) -> List[Dict[str, Any]]:
        async with get_async_session() as session:
            space = await self._find_space(session, subject)
            if space is None:
                return []
            return await self._list_stores_for_space(session, space.id)

    async def _get_search_space_id_async(self, identifier: str) -> Optional[int]:
        async with get_async_session() as session:
            space = await self._find_space(session, identifier)
            return space.id if space else None

    async def _register_search_space_async(
        self,
        name: str,
        slug: str,
        subject_name: str,
        weight_overrides: Optional[Dict[str, float]],
    ) -> Dict[str, Any]:
        from sqlalchemy.exc import IntegrityError

        async with get_async_session() as session:
            # Check if slug already exists
            result = await session.execute(
                select(SearchSpace).where(SearchSpace.slug == slug)
            )
            space = result.scalars().first()

            if space is None:
                space = SearchSpace(
                    name=name,
                    slug=slug,
                    subject_name=subject_name,
                    weight_overrides=weight_overrides or {},
                )
                session.add(space)
                try:
                    await session.commit()
                    await session.refresh(space)
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(
                        select(SearchSpace).where(SearchSpace.slug == slug)
                    )
                    space = result.scalars().first()

            return {
                "id": space.id,
                "name": space.name,
                "slug": space.slug,
                "subject_name": space.subject_name,
                "weight_overrides": space.weight_overrides or {},
            }

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    async def _find_space(self, session, identifier: str) -> Optional[SearchSpace]:
        from sqlalchemy import func

        result = await session.execute(
            select(SearchSpace).where(SearchSpace.slug == identifier)
        )
        space = result.scalars().first()
        if space:
            return space

        result = await session.execute(
            select(SearchSpace).where(func.lower(SearchSpace.name) == identifier.lower())
        )
        space = result.scalars().first()
        if space:
            return space

        if identifier.isdigit():
            result = await session.execute(
                select(SearchSpace).where(SearchSpace.id == int(identifier))
            )
            space = result.scalars().first()
            if space:
                return space

        return None

    async def _list_stores_for_space(
        self, session, search_space_id: int
    ) -> List[Dict[str, Any]]:
        result = await session.execute(
            select(AITADocument)
            .where(AITADocument.search_space_id == search_space_id)
            .order_by(AITADocument.created_at.desc())
        )
        docs = result.scalars().all()

        stores: List[Dict[str, Any]] = []
        for doc in docs:
            meta = doc.metadata or {}
            stores.append(
                {
                    "id": str(doc.id),
                    "document_id": doc.id,
                    "kind": doc.material_kind or "other",
                    "title": doc.title or "",
                    "status": (doc.status or {}).get("state", "unknown"),
                    "week": meta.get("week"),
                    "source_pdf": meta.get("source_pdf"),
                    "search_space_id": search_space_id,
                }
            )
        return stores
