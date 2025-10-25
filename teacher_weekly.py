"""Teacher-facing weekly upload storage and indexing utilities."""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.indexers.handwriting import ingest as handwriting_ingest, IngestOptions
from backend.knowledge import KnowledgeManager

log = logging.getLogger(__name__)

TOTAL_WEEKS_DEFAULT = 16
VALID_KINDS = {"notes", "slides"}


def _slugify_course(name: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in name.strip())
    slug = "-".join(filter(None, slug.split("-")))
    return slug or "course"


def _utc_now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _empty_section() -> Dict[str, Any]:
    return {"latest": None, "history": []}


def _empty_week_block() -> Dict[str, Any]:
    return {"notes": _empty_section(), "slides": _empty_section()}


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
    """Manage per-course weekly uploads and embedding directories."""

    def __init__(self, base_dir: Optional[os.PathLike[str] | str] = None, *, total_weeks: Optional[int] = None):
        root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent / "text-embeder" / "teacher_weekly"
        self.root = root
        self.total_weeks = int(total_weeks or os.getenv("TEACHER_TOTAL_WEEKS", TOTAL_WEEKS_DEFAULT))
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_root = self.root / "indexes"
        self.index_root.mkdir(parents=True, exist_ok=True)
        self.manifest_root = self.root / "courses"
        self.manifest_root.mkdir(parents=True, exist_ok=True)
        self._store_manager = KnowledgeManager()

    # ------------------------------------------------------------------
    # Manifest helpers
    # ------------------------------------------------------------------
    def _manifest_path(self, course: str) -> Path:
        return self.manifest_root / f"{_slugify_course(course)}.json"

    def _ensure_weeks(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        weeks = manifest.setdefault("weeks", {})
        for week in range(1, self.total_weeks + 1):
            key = str(week)
            block = weeks.get(key)
            if not isinstance(block, dict):
                block = _empty_week_block()
            else:
                for kind in VALID_KINDS:
                    section = block.get(kind)
                    if not isinstance(section, dict):
                        block[kind] = _empty_section()
                        continue
                    if not isinstance(section.get("history"), list):
                        section["history"] = []
                    if "latest" in section and section["latest"] is not None:
                        section["latest"] = dict(section["latest"])
                    else:
                        section["latest"] = None
            weeks[key] = block
        manifest["weeks"] = weeks
        return manifest

    def _load_manifest(self, course: str) -> Dict[str, Any]:
        path = self._manifest_path(course)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"Malformed teacher manifest for {course!r}: {exc}") from exc
        else:
            data = {
                "course": course,
                "slug": _slugify_course(course),
                "current_week": 1,
                "weeks": {},
            }
        data["course"] = course
        data["slug"] = data.get("slug") or _slugify_course(course)
        if not isinstance(data.get("current_week"), int):
            data["current_week"] = 1
        data = self._ensure_weeks(data)
        return data

    def _save_manifest(self, course: str, manifest: Dict[str, Any]) -> None:
        path = self._manifest_path(course)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def list_course(self, course: str) -> Dict[str, Any]:
        manifest = self._load_manifest(course)
        weeks_out: List[Dict[str, Any]] = []
        for week in range(1, self.total_weeks + 1):
            block = manifest["weeks"].get(str(week), _empty_week_block())
            weeks_out.append(
                {
                    "week": week,
                    "notes": self._serialize_section(block.get("notes")),
                    "slides": self._serialize_section(block.get("slides")),
                }
            )
        return {
            "course": manifest["course"],
            "slug": manifest["slug"],
            "current_week": int(manifest.get("current_week", 1) or 1),
            "weeks": weeks_out,
        }

    def _serialize_section(self, section: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(section, dict):
            section = _empty_section()
        latest = section.get("latest")
        history = [self._coerce_record(rec) for rec in section.get("history", [])]
        history = [rec for rec in history if rec is not None]
        out = {
            "latest": self._coerce_record(latest),
            "history": history,
        }
        return out

    def _coerce_record(self, rec: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(rec, dict):
            return None
        out = {
            "id": rec.get("id"),
            "week": int(rec.get("week", 0) or 0),
            "kind": rec.get("kind"),
            "title": rec.get("title"),
            "uploaded_at": rec.get("uploaded_at"),
            "source_name": rec.get("source_name"),
            "index_path": rec.get("index_path"),
            "doc_id": rec.get("doc_id"),
            "page_count": rec.get("page_count"),
            "material_id": rec.get("material_id"),
        }
        return out if out.get("index_path") else None

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

        manifest = self._load_manifest(course)
        resolved_title = (title or f"Week {week} {kind_norm.title()}").strip()
        try:
            ingestion = self._ingest_with_handwriting(
                slug=manifest["slug"],
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

        self._register_store_entry(course, kind_norm, week, record.title, record.index_path)

        week_block = manifest["weeks"].get(str(week)) or _empty_week_block()
        section = week_block.get(kind_norm) or _empty_section()
        history: List[Dict[str, Any]] = [rec for rec in section.get("history", []) if isinstance(rec, dict)]
        history.insert(0, record.to_dict())
        section["history"] = history
        section["latest"] = record.to_dict()
        week_block[kind_norm] = section
        manifest["weeks"][str(week)] = week_block
        self._save_manifest(course, manifest)
        return record

    def set_current_week(self, course: str, week: int) -> Dict[str, Any]:
        if week < 1 or week > self.total_weeks:
            raise ValueError(f"current_week must be between 1 and {self.total_weeks}")
        manifest = self._load_manifest(course)
        manifest["current_week"] = week
        self._save_manifest(course, manifest)
        return {
            "course": manifest["course"],
            "current_week": week,
        }

    def resolve_week_doc_sets(self, course: str, *, week: Optional[int] = None, include_previous: bool = False) -> List[str]:
        manifest = self._load_manifest(course)
        target_week = week or int(manifest.get("current_week", 1) or 1)
        target_week = max(1, min(target_week, self.total_weeks))
        paths: List[str] = []
        weeks_to_collect = range(1, target_week + 1) if include_previous else [target_week]
        for wk in weeks_to_collect:
            block = manifest["weeks"].get(str(wk)) or {}
            for kind in VALID_KINDS:
                section = block.get(kind) or {}
                latest = section.get("latest") or {}
                idx = latest.get("index_path")
                if not idx:
                    continue
                p = Path(idx)
                if p.exists():
                    resolved = str(p.resolve())
                    if resolved not in paths:
                        paths.append(resolved)
        return paths

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
            manifest = self._load_manifest(course)
            stores = [entry for entry in manifest.get("stores", []) if isinstance(entry, dict)]
            for entry in stores:
                if entry.get("index_path") == str(path.resolve()):
                    entry["week"] = week
            manifest["stores"] = stores
            self._save_manifest(course, manifest)
        except Exception as exc:
            log.debug("Failed to register store entry for %s (kind=%s): %s", title, store_kind, exc)


__all__ = ["TeacherWeeklyStorage", "UploadRecord"]
