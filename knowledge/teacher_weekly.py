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
import re
import shutil
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.weights import WEIGHT_MAX, WEIGHT_MIN, get_env_weights, normalize_weights
from database.models import (
    AITADocument,
    DocumentStatus,
    SearchSpace,
    TeacherCourse,
    TeacherUpload,
    TeacherUploadJob,
)
from database.session import get_async_session, run_async
from indexing.connector_document import AITAConnectorDocument
from indexing.document_hashing import compute_content_hash, compute_unique_identifier_hash
from indexing.indexing_service import AITAIndexingService
from knowledge.teacher_pdf_ingestion import TeacherPDFIngestor, TeacherPDFIngestionResult
from vendors.supabase_storage import SupabaseStorageClient

log = logging.getLogger(__name__)

TOTAL_WEEKS_DEFAULT = 16
# Weekly materials are pinned to a week (1..total_weeks). Course-wide materials
# (e.g. a textbook) belong to the class as a whole — their tracking row uses the
# COURSE_WIDE_WEEK sentinel and their indexed document stores week=NULL so it
# stays visible across every week (see retrieval/document_visibility.py).
WEEKLY_KINDS = {"notes", "slides"}
COURSE_WIDE_KINDS = {"textbook"}
VALID_KINDS = WEEKLY_KINDS | COURSE_WIDE_KINDS
COURSE_WIDE_WEEK = 0
_INACTIVE_STATUS = {"state": "inactive"}
UPLOAD_STATUS_QUEUED = "queued"
UPLOAD_STATUS_PROCESSING = "processing"
UPLOAD_STATUS_READY = "ready"
UPLOAD_STATUS_FAILED = "failed"
UPLOAD_STATUS_SUPERSEDED = "superseded"
JOB_STATE_QUEUED = "queued"
JOB_STATE_PROCESSING = "processing"
JOB_STATE_COMPLETED = "completed"
JOB_STATE_FAILED = "failed"
DEFAULT_UPLOAD_BUCKET = "teacher-weekly-uploads"
DEFAULT_PAGES_BUCKET = "teacher-weekly-pages"
DEFAULT_MAX_RETRIES = 3
DEFAULT_JOB_LEASE_SECONDS = 900
DEFAULT_JOB_POLL_SECONDS = 5.0


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


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


def _safe_filename(name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-")
    return token or "teacher-upload.pdf"


def _normalize_upload_week(kind: str, week: Any, total_weeks: int) -> int:
    """Resolve the tracking-row week for an upload.

    Course-wide kinds (e.g. ``textbook``) are not tied to a week and use the
    ``COURSE_WIDE_WEEK`` sentinel. Weekly kinds must fall in ``1..total_weeks``.
    Raises ``ValueError`` on an unknown kind or out-of-range weekly week.
    """
    kind_norm = (kind or "").strip().lower()
    if kind_norm not in VALID_KINDS:
        raise ValueError("kind must be 'notes', 'slides', or 'textbook'")
    if kind_norm in COURSE_WIDE_KINDS:
        return COURSE_WIDE_WEEK
    week_val = int(week)
    if week_val < 1 or week_val > total_weeks:
        raise ValueError(f"week must be between 1 and {total_weeks}")
    return week_val


def _document_week(kind: str, tracking_week: int) -> Optional[int]:
    """Week stored on the indexed ``AITADocument``.

    Course-wide materials store ``NULL`` so they remain visible for every week
    (the weekly activate/deactivate cycle only touches rows where week IS NOT
    NULL). Weekly materials store their tracking week unchanged.
    """
    if (kind or "").strip().lower() in COURSE_WIDE_KINDS:
        return None
    return int(tracking_week)


def _truncate_error(message: str | None) -> Optional[str]:
    if message is None:
        return None
    cleaned = " ".join(str(message).split()).strip()
    return cleaned[:500] if cleaned else None


# Patterns that indicate the error is permanent and retrying won't help.
_TERMINAL_ERROR_PATTERNS: tuple[str, ...] = (
    "No searchable chunks produced",
    "PyMuPDF is required",
    "invalid pdf",
    "not a pdf",
    "password protected",
    "encrypted",
    "401",
    "403",
    "AuthenticationError",
    "PermissionError",
    "InvalidFileError",
)


def _is_terminal_error(exc: BaseException) -> bool:
    """Return True if the exception represents a permanent failure that should not be retried."""
    msg = str(exc).lower()
    for pattern in _TERMINAL_ERROR_PATTERNS:
        if pattern.lower() in msg:
            return True
    if isinstance(exc, (FileNotFoundError, ValueError, TypeError, PermissionError)):
        return True
    return False


@dataclass
class UploadRecord:
    id: str
    week: int
    kind: str
    title: str
    status: str
    uploaded_at: Optional[str]
    source_name: Optional[str]
    page_count: Optional[int]
    index_path: Optional[str]
    doc_id: Optional[str]
    material_id: Optional[str]
    error_message: Optional[str] = None
    warning_count: int = 0
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    ocr_provider: Optional[str] = None
    ocr_summary: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "week": self.week,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "uploaded_at": self.uploaded_at,
            "source_name": self.source_name,
            "page_count": self.page_count,
            "index_path": self.index_path,
            "doc_id": self.doc_id,
            "material_id": self.material_id,
            "error_message": self.error_message,
            "warning_count": self.warning_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "ocr_provider": self.ocr_provider,
            "ocr_summary": self.ocr_summary or {},
        }


@dataclass
class ClaimedUploadJob:
    job_id: int
    upload_id: int
    search_space_id: int
    week: int
    kind: str
    title: str
    source_name: str
    storage_key: str


class TeacherWeeklyStorage:
    """Manage per-course weekly uploads and retrieval weights in pgvector tables."""

    def __init__(
        self,
        base_dir: Optional[os.PathLike[str] | str] = None,
        *,
        total_weeks: Optional[int] = None,
        storage_client: Optional[SupabaseStorageClient] = None,
    ):
        root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parents[1] / "runtime" / "teacher_weekly"
        self.root = root
        self.total_weeks = int(total_weeks or os.getenv("TEACHER_TOTAL_WEEKS", TOTAL_WEEKS_DEFAULT))
        self.upload_bucket = (os.getenv("TEACHER_UPLOAD_BUCKET") or DEFAULT_UPLOAD_BUCKET).strip() or DEFAULT_UPLOAD_BUCKET
        self.pages_bucket = (os.getenv("TEACHER_UPLOAD_PAGES_BUCKET") or DEFAULT_PAGES_BUCKET).strip() or DEFAULT_PAGES_BUCKET
        self.max_retries = max(1, _env_int("TEACHER_UPLOAD_MAX_RETRIES", DEFAULT_MAX_RETRIES))
        self.job_lease_seconds = max(30, _env_int("TEACHER_UPLOAD_JOB_LEASE_SECONDS", DEFAULT_JOB_LEASE_SECONDS))
        self.job_poll_seconds = max(0.5, _env_float("TEACHER_UPLOAD_JOB_POLL_SECONDS", DEFAULT_JOB_POLL_SECONDS))
        self.storage = storage_client or SupabaseStorageClient()
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

    def enqueue_upload_by_search_space(
        self,
        search_space_id: int,
        *,
        week: int,
        kind: str,
        pdf_path: Path,
        title: Optional[str] = None,
        uploaded_by: Optional[str] = None,
    ) -> UploadRecord:
        return run_async(
            self._enqueue_upload_by_search_space_async(
                int(search_space_id),
                week=int(week),
                kind=kind,
                pdf_path=pdf_path,
                title=title,
                uploaded_by=uploaded_by,
            )
        )

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
        upload = self.enqueue_upload_by_search_space(
            search_space_id,
            week=week,
            kind=kind,
            pdf_path=pdf_path,
            title=title,
            uploaded_by=uploaded_by,
        )
        self.process_upload_by_id(int(upload.id))
        return self.get_upload_record(int(upload.id))

    def get_upload_record(self, upload_id: int) -> UploadRecord:
        return run_async(self._get_upload_record_async(int(upload_id)))

    def get_upload_search_space_id(self, upload_id: int) -> int:
        return run_async(self._get_upload_search_space_id_async(int(upload_id)))

    def retry_upload(self, upload_id: int) -> UploadRecord:
        return run_async(self._retry_upload_async(int(upload_id)))

    def reindex_upload(self, upload_id: int) -> UploadRecord:
        """Force re-processing of a completed upload (e.g. after OCR degradation).

        Unlike retry_upload, this works on 'ready' uploads whose OCR was degraded.
        It resets the upload to queued so the background worker re-processes it,
        appending a reindex marker to force a new content hash.
        """
        return run_async(self._reindex_upload_async(int(upload_id)))

    def process_upload_by_id(self, upload_id: int) -> UploadRecord:
        claimed = run_async(self._claim_upload_job_for_upload_async(int(upload_id)))
        if claimed is None:
            raise ValueError(f"No queued teacher upload job found for upload {upload_id}")
        self._process_claimed_upload_job(claimed)
        return self.get_upload_record(int(upload_id))

    def process_next_upload_job(self, *, lease_owner: Optional[str] = None) -> bool:
        worker_id = lease_owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        claimed = run_async(self._claim_next_upload_job_async(worker_id))
        if claimed is None:
            return False
        self._process_claimed_upload_job(claimed)
        return True

    def run_upload_worker_loop(self, *, lease_owner: Optional[str] = None) -> None:
        worker_id = lease_owner or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        log.info("Teacher upload worker started as %s", worker_id)
        while True:
            processed = False
            try:
                processed = self.process_next_upload_job(lease_owner=worker_id)
            except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
                raise
            except Exception:
                log.exception("Teacher upload worker iteration failed")
            if not processed:
                time.sleep(self.job_poll_seconds)

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

    def _validate_upload_input(
        self,
        *,
        week: int,
        kind: str,
        pdf_path: Path,
        title: Optional[str],
    ) -> tuple[int, str, Path, str]:
        kind_norm = (kind or "").strip().lower()
        week_val = _normalize_upload_week(kind_norm, week, self.total_weeks)

        pdf_resolved = Path(pdf_path).expanduser().resolve()
        if not pdf_resolved.is_file():
            raise FileNotFoundError(f"Teacher upload file not found: {pdf_resolved}")

        if kind_norm in COURSE_WIDE_KINDS:
            default_title = kind_norm.title()
        else:
            default_title = f"Week {week_val} {kind_norm.title()}"
        resolved_title = (title or default_title).strip() or default_title
        return week_val, kind_norm, pdf_resolved, resolved_title

    def _build_storage_key(
        self,
        *,
        search_space_id: int,
        week: int,
        kind: str,
        source_name: str,
    ) -> str:
        return (
            f"search-space-{int(search_space_id)}/"
            f"week-{int(week):02d}/"
            f"{kind}/"
            f"{uuid.uuid4().hex}/"
            f"{_safe_filename(source_name)}"
        )

    def _store_page_asset(
        self,
        *,
        upload_id: int,
        page_number: int,
        image_bytes: bytes,
        width: int,
        height: int,
    ) -> Dict[str, Any]:
        object_key = (
            f"teacher-uploads/{int(upload_id)}/"
            f"page-{int(page_number):04d}.png"
        )
        self.storage.upload_bytes(
            bucket=self.pages_bucket,
            object_key=object_key,
            data=image_bytes,
            content_type="image/png",
        )
        return {
            "bucket": self.pages_bucket,
            "storage_key": object_key,
            "width": int(width),
            "height": int(height),
        }

    def _ingest_pdf_upload(
        self,
        *,
        claimed: ClaimedUploadJob,
        pdf_path: Path,
        reindex_marker: Optional[str] = None,
    ) -> tuple[AITAConnectorDocument, TeacherPDFIngestionResult, str]:
        pdf_resolved = Path(pdf_path).expanduser().resolve()
        source_sha256 = _sha256_file(pdf_resolved)
        ingestor = TeacherPDFIngestor()
        ingestion = ingestor.ingest(
            pdf_resolved,
            doc_id=f"teacher:{claimed.search_space_id}:{claimed.week}:{claimed.kind}:{claimed.upload_id}",
            upload_page_asset=lambda page_number, image_bytes, width, height: self._store_page_asset(
                upload_id=claimed.upload_id,
                page_number=page_number,
                image_bytes=image_bytes,
                width=width,
                height=height,
            ),
        )
        if not ingestion.items:
            raise RuntimeError("No searchable chunks produced from upload")

        # Detect OCR degradation: pages that needed Mathpix but didn't get it
        ocr_summary = ingestion.ocr_summary or {}
        ocr_degraded = bool(
            ocr_summary.get("warning_pages", 0) > 0
            and ocr_summary.get("mathpix_pages", 0) == 0
            and ocr_summary.get("native_plus_mathpix_pages", 0) == 0
        )

        source_md = ingestion.source_markdown or claimed.title
        if reindex_marker:
            source_md = f"{source_md}\n<!-- reindex:{reindex_marker} -->"

        # Course-wide materials (textbook) index with week=NULL so retrieval keeps
        # them visible for every week; weekly materials keep their week.
        doc_week = _document_week(claimed.kind, claimed.week)

        connector_doc = AITAConnectorDocument(
            title=claimed.title,
            source_markdown=source_md,
            unique_id=f"teacher-upload:{claimed.upload_id}",
            document_type="EDUCATIONAL_FILE",
            search_space_id=int(claimed.search_space_id),
            material_kind=claimed.kind,
            should_summarize=False,
            page_count=ingestion.page_count,
            week=doc_week,
            metadata={
                "teacher_upload_id": str(claimed.upload_id),
                "source_name": pdf_resolved.name,
                "source_pdf": str(pdf_resolved),
                "source_storage_key": claimed.storage_key,
                "source_storage_bucket": self.upload_bucket,
                "source_pdf_sha256": source_sha256,
                "kind": claimed.kind,
                "material_kind": claimed.kind,
                "week": doc_week,
                "uploaded_via": "teacher_weekly_hybrid_ingestor",
                "ocr_degraded": ocr_degraded,
                "ocr_provider": ingestion.ocr_provider,
                "artifact_manifest": ingestion.artifact_manifest,
                "ocr_summary": ingestion.ocr_summary,
                "warning_count": ingestion.warning_count,
                "page_debug": [
                    {
                        "page": page.page_number,
                        "ocr_confidence": page.ocr_confidence,
                        "extraction_mode": page.extraction_mode,
                        "latex_text": page.latex_text or None,
                        "page_asset": dict(page.asset or {}),
                        "warnings": list(page.warnings or []),
                    }
                    for page in ingestion.pages
                ],
            },
        )
        return connector_doc, ingestion, source_sha256

    def _process_claimed_upload_job(self, claimed: ClaimedUploadJob) -> None:
        work_dir = Path(tempfile.mkdtemp(prefix=f"teacher_worker_{claimed.upload_id}_", dir=str(self.root)))
        pdf_path = work_dir / _safe_filename(claimed.source_name)
        try:
            payload = self.storage.download_bytes(bucket=self.upload_bucket, object_key=claimed.storage_key)
            pdf_path.write_bytes(payload)
            # Check if this is a re-index request; pass marker to bust content hash
            reindex_marker = run_async(self._get_reindex_marker_async(claimed.upload_id))
            connector_doc, ingestion, source_sha256 = self._ingest_pdf_upload(
                claimed=claimed,
                pdf_path=pdf_path,
                reindex_marker=reindex_marker,
            )
            run_async(
                self._index_existing_upload_async(
                    claimed=claimed,
                    connector_doc=connector_doc,
                    items=ingestion.items,
                    ingestion=ingestion,
                    source_sha256=source_sha256,
                )
            )
        except Exception as exc:
            log.exception(
                "Teacher upload job failed upload_id=%s job_id=%s",
                claimed.upload_id,
                claimed.job_id,
            )
            run_async(self._handle_job_failure_async(
                claimed, str(exc), terminal=_is_terminal_error(exc),
            ))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

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

    async def _enqueue_upload_by_search_space_async(
        self,
        search_space_id: int,
        *,
        week: int,
        kind: str,
        pdf_path: Path,
        title: Optional[str],
        uploaded_by: Optional[str],
    ) -> UploadRecord:
        week_val, kind_norm, pdf_resolved, resolved_title = self._validate_upload_input(
            week=week,
            kind=kind,
            pdf_path=pdf_path,
            title=title,
        )
        source_name = pdf_resolved.name
        source_sha256 = _sha256_file(pdf_resolved)

        async with get_async_session() as session:
            space = await self._load_search_space(session, int(search_space_id))
            if space is None:
                raise ValueError(f"Unknown search_space_id: {search_space_id}")
            await self._get_or_create_teacher_course(session, search_space_id=int(search_space_id))
            await session.commit()

        storage_key = self._build_storage_key(
            search_space_id=int(search_space_id),
            week=week_val,
            kind=kind_norm,
            source_name=source_name,
        )
        self.storage.upload_bytes(
            bucket=self.upload_bucket,
            object_key=storage_key,
            data=pdf_resolved.read_bytes(),
            content_type="application/pdf",
        )

        now = _utc_now()
        async with get_async_session() as session:
            upload = TeacherUpload(
                search_space_id=int(search_space_id),
                week=week_val,
                kind=kind_norm,
                title=resolved_title,
                source_name=source_name,
                doc_id=None,
                status=UPLOAD_STATUS_QUEUED,
                storage_key=storage_key,
                artifact_manifest={"bucket": self.pages_bucket, "pages": []},
                ocr_provider=None,
                ocr_summary={},
                warning_count=0,
                error_message=None,
                started_at=None,
                completed_at=None,
                attempt_count=0,
                page_count=None,
                is_latest=False,
                uploaded_by=uploaded_by,
                metadata_={
                    "search_space_id": int(search_space_id),
                    "storage_bucket": self.upload_bucket,
                    "pages_bucket": self.pages_bucket,
                    "source_pdf_sha256": source_sha256,
                },
                uploaded_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(upload)
            await session.flush()

            job = TeacherUploadJob(
                upload_id=int(upload.id),
                state=JOB_STATE_QUEUED,
                lease_owner=None,
                lease_expires_at=None,
                attempt_count=0,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            await session.commit()
            await session.refresh(upload)
            return self._build_upload_record(upload)

    async def _get_upload_record_async(self, upload_id: int) -> UploadRecord:
        async with get_async_session() as session:
            row = await session.get(TeacherUpload, int(upload_id))
            if row is None:
                raise ValueError(f"Unknown teacher upload: {upload_id}")
            return self._build_upload_record(row)

    async def _get_upload_search_space_id_async(self, upload_id: int) -> int:
        async with get_async_session() as session:
            row = await session.get(TeacherUpload, int(upload_id))
            if row is None:
                raise ValueError(f"Unknown teacher upload: {upload_id}")
            return int(row.search_space_id)

    async def _retry_upload_async(self, upload_id: int) -> UploadRecord:
        async with get_async_session() as session:
            upload = await session.get(TeacherUpload, int(upload_id))
            if upload is None:
                raise ValueError(f"Unknown teacher upload: {upload_id}")
            if str(upload.status or "") != UPLOAD_STATUS_FAILED:
                raise ValueError("Only failed uploads can be retried")
            if not upload.storage_key:
                raise ValueError("Upload is missing stored PDF and cannot be retried")

            job_result = await session.execute(
                select(TeacherUploadJob)
                .where(TeacherUploadJob.upload_id == int(upload.id))
                .order_by(TeacherUploadJob.created_at.desc(), TeacherUploadJob.id.desc())
            )
            job = job_result.scalars().first()
            now = _utc_now()

            upload.status = UPLOAD_STATUS_QUEUED
            upload.error_message = None
            upload.warning_count = 0
            upload.started_at = None
            upload.completed_at = None
            upload.ocr_provider = None
            upload.ocr_summary = {}
            upload.updated_at = now

            if job is None:
                job = TeacherUploadJob(
                    upload_id=int(upload.id),
                    state=JOB_STATE_QUEUED,
                    lease_owner=None,
                    lease_expires_at=None,
                    attempt_count=0,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(job)
            else:
                job.state = JOB_STATE_QUEUED
                job.lease_owner = None
                job.lease_expires_at = None
                # Allow one more attempt instead of resetting to 0 (prevents infinite retry loops)
                job.attempt_count = max(0, int(job.attempt_count or 0) - 1)
                job.last_error = None
                job.updated_at = now

            await session.commit()
            await session.refresh(upload)
            return self._build_upload_record(upload)

    async def _get_reindex_marker_async(self, upload_id: int) -> Optional[str]:
        """Return the reindex_requested_at timestamp if set, else None."""
        async with get_async_session() as session:
            upload = await session.get(TeacherUpload, int(upload_id))
            if upload is None:
                return None
            meta = upload.metadata_ or {}
            return meta.get("reindex_requested_at")

    async def _reindex_upload_async(self, upload_id: int) -> UploadRecord:
        async with get_async_session() as session:
            upload = await session.get(TeacherUpload, int(upload_id))
            if upload is None:
                raise ValueError(f"Unknown teacher upload: {upload_id}")
            if str(upload.status or "") not in (UPLOAD_STATUS_READY, UPLOAD_STATUS_FAILED):
                raise ValueError("Only ready or failed uploads can be re-indexed")
            if not upload.storage_key:
                raise ValueError("Upload is missing stored PDF and cannot be re-indexed")

            job_result = await session.execute(
                select(TeacherUploadJob)
                .where(TeacherUploadJob.upload_id == int(upload.id))
                .order_by(TeacherUploadJob.created_at.desc(), TeacherUploadJob.id.desc())
            )
            job = job_result.scalars().first()
            now = _utc_now()

            # Mark for re-index: stamp the metadata so content hash changes
            meta = dict(upload.metadata_ or {})
            meta["reindex_requested_at"] = now.isoformat()
            meta.pop("ocr_degraded", None)

            upload.status = UPLOAD_STATUS_QUEUED
            upload.error_message = None
            upload.warning_count = 0
            upload.started_at = None
            upload.completed_at = None
            upload.ocr_provider = None
            upload.ocr_summary = {}
            upload.metadata_ = meta
            upload.updated_at = now

            if job is None:
                job = TeacherUploadJob(
                    upload_id=int(upload.id),
                    state=JOB_STATE_QUEUED,
                    lease_owner=None,
                    lease_expires_at=None,
                    attempt_count=0,
                    last_error=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(job)
            else:
                job.state = JOB_STATE_QUEUED
                job.lease_owner = None
                job.lease_expires_at = None
                job.attempt_count = 0
                job.last_error = None
                job.updated_at = now

            await session.commit()
            await session.refresh(upload)
            return self._build_upload_record(upload)

    async def _claim_upload_job_for_upload_async(self, upload_id: int) -> Optional[ClaimedUploadJob]:
        owner = f"sync:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        return await self._claim_upload_job_async(owner, upload_id=int(upload_id))

    async def _claim_next_upload_job_async(self, lease_owner: str) -> Optional[ClaimedUploadJob]:
        return await self._claim_upload_job_async(lease_owner, upload_id=None)

    async def _claim_upload_job_async(
        self,
        lease_owner: str,
        *,
        upload_id: Optional[int],
    ) -> Optional[ClaimedUploadJob]:
        now = _utc_now()
        async with get_async_session() as session:
            stmt = (
                select(TeacherUploadJob, TeacherUpload)
                .join(TeacherUpload, TeacherUpload.id == TeacherUploadJob.upload_id)
                .where(
                    TeacherUploadJob.state.in_((JOB_STATE_QUEUED, JOB_STATE_PROCESSING)),
                    or_(
                        TeacherUploadJob.lease_expires_at.is_(None),
                        TeacherUploadJob.lease_expires_at < now,
                    ),
                    TeacherUpload.status.in_((UPLOAD_STATUS_QUEUED, UPLOAD_STATUS_PROCESSING)),
                    TeacherUpload.storage_key.is_not(None),
                )
                .order_by(TeacherUploadJob.created_at.asc(), TeacherUploadJob.id.asc())
                .with_for_update(skip_locked=True)
            )
            if upload_id is not None:
                stmt = stmt.where(TeacherUploadJob.upload_id == int(upload_id))

            row = (await session.execute(stmt)).first()
            if row is None:
                return None

            job, upload = row
            job.state = JOB_STATE_PROCESSING
            job.lease_owner = lease_owner
            job.lease_expires_at = now + timedelta(seconds=self.job_lease_seconds)
            job.attempt_count = int(job.attempt_count or 0) + 1
            job.updated_at = now

            upload.status = UPLOAD_STATUS_PROCESSING
            upload.started_at = upload.started_at or now
            upload.completed_at = None
            upload.error_message = None
            upload.attempt_count = int(upload.attempt_count or 0) + 1
            upload.updated_at = now

            await session.commit()
            return ClaimedUploadJob(
                job_id=int(job.id),
                upload_id=int(upload.id),
                search_space_id=int(upload.search_space_id),
                week=int(upload.week),
                kind=str(upload.kind),
                title=str(upload.title),
                source_name=upload.source_name or "teacher-upload.pdf",
                storage_key=str(upload.storage_key),
            )

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

        await session.execute(
            update(AITADocument)
            .where(
                scoped,
                kind_filter,
                has_week,
                AITADocument.status["state"].astext != DocumentStatus.FAILED,
            )
            .values(status=_INACTIVE_STATUS, updated_at=now)
        )

        active_doc_rows = await session.execute(
            select(TeacherUpload.doc_id)
            .where(
                TeacherUpload.search_space_id == search_space_id,
                TeacherUpload.is_latest.is_(True),
                TeacherUpload.status == UPLOAD_STATUS_READY,
                TeacherUpload.week <= current_week,
                TeacherUpload.kind.in_(tuple(VALID_KINDS)),
                TeacherUpload.doc_id.is_not(None),
            )
        )
        active_doc_ids = [int(doc_id) for doc_id in active_doc_rows.scalars().all() if doc_id is not None]
        if active_doc_ids:
            await session.execute(
                update(AITADocument)
                .where(AITADocument.id.in_(active_doc_ids))
                .values(status=DocumentStatus.ready(), updated_at=now)
            )

    async def _index_existing_upload_async(
        self,
        *,
        claimed: ClaimedUploadJob,
        connector_doc: AITAConnectorDocument,
        items: List[Any],
        ingestion: TeacherPDFIngestionResult,
        source_sha256: str,
    ) -> None:
        async with get_async_session() as session:
            upload = await session.get(TeacherUpload, int(claimed.upload_id))
            if upload is None:
                raise ValueError(f"Unknown teacher upload: {claimed.upload_id}")

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
            if not DocumentStatus.is_state(indexed_doc.status, DocumentStatus.READY):
                reason = DocumentStatus.get_failure_reason(indexed_doc.status) or "Indexing failed for teacher upload"
                raise RuntimeError(reason)

            course_row = await self._get_or_create_teacher_course(
                session, search_space_id=int(upload.search_space_id)
            )
            now = _utc_now()
            previous_upload_rows = await session.execute(
                select(TeacherUpload.id, TeacherUpload.doc_id)
                .where(
                    TeacherUpload.search_space_id == int(upload.search_space_id),
                    TeacherUpload.week == int(upload.week),
                    TeacherUpload.kind == str(upload.kind),
                    TeacherUpload.is_latest.is_(True),
                    TeacherUpload.id != int(upload.id),
                )
            )
            previous_doc_ids = [
                int(doc_id)
                for _, doc_id in previous_upload_rows.all()
                if doc_id is not None
            ]

            await session.execute(
                update(TeacherUpload)
                .where(
                    TeacherUpload.search_space_id == int(upload.search_space_id),
                    TeacherUpload.week == int(upload.week),
                    TeacherUpload.kind == str(upload.kind),
                    TeacherUpload.is_latest.is_(True),
                    TeacherUpload.id != int(upload.id),
                )
                .values(
                    is_latest=False,
                    status=UPLOAD_STATUS_SUPERSEDED,
                    updated_at=now,
                )
            )
            if previous_doc_ids:
                await session.execute(
                    update(AITADocument)
                    .where(AITADocument.id.in_(previous_doc_ids))
                    .values(status=_INACTIVE_STATUS, updated_at=now)
                )

            upload.doc_id = int(indexed_doc.id) if indexed_doc.id is not None else None
            upload.page_count = ingestion.page_count or indexed_doc.page_count
            upload.is_latest = True
            upload.status = UPLOAD_STATUS_READY
            upload.error_message = None
            upload.completed_at = now
            upload.updated_at = now
            upload.warning_count = int(ingestion.warning_count or 0)
            upload.ocr_provider = ingestion.ocr_provider
            upload.ocr_summary = dict(ingestion.ocr_summary or {})
            upload.artifact_manifest = {
                "bucket": self.pages_bucket,
                **(ingestion.artifact_manifest or {}),
            }
            ocr_summary = ingestion.ocr_summary or {}
            is_ocr_degraded = bool(
                ocr_summary.get("warning_pages", 0) > 0
                and ocr_summary.get("mathpix_pages", 0) == 0
                and ocr_summary.get("native_plus_mathpix_pages", 0) == 0
            )
            upload.metadata_ = {
                **(upload.metadata_ or {}),
                "material_id": f"doc-{indexed_doc.id}" if indexed_doc.id is not None else None,
                "source_pdf_sha256": source_sha256,
                "storage_bucket": self.upload_bucket,
                "pages_bucket": self.pages_bucket,
                "storage_key": claimed.storage_key,
                "ocr_degraded": is_ocr_degraded,
                "ocr_provider": ingestion.ocr_provider,
                "ocr_summary": ingestion.ocr_summary,
                "artifact_manifest": ingestion.artifact_manifest,
            }

            job = await session.get(TeacherUploadJob, int(claimed.job_id))
            if job is not None:
                job.state = JOB_STATE_COMPLETED
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error = None
                job.updated_at = now

            await self._sync_week_activation(
                session,
                search_space_id=int(upload.search_space_id),
                current_week=int(course_row.current_week or 1),
            )
            await session.commit()

    async def _handle_job_failure_async(
        self,
        claimed: ClaimedUploadJob,
        error_message: str,
        *,
        terminal: bool = False,
    ) -> None:
        async with get_async_session() as session:
            upload = await session.get(TeacherUpload, int(claimed.upload_id))
            job = await session.get(TeacherUploadJob, int(claimed.job_id))
            now = _utc_now()
            message = _truncate_error(error_message) or "Teacher upload processing failed"

            attempts = int(job.attempt_count or 0) if job is not None else 0
            is_terminal = terminal or attempts >= self.max_retries
            if is_terminal and not terminal:
                log.warning(
                    "Teacher upload job exhausted retries upload_id=%s attempts=%d",
                    claimed.upload_id, attempts,
                )

            if job is not None:
                job.state = JOB_STATE_FAILED if is_terminal else JOB_STATE_QUEUED
                job.lease_owner = None
                job.lease_expires_at = None
                job.last_error = message
                job.updated_at = now

            if upload is not None:
                upload.status = UPLOAD_STATUS_FAILED if is_terminal else UPLOAD_STATUS_QUEUED
                upload.error_message = message
                upload.completed_at = now if is_terminal else None
                upload.updated_at = now

            await session.commit()

    def _build_upload_record(self, row: TeacherUpload) -> UploadRecord:
        meta = row.metadata_ or {}
        material_id = meta.get("material_id")
        if material_id is None and row.doc_id is not None:
            material_id = f"doc-{row.doc_id}"
        return UploadRecord(
            id=str(row.id),
            week=int(row.week),
            kind=str(row.kind),
            title=str(row.title),
            status=str(row.status or UPLOAD_STATUS_READY),
            uploaded_at=_iso(row.uploaded_at),
            source_name=row.source_name,
            page_count=row.page_count,
            index_path=None,
            doc_id=str(row.doc_id) if row.doc_id is not None else None,
            material_id=material_id,
            error_message=row.error_message,
            warning_count=int(row.warning_count or 0),
            started_at=_iso(row.started_at),
            completed_at=_iso(row.completed_at),
            ocr_provider=row.ocr_provider,
            ocr_summary=row.ocr_summary or {},
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

        return self._assemble_course_payload(
            search_space_id=int(search_space_id),
            course=str(space.name),
            slug=str(space.slug),
            current_week=int(course_row.current_week or 1),
            uploads=uploads,
        )

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

    def _assemble_course_payload(
        self,
        *,
        search_space_id: int,
        course: str,
        slug: str,
        current_week: int,
        uploads: List[TeacherUpload],
    ) -> Dict[str, Any]:
        """Build the teacher course payload from upload rows.

        Weekly kinds (notes/slides) populate the per-week grid; course-wide
        textbook rows populate a single course-level ``textbook`` section. Pure
        (no I/O) so it is unit-testable without a database.
        """
        def _kind_of(row: TeacherUpload) -> str:
            return (row.kind or "").strip().lower()

        weeks: List[Dict[str, Any]] = []
        for week_num in range(1, self.total_weeks + 1):
            week_rows = [row for row in uploads if int(row.week or 0) == week_num]
            notes_rows = [row for row in week_rows if _kind_of(row) == "notes"]
            slides_rows = [row for row in week_rows if _kind_of(row) == "slides"]
            weeks.append(
                {
                    "week": week_num,
                    "notes": self._build_section(notes_rows),
                    "slides": self._build_section(slides_rows),
                }
            )

        textbook_rows = [row for row in uploads if _kind_of(row) in COURSE_WIDE_KINDS]

        return {
            "search_space_id": int(search_space_id),
            "course": str(course),
            "slug": str(slug),
            "current_week": int(current_week or 1),
            "weeks": weeks,
            "textbook": self._build_section(textbook_rows),
        }

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
        return self._build_upload_record(row).to_dict()


__all__ = ["TeacherWeeklyStorage", "UploadRecord"]
