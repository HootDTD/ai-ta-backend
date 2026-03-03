"""Teacher-facing weekly upload storage and indexing utilities."""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .manager import KnowledgeManager
from backend.config.weights import (
    WEIGHT_KINDS,
    WEIGHT_MIN,
    WEIGHT_MAX,
    get_env_weights,
    normalize_weights,
)
from backend.vendors import supabase_client as sb

log = logging.getLogger(__name__)

TOTAL_WEEKS_DEFAULT = 16
VALID_KINDS = {"notes", "slides"}


def _slugify_course(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    slug = "-".join(filter(None, slug.split("-")))
    return slug or "course"


def _utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


@dataclass
class UploadRecord:
    id: str
    week: int
    kind: str
    title: str
    uploaded_at: str
    source_name: str
    index_path: str
    doc_id: str
    page_count: Optional[int]
    material_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "week": self.week,
            "kind": self.kind,
            "title": self.title,
            "uploaded_at": self.uploaded_at,
            "source_name": self.source_name,
            "index_path": self.index_path,
            "doc_id": self.doc_id,
            "page_count": self.page_count,
            "material_id": self.material_id,
        }


class TeacherWeeklyStorage:
    """Manage per-course weekly uploads via Supabase + local index files."""

    def __init__(self, base_dir: Optional[os.PathLike[str] | str] = None, *, total_weeks: Optional[int] = None):
        root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent / "text-embeder" / "teacher_weekly"
        self.root = root
        self.total_weeks = int(total_weeks or os.getenv("TEACHER_TOTAL_WEEKS", TOTAL_WEEKS_DEFAULT))
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_root = self.root / "indexes"
        self.index_root.mkdir(parents=True, exist_ok=True)
        self._store_manager = KnowledgeManager()

    # ------------------------------------------------------------------
    # Supabase course helpers
    # ------------------------------------------------------------------
    def _get_or_create_course(self, course: str) -> Dict[str, Any]:
        """Get or create a course row in Supabase teacher_courses."""
        slug = _slugify_course(course)
        row = sb.select_one("teacher_courses", {"slug": f"eq.{slug}"})
        if row:
            return row
        defaults = get_env_weights()
        rows = sb.insert("teacher_courses", {
            "course": course,
            "slug": slug,
            "current_week": 1,
            "weights": defaults,
            "weight_bounds": {"min": WEIGHT_MIN, "max": WEIGHT_MAX},
        })
        return rows[0]

    # ------------------------------------------------------------------
    # Retrieval weight helpers
    # ------------------------------------------------------------------
    def get_retrieval_weights(self, course: str) -> Dict[str, float]:
        row = self._get_or_create_course(course)
        return dict(row.get("weights", {}))

    def update_retrieval_weights(self, course: str, weights: Mapping[str, Any]) -> Dict[str, float]:
        row = self._get_or_create_course(course)
        current = row.get("weights", {})
        updated = normalize_weights(weights, clamp=True, base=current)
        sb.update("teacher_courses", {"id": f"eq.{row['id']}"}, {
            "weights": updated,
            "weight_bounds": {"min": WEIGHT_MIN, "max": WEIGHT_MAX},
            "updated_at": "now()",
        })
        return dict(updated)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def list_course(self, course: str) -> Dict[str, Any]:
        row = self._get_or_create_course(course)
        course_id = row["id"]

        # Get all uploads for this course
        uploads = sb.select("teacher_uploads", {
            "course_id": f"eq.{course_id}",
            "order": "week.asc,kind.asc,uploaded_at.desc",
        })

        # Build weeks structure
        weeks_out: List[Dict[str, Any]] = []
        for week_num in range(1, self.total_weeks + 1):
            week_uploads = [u for u in uploads if u.get("week") == week_num]
            notes_section = self._build_section(week_uploads, "notes")
            slides_section = self._build_section(week_uploads, "slides")
            weeks_out.append({
                "week": week_num,
                "notes": notes_section,
                "slides": slides_section,
            })

        return {
            "course": row.get("course", course),
            "slug": row.get("slug", _slugify_course(course)),
            "current_week": int(row.get("current_week", 1) or 1),
            "weeks": weeks_out,
        }

    def _build_section(self, uploads: List[Dict[str, Any]], kind: str) -> Dict[str, Any]:
        """Build notes/slides section from upload records."""
        kind_uploads = [u for u in uploads if u.get("kind") == kind]
        latest = None
        history = []
        for u in kind_uploads:
            rec = self._upload_to_record(u)
            if rec is None:
                continue
            if u.get("is_latest"):
                latest = rec
            history.append(rec)
        return {"latest": latest, "history": history}

    def _upload_to_record(self, u: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert a Supabase upload row to a record dict."""
        if not u.get("index_path"):
            return None
        return {
            "id": u.get("id"),
            "week": int(u.get("week", 0) or 0),
            "kind": u.get("kind"),
            "title": u.get("title"),
            "uploaded_at": u.get("uploaded_at"),
            "source_name": u.get("source_name"),
            "index_path": u.get("index_path"),
            "doc_id": u.get("doc_id"),
            "page_count": u.get("page_count"),
            "material_id": u.get("material_id"),
        }

    def record_upload(
        self,
        course: str,
        *,
        week: int,
        kind: str,
        pdf_path: Path,
        title: Optional[str] = None,
    ) -> UploadRecord:
        if week < 1 or week > self.total_weeks:
            raise ValueError(f"week must be between 1 and {self.total_weeks}")
        kind_norm = kind.strip().lower()
        if kind_norm not in VALID_KINDS:
            raise ValueError("kind must be 'notes' or 'slides'")

        course_row = self._get_or_create_course(course)
        resolved_title = (title or f"Week {week} {kind_norm.title()}").strip()

        # TODO: wire pgvector-native ingestion via AITAIndexingService
        #       (the old FAISS handwriting pipeline has been removed)
        raise NotImplementedError(
            "Teacher upload ingestion needs to be ported to the pgvector "
            "AITAIndexingService pipeline. The old FAISS-based handwriting "
            "ingest has been removed."
        )

    def set_current_week(self, course: str, week: int) -> Dict[str, Any]:
        if week < 1 or week > self.total_weeks:
            raise ValueError(f"current_week must be between 1 and {self.total_weeks}")
        row = self._get_or_create_course(course)
        sb.update("teacher_courses", {"id": f"eq.{row['id']}"}, {
            "current_week": week,
            "updated_at": "now()",
        })
        return {"course": row.get("course", course), "current_week": week}

    def resolve_week_doc_sets(self, course: str, *, week: Optional[int] = None, include_previous: bool = False) -> List[str]:
        row = self._get_or_create_course(course)
        target_week = week or int(row.get("current_week", 1) or 1)
        target_week = max(1, min(target_week, self.total_weeks))

        if include_previous:
            uploads = sb.select("teacher_uploads", {
                "course_id": f"eq.{row['id']}",
                "is_latest": "eq.true",
                "week": f"lte.{target_week}",
                "order": "week.asc",
            })
        else:
            uploads = sb.select("teacher_uploads", {
                "course_id": f"eq.{row['id']}",
                "is_latest": "eq.true",
                "week": f"eq.{target_week}",
            })

        paths: List[str] = []
        for u in uploads:
            idx = u.get("index_path")
            if not idx:
                continue
            p = Path(idx)
            if p.exists():
                resolved = str(p.resolve())
                if resolved not in paths:
                    paths.append(resolved)
        return paths

    def _env_int(self, default: int, *names: str) -> int:
        for name in names:
            if not name:
                continue
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        return default

    def _env_str(self, default: str, *names: str) -> str:
        for name in names:
            if not name:
                continue
            raw = os.getenv(name)
            if raw and raw.strip():
                return raw.strip()
        return default

    def _register_store_entry(self, course: str, kind: str, week: int, title: str, index_path: str) -> None:
        if not index_path:
            return
        path = Path(index_path)
        if not path.exists():
            return
        store_kind = "slides" if kind == "slides" else "notes" if kind == "notes" else "other"
        try:
            self._store_manager.register_store(
                course,
                kind=store_kind,
                title=title,
                index_path=path,
            )
        except Exception as exc:
            log.debug("Failed to register store entry for %s (kind=%s): %s", title, store_kind, exc)


__all__ = ["TeacherWeeklyStorage", "UploadRecord"]
