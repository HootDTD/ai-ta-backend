"""DB-backed workspace repository using the merged ``app.courses`` model.

Implements WorkspaceRepository by querying courses and documents instead of
Supabase REST API or static config files.

This is the primary workspace repository for all retrieval.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from database.models import Document, Course
from database.session import get_async_session, run_async
from retrieval.document_visibility import active_document_conditions
from .manager import ClassWorkspace, WorkspaceMaterial, WorkspaceRepository

log = logging.getLogger(__name__)


class DBWorkspaceRepository(WorkspaceRepository):
    """WorkspaceRepository backed by app.courses + app.documents tables.

    load_workspace() accepts a class identifier that can be:
    - Course.slug  (e.g. "aae-33300")
    - Course.name  (e.g. "AAE 33300")
    - str(Course.id)

    Materials are built from Document rows where status.state == 'ready'.
    index_path is set to Path("") because there are no FAISS directories in
    the pgvector path — callers that still need index_path (old code paths)
    will get an empty path, which they should ignore when the flag is on.
    """

    def load_workspace(self, identifier: str) -> ClassWorkspace:
        """Synchronous entry point; bridges to the shared background loop."""
        return run_async(self._load_workspace_async(identifier))

    async def _load_workspace_async(self, identifier: str) -> ClassWorkspace:
        async with get_async_session() as session:
            course = await self._find_course(session, identifier)
            if course is None:
                raise KeyError(
                    f"No Course found for identifier {identifier!r}."
                )

            materials = await self._load_materials(session, course.id)

        return ClassWorkspace(
            class_id=str(course.id),
            class_name=course.name,
            slug=course.slug,
            subject_name=course.subject_name,
            materials=materials,
            weight_overrides=course.retrieval_weights or {},
            metadata={"search_space_id": course.id},
        )

    async def _find_course(self, session, identifier: str) -> Optional[Course]:
        """Try slug, name (case-insensitive), then integer id."""
        from sqlalchemy import func

        # Try slug exact match
        result = await session.execute(
            select(Course).where(Course.slug == identifier)
        )
        space = result.scalars().first()
        if space:
            return space

        # Try name case-insensitive
        result = await session.execute(
            select(Course).where(func.lower(Course.name) == identifier.lower())
        )
        space = result.scalars().first()
        if space:
            return space

        # Try integer id
        if identifier.isdigit():
            result = await session.execute(
                select(Course).where(Course.id == int(identifier))
            )
            space = result.scalars().first()
            if space:
                return space

        return None

    async def _load_materials(
        self, session, search_space_id: int
    ) -> List[WorkspaceMaterial]:
        result = await session.execute(
            select(Document).where(*active_document_conditions(search_space_id))
        )
        docs = result.scalars().all()

        materials: List[WorkspaceMaterial] = []
        for doc in docs:
            meta = doc.metadata_ or {}
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
