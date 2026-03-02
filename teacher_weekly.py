"""Teacher-facing weekly upload storage and indexing utilities."""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from backend.indexers.handwriting import ingest as handwriting_ingest, IngestOptions
from backend.knowledge import KnowledgeManager
from backend.store_weights import (
    WEIGHT_KINDS,
    WEIGHT_MIN,
    WEIGHT_MAX,
    get_env_weights,
    normalize_weights,
)
from backend import supabase_client as sb

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

        try:
            ingestion = self._ingest_with_handwriting(
                slug=course_row.get("slug", _slugify_course(course)),
                week=week,
                kind=kind_norm,
                pdf_path=pdf_path,
                title=resolved_title,
            )
        except Exception as exc:
            raise RuntimeError(f"Teacher ingestion failed: {exc}") from exc

        index_path = ingestion.get("index_path")
        record = UploadRecord(
            id=uuid.uuid4().hex,
            week=week,
            kind=kind_norm,
            title=ingestion.get("title", resolved_title),
            uploaded_at=ingestion.get("created_at", _utc_now()),
            source_name=pdf_path.name,
            index_path=str(index_path) if index_path else "",
            doc_id=str(ingestion.get("doc_id", "")) if ingestion.get("doc_id") else "",
            page_count=ingestion.get("page_count"),
            material_id=str(ingestion.get("material_id", "")) if ingestion.get("material_id") else "",
        )

        # Mark old latest as not-latest
        sb.update("teacher_uploads", {
            "course_id": f"eq.{course_row['id']}",
            "week": f"eq.{week}",
            "kind": f"eq.{kind_norm}",
            "is_latest": "eq.true",
        }, {"is_latest": False})

        # Insert new upload as latest
        sb.insert("teacher_uploads", {
            "course_id": course_row["id"],
            "week": week,
            "kind": kind_norm,
            "title": record.title,
            "uploaded_at": record.uploaded_at,
            "source_name": record.source_name,
            "index_path": record.index_path,
            "doc_id": record.doc_id,
            "page_count": record.page_count,
            "material_id": record.material_id,
            "is_latest": True,
        })

        # Register in knowledge stores
        self._register_store_entry(course, kind_norm, week, record.title, record.index_path)

        # Dual-write to new pgvector schema (when USE_PGVECTOR_RETRIEVAL=true)
        from .config import use_pgvector_retrieval
        if use_pgvector_retrieval():
            self._dual_write_pgvector(
                course=course,
                kind=kind_norm,
                week=week,
                title=record.title,
                index_path=record.index_path,
                page_count=record.page_count,
            )

        return record

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

    # ------------------------------------------------------------------
    # Ingestion (local FAISS pipeline — unchanged)
    # ------------------------------------------------------------------
    def _ingest_with_handwriting(self, *, slug: str, week: int, kind: str, pdf_path: Path, title: str) -> Dict[str, Any]:
        slug = (slug or "").strip()
        slug = slug or _slugify_course(slug)
        base_dir = self.index_root / f"{slug}-week-{week:02d}-{kind}"
        base_dir.mkdir(parents=True, exist_ok=True)
        material_id = uuid.uuid4().hex
        out_dir = base_dir / f"km_{material_id}"
        out_dir.mkdir(parents=True, exist_ok=False)
        doc_id = f"{slug}-{kind}-{material_id[:8]}"
        opts = self._build_handwriting_options()
        try:
            handwriting_ingest(pdf_path, doc_id=doc_id, out_dir=out_dir, options=opts)
        except Exception:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        finally:
            with suppress(FileNotFoundError):
                pdf_path.unlink()

        import json
        meta: Dict[str, Any] = {}
        meta_path = out_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        store_kind = "slides" if kind == "slides" else "notes"
        meta["week"] = week
        meta["store_kind"] = store_kind
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return {
            "title": title,
            "index_path": str(out_dir.resolve()),
            "doc_id": doc_id,
            "material_id": material_id,
            "created_at": meta.get("created_at", _utc_now()),
            "page_count": meta.get("page_count"),
            "store_kind": store_kind,
            "week": week,
        }

    def _dual_write_pgvector(
        self,
        *,
        course: str,
        kind: str,
        week: int,
        title: str,
        index_path: str,
        page_count: Optional[int],
    ) -> None:
        """Dual-write teacher upload items into the pgvector schema.

        Reads items.jsonl from the freshly-created index directory and indexes
        them via AITAIndexingService. Failures are logged but not re-raised.
        """
        import json as _json
        from pathlib import Path as _Path
        from .knowledge import _index_items_to_pgvector

        items_path = _Path(index_path) / "items.jsonl"
        if not items_path.exists():
            log.warning("pgvector dual-write: items.jsonl not found at %s", items_path)
            return

        # Load Item-like objects from JSONL as simple namespaces
        items = []
        try:
            with items_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        d = _json.loads(line)
                        # Create a simple namespace that mimics Item attributes
                        items.append(type("Item", (), d)())
        except Exception as e:
            log.error("pgvector dual-write: failed reading items.jsonl: %s", e)
            return

        if not items:
            return

        source_markdown = "\n\n".join(
            getattr(it, "text", "") or getattr(it, "raw_text", "") or ""
            for it in items
        )

        _index_items_to_pgvector(
            subject=course,
            title=title,
            kind=kind,
            items=items,
            source_markdown=source_markdown,
            page_count=page_count,
            week=week,
            metadata={"index_path": index_path, "week": week},
        )

    def _build_handwriting_options(self) -> IngestOptions:
        token_limit = self._env_int(1000, "HANDWRITING_TOKEN_LIMIT", "KNOWLEDGE_TOKEN_LIMIT")
        overlap = self._env_int(150, "HANDWRITING_OVERLAP_TOKENS", "KNOWLEDGE_OVERLAP_TOKENS")
        embed_model = self._env_str("text-embedding-3-large", "HANDWRITING_EMBED_MODEL", "KNOWLEDGE_EMBED_MODEL")
        embed_dim = self._env_int(3072, "HANDWRITING_EMBED_DIM", "KNOWLEDGE_EMBED_DIM")
        workers = max(1, self._env_int(4, "HANDWRITING_WORKERS"))
        dpi_raw = self._env_str("", "HANDWRITING_DPI", "OCR_DPI")
        dpi = None
        if dpi_raw:
            try:
                dpi = int(dpi_raw)
            except ValueError:
                dpi = None
        return IngestOptions(
            token_limit=token_limit,
            overlap_tokens=overlap,
            embed_model=embed_model,
            embed_dim=embed_dim,
            workers=workers,
            dpi=dpi,
        )

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
