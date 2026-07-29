from __future__ import annotations

"""Shared visibility and metadata helpers for pgvector retrieval."""

from typing import Any, Dict

from sqlalchemy import exists, func, or_, select

from database.models import Course, Document, Upload

WEEKLY_UPLOAD_KINDS: tuple[str, ...] = ("notes", "slides")


def active_document_conditions(search_space_id: int) -> list[Any]:
    current_week_sq = (
        select(func.coalesce(Course.current_week, 1))
        .where(Course.id == Document.course_id)
        .scalar_subquery()
    )

    active_weekly_upload_exists = exists(
        select(1)
        .select_from(Upload)
        .where(
            Upload.document_id == Document.id,
            Upload.course_id == Document.course_id,
            Upload.is_latest.is_(True),
            Upload.status == "ready",
            Upload.kind.in_(WEEKLY_UPLOAD_KINDS),
            Upload.week <= current_week_sq,
        )
    )

    return [
        Document.course_id == int(search_space_id),
        Document.status == "ready",
        or_(
            Document.week.is_(None),
            ~Document.material_kind.in_(WEEKLY_UPLOAD_KINDS),
            active_weekly_upload_exists,
        ),
    ]


def build_chunk_metadata(document_metadata: Dict[str, Any] | None, page_number: Any) -> Dict[str, Any]:
    meta = dict(document_metadata or {})
    page_entry = _page_debug_entry(meta, page_number)
    page_asset = page_entry.get("page_asset") or {}

    return {
        "document_id": meta.get("document_id"),
        "teacher_upload_id": meta.get("teacher_upload_id"),
        "week": meta.get("week"),
        "kind": meta.get("kind"),
        "material_kind": meta.get("kind") or meta.get("material_kind"),
        "ocr_provider": meta.get("ocr_provider"),
        "ocr_confidence": page_entry.get("ocr_confidence"),
        "extraction_mode": page_entry.get("extraction_mode"),
        "page_asset": page_asset,
        "raw_latex": page_entry.get("latex_text"),
        "source_name": meta.get("source_name"),
        "source_pdf": meta.get("source_pdf"),
        "artifact_manifest": meta.get("artifact_manifest"),
        "warning_count": meta.get("warning_count"),
    }


def _page_debug_entry(document_metadata: Dict[str, Any], page_number: Any) -> Dict[str, Any]:
    try:
        page_num = int(page_number)
    except (TypeError, ValueError):
        return {}

    for entry in document_metadata.get("page_debug") or []:
        try:
            if int(entry.get("page")) == page_num:
                return dict(entry)
        except (TypeError, ValueError):
            continue
    return {}


__all__ = ["WEEKLY_UPLOAD_KINDS", "active_document_conditions", "build_chunk_metadata"]
