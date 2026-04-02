"""DB-backed workspace repository using pgvector schema (aita_search_spaces).

Implements WorkspaceRepository by querying the aita_search_spaces and
aita_documents tables instead of Supabase REST API or static config files.

This is the primary workspace repository for all retrieval.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from database.models import AITADocument, SearchSpace
from database.session import get_async_session, run_async
from retrieval.document_visibility import active_document_conditions
from .manager import ClassWorkspace, WorkspaceMaterial, WorkspaceRepository

log = logging.getLogger(__name__)


class DBWorkspaceRepository(WorkspaceRepository):
    """WorkspaceRepository backed by aita_search_spaces + aita_documents tables.

    load_workspace() accepts a class identifier that can be:
    - SearchSpace.slug  (e.g. "aae-33300")
    - SearchSpace.name  (e.g. "AAE 33300")
    - str(SearchSpace.id)

    Materials are built from AITADocument rows where status.state == 'ready'.
    index_path is set to Path("") because there are no FAISS directories in
    the pgvector path — callers that still need index_path (old code paths)
    will get an empty path, which they should ignore when the flag is on.
    """

    def load_workspace(self, identifier: str) -> ClassWorkspace:
        """Synchronous entry point; bridges to the shared background loop."""
        return run_async(self._load_workspace_async(identifier))

    async def _load_workspace_async(self, identifier: str) -> ClassWorkspace:
        async with get_async_session() as session:
            space = await self._find_space(session, identifier)
            if space is None:
                raise KeyError(
                    f"No SearchSpace found for identifier {identifier!r}. "
                    "Run migrations/001_create_schema.py and 002_seed_from_supabase.py first."
                )

            materials = await self._load_materials(session, space.id)

        return ClassWorkspace(
            class_id=str(space.id),
            class_name=space.name,
            slug=space.slug,
            subject_name=space.subject_name,
            materials=materials,
            weight_overrides=space.weight_overrides or {},
            metadata={"search_space_id": space.id},
        )

    async def _find_space(self, session, identifier: str) -> Optional[SearchSpace]:
        """Try slug, name (case-insensitive), then integer id."""
        from sqlalchemy import func

        # Try slug exact match
        result = await session.execute(
            select(SearchSpace).where(SearchSpace.slug == identifier)
        )
        space = result.scalars().first()
        if space:
            return space

        # Try name case-insensitive
        result = await session.execute(
            select(SearchSpace).where(func.lower(SearchSpace.name) == identifier.lower())
        )
        space = result.scalars().first()
        if space:
            return space

        # Try integer id
        if identifier.isdigit():
            result = await session.execute(
                select(SearchSpace).where(SearchSpace.id == int(identifier))
            )
            space = result.scalars().first()
            if space:
                return space

        return None

    async def _load_materials(
        self, session, search_space_id: int
    ) -> List[WorkspaceMaterial]:
        result = await session.execute(
            select(AITADocument).where(*active_document_conditions(search_space_id))
        )
        docs = result.scalars().all()

        materials: List[WorkspaceMaterial] = []
        for doc in docs:
            meta = doc.document_metadata or {}
            materials.append(
                WorkspaceMaterial(
                    id=str(doc.id),
                    kind=doc.material_kind or "other",
                    title=doc.title or "",
                    index_path=Path(meta.get("index_path", "")),
                    priority=meta.get("priority", 0),
                    weight_override=None,
                    metadata={
                        "document_id": doc.id,
                        "search_space_id": search_space_id,
                        "week": meta.get("week"),
                        "source_pdf": meta.get("source_pdf"),
                    },
                )
            )

        # Sort: priority desc, then title asc (mirrors ClassWorkspace.sorted_materials())
        materials.sort(key=lambda m: (-m.priority, m.title.lower()))
        return materials
