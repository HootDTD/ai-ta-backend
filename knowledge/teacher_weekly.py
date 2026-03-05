"""Teacher weekly uploads and retrieval-weight controls backed by pgvector.

This module is Supabase-first and uses the shared SQLAlchemy models/tables:
    - aita_search_spaces
    - aita_documents / aita_chunks
    - teacher_courses
    - teacher_uploads
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config.weights import WEIGHT_MAX, WEIGHT_MIN, get_env_weights, normalize_weights
from ..database.models import (
    AITADocument,
    DocumentStatus,
    SearchSpace,
    TeacherCourse,
    TeacherUpload,
)
from ..database.session import get_async_session, run_async
from ..indexing.connector_document import AITAConnectorDocument
from ..indexing.document_hashing import compute_content_hash, compute_unique_identifier_hash
from ..indexing.indexing_service import AITAIndexingService

log = logging.getLogger(__name__)

TOTAL_WEEKS_DEFAULT = 16
VALID_KINDS = {"notes", "slides"}
_INACTIVE_STATUS = {"state": "inactive"}
_LAYOUT_MODULE = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_layout_module():
    global _LAYOUT_MODULE
    if _LAYOUT_MODULE is not None:
        return _LAYOUT_MODULE

    script_path = Path(__file__).resolve().parents[1] / "text-embeder" / "layout_multimodal_embedder.py"
    if not script_path.is_file():
        raise RuntimeError(f"Layout embedder script not found: {script_path}")

    spec = spec_from_file_location("backend_layout_embedder", str(script_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import layout embedder from {script_path}")

    module = module_from_spec(spec)
    if module is None:
        raise RuntimeError(f"Unable to initialize module from {script_path}")
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[assignment]
    _LAYOUT_MODULE = module
    return module


def _build_source_markdown(items: List[Any], fallback_title: str) -> str:
    texts: List[str] = []
    for item in items:
        text = (getattr(item, "text", None) or getattr(item, "raw_text", None) or "").strip()
        if text:
            texts.append(text)
    merged = "\n".join(texts).strip()
    return merged if merged else fallback_title


@dataclass
class UploadRecord:
    id: str
    week: int
    kind: str
    title: str
    uploaded_at: Optional[str]
    source_name: Optional[str]
    page_count: Optional[int]
    index_path: Optional[str]
    doc_id: Optional[str]
    material_id: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "week": self.week,
            "kind": self.kind,
            "title": self.title,
            "uploaded_at": self.uploaded_at,
            "source_name": self.source_name,
            "page_count": self.page_count,
            "index_path": self.index_path,
            "doc_id": self.doc_id,
            "material_id": self.material_id,
        }


class TeacherWeeklyStorage:
    """Manage per-course weekly uploads and retrieval weights in pgvector tables."""

    def __init__(self, base_dir: Optional[os.PathLike[str] | str] = None, *, total_weeks: Optional[int] = None):
        root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[1] / "runtime" / "teacher_weekly"
        self.root = root
        self.total_weeks = int(total_weeks or os.getenv("TEACHER_TOTAL_WEEKS", TOTAL_WEEKS_DEFAULT))
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API (canonical: search_space_id)
    # ------------------------------------------------------------------
    def list_course_by_search_space(self, search_space_id: int) -> Dict[str, Any]:
        return run_async(self._list_course_by_search_space_async(int(search_space_id)))

    def set_current_week_by_search_space(self, search_space_id: int, week: int) -> Dict[str, Any]:
        if week < 1 or week > self.total_weeks:
            raise ValueError(f"current_week must be between 1 and {self.total_weeks}")
        return run_async(self._set_current_week_by_search_space_async(int(search_space_id), int(week)))

    def get_retrieval_weights_by_search_space(self, search_space_id: int) -> Dict[str, float]:
        return run_async(self._get_retrieval_weights_by_search_space_async(int(search_space_id)))

    def update_retrieval_weights_by_search_space(
        self,
        search_space_id: int,
        weights: Mapping[str, Any],
    ) -> Dict[str, float]:
        return run_async(self._update_retrieval_weights_by_search_space_async(int(search_space_id), weights))

    def record_upload_by_search_space(
        self,
        search_space_id: int,
        *,
        week: int,
        kind: str,
        pdf_path: Path,
        title: Optional[str] = None,
        uploaded_by: Optional[str] = None,
    ) -> UploadRecord:
        week_val = int(week)
        if week_val < 1 or week_val > self.total_weeks:
            raise ValueError(f"week must be between 1 and {self.total_weeks}")
        kind_norm = (kind or "").strip().lower()
        if kind_norm not in VALID_KINDS:
            raise ValueError("kind must be 'notes' or 'slides'")

        pdf_resolved = Path(pdf_path).expanduser().resolve()
        if not pdf_resolved.is_file():
            raise FileNotFoundError(f"Teacher upload file not found: {pdf_resolved}")

        resolved_title = (title or f"Week {week_val} {kind_norm.title()}").strip() or f"Week {week_val} {kind_norm.title()}"
        source_sha256 = _sha256_file(pdf_resolved)

        module = _load_layout_module()
        extract_document = getattr(module, "extract_document", None)
        if not callable(extract_document):
            raise RuntimeError("layout_multimodal_embedder.extract_document is unavailable")

        ingest_id = uuid.uuid4().hex
        temp_out = Path(tempfile.mkdtemp(prefix=f"teacher_upload_{ingest_id}_", dir=str(self.root)))
        try:
            items = extract_document(
                pdf_path=pdf_resolved,
                doc_id=f"teacher:{search_space_id}:{week_val}:{kind_norm}:{ingest_id}",
                out_dir=temp_out,
                do_ocr=True,
            )
        finally:
            # Extracted artifacts are transient; indexed text/chunks live in Postgres.
            try:
                import shutil

                shutil.rmtree(temp_out, ignore_errors=True)
            except Exception:
                pass

        if not isinstance(items, list) or not items:
            raise RuntimeError("No extractable content found in uploaded PDF")

        page_count = max((int(getattr(item, "page", 0) or 0) for item in items), default=0) or None
        source_markdown = _build_source_markdown(items, fallback_title=resolved_title)
        unique_id = f"teacher-upload:{search_space_id}:{kind_norm}:{week_val}:{source_sha256}"

        metadata = {
            "source_pdf": str(pdf_resolved),
            "source_name": pdf_resolved.name,
            "source_pdf_sha256": source_sha256,
            "kind": kind_norm,
            "week": week_val,
            "uploaded_via": "teacher_weekly",
        }

        connector_doc = AITAConnectorDocument(
            title=resolved_title,
            source_markdown=source_markdown,
            unique_id=unique_id,
            document_type="EDUCATIONAL_FILE",
            search_space_id=int(search_space_id),
            material_kind=kind_norm,
            should_summarize=False,
            page_count=page_count,
            week=week_val,
            metadata=metadata,
        )

        return run_async(
            self._index_and_record_upload_async(
                connector_doc=connector_doc,
                items=items,
                source_name=pdf_resolved.name,
                uploaded_by=uploaded_by,
            )
        )

    # ------------------------------------------------------------------
    # Compatibility wrappers (class-name based) for pre-migration endpoints
    # ------------------------------------------------------------------
    def list_course(self, course: str) -> Dict[str, Any]:
        search_space_id = self._resolve_search_space_id(course)
        return self.list_course_by_search_space(search_space_id)

    def set_current_week(self, course: str, week: int) -> Dict[str, Any]:
        search_space_id = self._resolve_search_space_id(course)
        return self.set_current_week_by_search_space(search_space_id, week)

    def get_retrieval_weights(self, course: str) -> Dict[str, float]:
        search_space_id = self._resolve_search_space_id(course)
        return self.get_retrieval_weights_by_search_space(search_space_id)

    def update_retrieval_weights(self, course: str, weights: Mapping[str, Any]) -> Dict[str, float]:
        search_space_id = self._resolve_search_space_id(course)
        return self.update_retrieval_weights_by_search_space(search_space_id, weights)

    def record_upload(
        self,
        course: str,
        *,
        week: int,
        kind: str,
        pdf_path: Path,
        title: Optional[str] = None,
    ) -> UploadRecord:
        search_space_id = self._resolve_search_space_id(course)
        return self.record_upload_by_search_space(
            search_space_id,
            week=week,
            kind=kind,
            pdf_path=pdf_path,
            title=title,
            uploaded_by=None,
        )

    def resolve_week_doc_sets(
        self,
        course: str,
        *,
        week: Optional[int] = None,
        include_previous: bool = False,
    ) -> List[str]:
        # pgvector path does not use FAISS index directories.
        _ = (course, week, include_previous)
        return []

    # ------------------------------------------------------------------
    # Internal async helpers
    # ------------------------------------------------------------------
    def _resolve_search_space_id(self, identifier: str) -> int:
        if not identifier or not str(identifier).strip():
            raise ValueError("class is required")
        return run_async(self._resolve_search_space_id_async(str(identifier).strip()))

    async def _resolve_search_space_id_async(self, identifier: str) -> int:
        async with get_async_session() as session:
            space = await self._load_search_space(session, identifier)
            if space is None:
                raise ValueError(f"Unknown class: {identifier}")
            return int(space.id)

    async def _load_search_space(self, session: AsyncSession, identifier: str | int) -> Optional[SearchSpace]:
        if isinstance(identifier, int):
            result = await session.execute(select(SearchSpace).where(SearchSpace.id == int(identifier)))
            return result.scalars().first()

        token = str(identifier).strip()
        if token.isdigit():
            result = await session.execute(select(SearchSpace).where(SearchSpace.id == int(token)))
            found = result.scalars().first()
            if found is not None:
                return found

        result = await session.execute(select(SearchSpace).where(SearchSpace.slug == token))
        found = result.scalars().first()
        if found is not None:
            return found

        lowered = token.lower()
        result = await session.execute(
            select(SearchSpace).where(func.lower(SearchSpace.name) == lowered)
        )
        return result.scalars().first()

    async def _get_or_create_teacher_course(
        self,
        session: AsyncSession,
        *,
        search_space_id: int,
    ) -> TeacherCourse:
        result = await session.execute(
            select(TeacherCourse).where(TeacherCourse.search_space_id == search_space_id)
        )
        existing = result.scalars().first()
        if existing is not None:
            return existing

        course = TeacherCourse(
            search_space_id=search_space_id,
            current_week=1,
            weights=get_env_weights(),
            weight_bounds={"min": WEIGHT_MIN, "max": WEIGHT_MAX},
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        session.add(course)
        try:
            await session.flush()
            return course
        except IntegrityError:
            # Another request created this row concurrently.
            await session.rollback()
            retry = await session.execute(
                select(TeacherCourse).where(TeacherCourse.search_space_id == search_space_id)
            )
            existing_retry = retry.scalars().first()
            if existing_retry is None:
                raise
            return existing_retry

    async def _sync_week_activation(
        self,
        session: AsyncSession,
        *,
        search_space_id: int,
        current_week: int,
    ) -> None:
        now = _utc_now()
        kind_filter = AITADocument.material_kind.in_(tuple(VALID_KINDS))
        scoped = AITADocument.search_space_id == search_space_id
        has_week = AITADocument.week.is_not(None)

        # Future weekly notes/slides stay hidden from retrieval.
        await session.execute(
            update(AITADocument)
            .where(scoped, kind_filter, has_week, AITADocument.week > current_week)
            .values(status=_INACTIVE_STATUS, updated_at=now)
        )

        # Current/past weekly notes/slides are retrievable unless they failed ingestion.
        await session.execute(
            update(AITADocument)
            .where(
                scoped,
                kind_filter,
                has_week,
                AITADocument.week <= current_week,
                AITADocument.status["state"].astext != DocumentStatus.FAILED,
            )
            .values(status=DocumentStatus.ready(), updated_at=now)
        )

    async def _list_course_by_search_space_async(self, search_space_id: int) -> Dict[str, Any]:
        async with get_async_session() as session:
            space = await self._load_search_space(session, int(search_space_id))
            if space is None:
                raise ValueError(f"Unknown search_space_id: {search_space_id}")

            course_row = await self._get_or_create_teacher_course(
                session, search_space_id=int(search_space_id)
            )
            await session.commit()

            uploads_result = await session.execute(
                select(TeacherUpload)
                .where(TeacherUpload.search_space_id == int(search_space_id))
                .order_by(TeacherUpload.week.asc(), TeacherUpload.kind.asc(), TeacherUpload.uploaded_at.desc())
            )
            uploads = uploads_result.scalars().all()

        weeks: List[Dict[str, Any]] = []
        for week_num in range(1, self.total_weeks + 1):
            week_rows = [row for row in uploads if int(row.week or 0) == week_num]
            notes_rows = [row for row in week_rows if (row.kind or "").strip().lower() == "notes"]
            slides_rows = [row for row in week_rows if (row.kind or "").strip().lower() == "slides"]
            weeks.append(
                {
                    "week": week_num,
                    "notes": self._build_section(notes_rows),
                    "slides": self._build_section(slides_rows),
                }
            )

        return {
            "search_space_id": int(search_space_id),
            "course": str(space.name),
            "slug": str(space.slug),
            "current_week": int(course_row.current_week or 1),
            "weeks": weeks,
        }

    async def _set_current_week_by_search_space_async(self, search_space_id: int, week: int) -> Dict[str, Any]:
        async with get_async_session() as session:
            space = await self._load_search_space(session, int(search_space_id))
            if space is None:
                raise ValueError(f"Unknown search_space_id: {search_space_id}")

            course_row = await self._get_or_create_teacher_course(
                session, search_space_id=int(search_space_id)
            )
            course_row.current_week = int(week)
            course_row.updated_at = _utc_now()

            await self._sync_week_activation(
                session,
                search_space_id=int(search_space_id),
                current_week=int(week),
            )
            await session.commit()
            return {
                "search_space_id": int(search_space_id),
                "course": str(space.name),
                "current_week": int(week),
            }

    async def _get_retrieval_weights_by_search_space_async(self, search_space_id: int) -> Dict[str, float]:
        async with get_async_session() as session:
            space = await self._load_search_space(session, int(search_space_id))
            if space is None:
                raise ValueError(f"Unknown search_space_id: {search_space_id}")

            course_row = await self._get_or_create_teacher_course(
                session, search_space_id=int(search_space_id)
            )
            await session.commit()
            base = get_env_weights()
            return normalize_weights(course_row.weights or {}, clamp=True, base=base)

    async def _update_retrieval_weights_by_search_space_async(
        self,
        search_space_id: int,
        weights: Mapping[str, Any],
    ) -> Dict[str, float]:
        async with get_async_session() as session:
            space = await self._load_search_space(session, int(search_space_id))
            if space is None:
                raise ValueError(f"Unknown search_space_id: {search_space_id}")

            course_row = await self._get_or_create_teacher_course(
                session, search_space_id=int(search_space_id)
            )
            base = get_env_weights()
            updated = normalize_weights(weights, clamp=True, base=base)
            course_row.weights = updated
            course_row.weight_bounds = {"min": WEIGHT_MIN, "max": WEIGHT_MAX}
            course_row.updated_at = _utc_now()
            await session.commit()
            return updated

    async def _index_and_record_upload_async(
        self,
        *,
        connector_doc: AITAConnectorDocument,
        items: List[Any],
        source_name: str,
        uploaded_by: Optional[str],
    ) -> UploadRecord:
        async with get_async_session() as session:
            search_space_id = int(connector_doc.search_space_id)
            space = await self._load_search_space(session, search_space_id)
            if space is None:
                raise ValueError(f"Unknown search_space_id: {search_space_id}")

            service = AITAIndexingService(session)
            docs = await service.prepare_for_indexing([connector_doc])

            indexed_doc: Optional[AITADocument] = None
            if docs:
                indexed_doc = await service.index_from_items(docs[0], connector_doc, items)
            else:
                uid_hash = compute_unique_identifier_hash(connector_doc)
                content_hash = compute_content_hash(connector_doc)
                result = await session.execute(
                    select(AITADocument)
                    .where(
                        or_(
                            AITADocument.unique_identifier_hash == uid_hash,
                            AITADocument.content_hash == content_hash,
                        )
                    )
                    .order_by(AITADocument.updated_at.desc())
                )
                indexed_doc = result.scalars().first()

            if indexed_doc is None:
                raise RuntimeError("Failed to resolve indexed document for teacher upload")

            course_row = await self._get_or_create_teacher_course(
                session, search_space_id=search_space_id
            )

            now = _utc_now()
            await session.execute(
                update(TeacherUpload)
                .where(
                    TeacherUpload.search_space_id == search_space_id,
                    TeacherUpload.week == int(connector_doc.week or 0),
                    TeacherUpload.kind == str(connector_doc.material_kind),
                    TeacherUpload.is_latest.is_(True),
                )
                .values(is_latest=False, updated_at=now)
            )

            upload = TeacherUpload(
                search_space_id=search_space_id,
                week=int(connector_doc.week or 0),
                kind=str(connector_doc.material_kind),
                title=str(connector_doc.title),
                source_name=source_name,
                doc_id=int(indexed_doc.id) if indexed_doc.id is not None else None,
                page_count=indexed_doc.page_count,
                is_latest=True,
                uploaded_by=uploaded_by,
                metadata_={
                    "search_space_id": search_space_id,
                    "material_id": f"doc-{indexed_doc.id}" if indexed_doc.id is not None else None,
                    "source_pdf": (connector_doc.metadata or {}).get("source_pdf"),
                    "source_pdf_sha256": (connector_doc.metadata or {}).get("source_pdf_sha256"),
                },
                uploaded_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(upload)

            await self._sync_week_activation(
                session,
                search_space_id=search_space_id,
                current_week=int(course_row.current_week or 1),
            )

            await session.commit()
            await session.refresh(upload)

            return UploadRecord(
                id=str(upload.id),
                week=int(upload.week),
                kind=str(upload.kind),
                title=str(upload.title),
                uploaded_at=_iso(upload.uploaded_at),
                source_name=upload.source_name,
                page_count=upload.page_count,
                index_path=None,
                doc_id=str(upload.doc_id) if upload.doc_id is not None else None,
                material_id=f"doc-{upload.doc_id}" if upload.doc_id is not None else None,
            )

    def _build_section(self, rows: List[TeacherUpload]) -> Dict[str, Any]:
        history: List[Dict[str, Any]] = []
        latest: Optional[Dict[str, Any]] = None
        for row in rows:
            entry = self._teacher_upload_to_dict(row)
            history.append(entry)
            if bool(row.is_latest) and latest is None:
                latest = entry
        return {"latest": latest, "history": history}

    def _teacher_upload_to_dict(self, row: TeacherUpload) -> Dict[str, Any]:
        meta = row.metadata_ or {}
        material_id = meta.get("material_id")
        if material_id is None and row.doc_id is not None:
            material_id = f"doc-{row.doc_id}"
        return {
            "id": str(row.id),
            "week": int(row.week),
            "kind": str(row.kind),
            "title": str(row.title),
            "uploaded_at": _iso(row.uploaded_at),
            "source_name": row.source_name,
            "page_count": row.page_count,
            "index_path": None,
            "doc_id": str(row.doc_id) if row.doc_id is not None else None,
            "material_id": material_id,
        }


__all__ = ["TeacherWeeklyStorage", "UploadRecord"]
