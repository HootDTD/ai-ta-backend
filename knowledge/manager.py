from __future__ import annotations

"""Management helpers for subject-specific knowledge materials."""

import hashlib
import logging
import os
import re
import shutil
import uuid
from collections import Counter
from contextlib import suppress
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select as sa_select
from sqlalchemy.exc import IntegrityError

from database.models import AITADocument, DocumentStatus, SearchSpace
from database.session import get_async_session, run_async

log = logging.getLogger(__name__)

_LAYOUT_MODULE = None


def _load_layout_module():
    """Dynamically load the layout embedder script as a module."""

    global _LAYOUT_MODULE
    if _LAYOUT_MODULE is not None:
        return _LAYOUT_MODULE

    script_path = Path(__file__).resolve().parent.parent / "text-embeder" / "layout_multimodal_embedder.py"
    spec = spec_from_file_location("backend_layout_embedder", str(script_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import layout embedder from {script_path}")

    module = module_from_spec(spec)
    if module is None or spec.loader is None:
        raise RuntimeError(f"Unable to create module from {script_path}")
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[assignment]
    _LAYOUT_MODULE = module
    return module


STORE_KINDS = {"textbook", "slides", "homework", "exams", "notes", "other"}


def _default_store_priority(kind: str) -> int:
    k = (kind or "").strip().lower()
    mapping = {
        "textbook": 100,
        "slides": 80,
        "notes": 75,
        "exams": 70,
        "homework": 60,
        "other": 50,
    }
    return mapping.get(k, 50)


def _index_items_to_pgvector(
    subject: str,
    title: str,
    kind: str,
    items: list,
    source_markdown: str,
    page_count: Optional[int],
    week: Optional[int],
    metadata: Optional[Dict[str, Any]],
) -> None:
    """Dual-write: index Item objects into the new pgvector schema.

    Called from add_pdf_material() and teacher_weekly.record_upload().
    Bridges to the shared background event loop via run_async().
    Failures are logged but not re-raised.
    """
    import hashlib as _hashlib

    from database.session import _get_session_factory, run_async as _run_async
    from indexing.connector_document import AITAConnectorDocument
    from indexing.document_hashing import compute_unique_identifier_hash, compute_content_hash
    from indexing.indexing_service import AITAIndexingService

    async def _run():
        try:
            from sqlalchemy import text as _text
            factory = _get_session_factory()
            async with factory() as session:
                slug = re.sub(r"[^a-z0-9]+", "-", subject.strip().lower()).strip("-")
                result = await session.execute(
                    _text("SELECT id FROM aita_search_spaces WHERE slug = :slug"),
                    {"slug": slug},
                )
                row = result.fetchone()
                if row is None:
                    log.warning(
                        "pgvector dual-write: SearchSpace not found for subject '%s' (slug='%s').",
                        subject, slug,
                    )
                    return
                search_space_id = row[0]

                unique_id = _hashlib.sha256(
                    f"{source_markdown[:200]}:{search_space_id}".encode()
                ).hexdigest()[:32]

                connector_doc = AITAConnectorDocument(
                    title=title,
                    source_markdown=source_markdown or title,
                    unique_id=unique_id,
                    document_type="EDUCATIONAL_FILE",
                    search_space_id=search_space_id,
                    material_kind=kind,
                    page_count=page_count,
                    week=week,
                    metadata=metadata or {},
                )

                service = AITAIndexingService(session)
                docs = await service.prepare_for_indexing([connector_doc])
                if not docs:
                    log.info("pgvector dual-write: document '%s' already up-to-date.", title)
                    return

                await service.index_from_items(docs[0], connector_doc, items)
                log.info("pgvector dual-write: indexed '%s' (%d items).", title, len(items))

        except Exception as exc:
            log.error("pgvector dual-write failed for '%s': %s", title, exc)

    try:
        _run_async(_run())
    except Exception as exc:
        log.error("pgvector dual-write runner error for '%s': %s", title, exc)


class KnowledgeManager:
    """Manage subject knowledge materials and their embedding indexes."""

    def __init__(self, base_dir: Optional[os.PathLike[str] | str] = None):
        if base_dir is not None:
            base_path = Path(base_dir)
        else:
            configured = os.getenv("KNOWLEDGE_BASE_DIR")
            if configured:
                base_path = Path(configured)
            else:
                base_path = Path(__file__).resolve().parent / "text-embeder" / "knowledge"
        self.base_dir = base_path
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _slugify(subject: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", subject.strip().lower())
        slug = slug.strip("-")
        return slug or "subject"

    def _subject_dir(self, subject: str, *, slug: Optional[str] = None) -> Path:
        actual_slug = slug or self._slugify(subject)
        return self.base_dir / actual_slug

    # ------------------------------------------------------------------
    # SQLAlchemy helpers (replacing Supabase REST calls)
    # ------------------------------------------------------------------
    def _get_or_create_search_space(self, subject: str) -> Dict[str, Any]:
        """Get or create a SearchSpace row for the given subject."""
        return run_async(self._get_or_create_search_space_async(subject))

    async def _get_or_create_search_space_async(self, subject: str) -> Dict[str, Any]:
        slug = self._slugify(subject)
        async with get_async_session() as session:
            result = await session.execute(
                sa_select(SearchSpace).where(SearchSpace.slug == slug)
            )
            space = result.scalars().first()
            if space:
                return {"id": space.id, "name": space.name, "slug": space.slug, "subject_name": space.subject_name}

            space = SearchSpace(
                name=subject,
                slug=slug,
                subject_name=subject,
            )
            session.add(space)
            try:
                await session.commit()
                await session.refresh(space)
            except IntegrityError:
                await session.rollback()
                result = await session.execute(
                    sa_select(SearchSpace).where(SearchSpace.slug == slug)
                )
                space = result.scalars().first()

            return {"id": space.id, "name": space.name, "slug": space.slug, "subject_name": space.subject_name}

    def load_manifest(self, subject: str) -> Dict[str, Any]:
        """Load subject manifest from the database."""
        return run_async(self._load_manifest_async(subject))

    async def _load_manifest_async(self, subject: str) -> Dict[str, Any]:
        subject = subject.strip()
        slug = self._slugify(subject)
        async with get_async_session() as session:
            result = await session.execute(
                sa_select(SearchSpace).where(SearchSpace.slug == slug)
            )
            space = result.scalars().first()
            if not space:
                return {"subject": subject, "slug": slug, "materials": [], "stores": []}

            result = await session.execute(
                sa_select(AITADocument)
                .where(AITADocument.search_space_id == space.id)
                .order_by(AITADocument.created_at.desc())
            )
            docs = result.scalars().all()

            stores = []
            for doc in docs:
                meta = doc.document_metadata or {}
                stores.append({
                    "id": str(doc.id),
                    "document_id": doc.id,
                    "kind": doc.material_kind or "other",
                    "title": doc.title or "",
                    "index_path": meta.get("index_path", ""),
                    "priority": meta.get("priority", _default_store_priority(doc.material_kind or "other")),
                    "created_at": str(doc.created_at or ""),
                })

            return {
                "subject": space.name or subject,
                "slug": space.slug or slug,
                "materials": [],
                "stores": stores,
            }

    # ------------------------------------------------------------------
    # Public queries
    # ------------------------------------------------------------------
    def list_subjects(self) -> List[Dict[str, Any]]:
        return run_async(self._list_subjects_async())

    async def _list_subjects_async(self) -> List[Dict[str, Any]]:
        subjects: List[Dict[str, Any]] = []
        async with get_async_session() as session:
            result = await session.execute(
                sa_select(SearchSpace).order_by(SearchSpace.name)
            )
            spaces = result.scalars().all()

            for space in spaces:
                doc_result = await session.execute(
                    sa_select(AITADocument)
                    .where(AITADocument.search_space_id == space.id)
                    .order_by(AITADocument.created_at.desc())
                )
                docs = doc_result.scalars().all()

                stores = []
                for doc in docs:
                    meta = doc.document_metadata or {}
                    stores.append({
                        "id": str(doc.id),
                        "document_id": doc.id,
                        "kind": doc.material_kind or "other",
                        "title": doc.title or "",
                        "index_path": meta.get("index_path", ""),
                        "priority": meta.get("priority", _default_store_priority(doc.material_kind or "other")),
                        "created_at": str(doc.created_at or ""),
                    })

                manifest = {"subject": space.name, "slug": space.slug, "materials": [], "stores": stores}
                normalized = self._normalize_subject(space.name, space.slug, manifest)
                subjects.append(normalized)
        return subjects

    def get_subject(self, subject: str) -> Dict[str, Any]:
        manifest = self.load_manifest(subject)
        return self._normalize_subject(
            manifest.get("subject", subject),
            manifest.get("slug") or self._slugify(subject),
            manifest,
        )

    def resolve_doc_sets(self, subject: str) -> List[Path]:
        subject = subject.strip()
        if not subject:
            return []
        normalized = self.get_subject(subject)
        doc_sets: List[Path] = []

        # Include stores sorted by priority
        stores = normalized.get("stores", []) or []
        for st in stores:
            p = st.get("index_path")
            if not p:
                continue
            path = Path(p)
            if path.exists():
                doc_sets.append(path)

        if doc_sets:
            return doc_sets

        # Fallback for legacy single-index setups
        fallback = self._default_index_path()
        if fallback is not None:
            return [fallback]
        return []

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def add_pdf_material(
        self,
        subject: str,
        pdf_path: Path,
        *,
        title: Optional[str] = None,
        embed_model: Optional[str] = None,
        embed_dim: Optional[int] = None,
        token_limit: Optional[int] = None,
        overlap_tokens: Optional[int] = None,
        min_figure_area_ratio: Optional[float] = None,
        caption_model: Optional[str] = None,
        do_ocr: Optional[bool] = None,
    ) -> Dict[str, Any]:
        subject = subject.strip()
        if not subject:
            raise ValueError("subject must be a non-empty string")
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        layout = _load_layout_module()
        if not getattr(layout, "HAVE_NUMPY", False):
            raise RuntimeError("numpy is required to embed knowledge materials")

        embed_model = (embed_model or os.getenv("KNOWLEDGE_EMBED_MODEL") or "text-embedding-3-large").strip()
        embed_dim = int(embed_dim or os.getenv("KNOWLEDGE_EMBED_DIM", "3072"))
        token_limit = int(token_limit or os.getenv("KNOWLEDGE_TOKEN_LIMIT", "1000"))
        overlap_tokens = int(overlap_tokens or os.getenv("KNOWLEDGE_OVERLAP_TOKENS", "150"))
        min_figure_area_ratio = float(min_figure_area_ratio or os.getenv("KNOWLEDGE_MIN_FIGURE_AREA", "0.01"))
        caption_default = os.getenv("KNOWLEDGE_CAPTION_MODEL")
        if caption_model is None:
            caption_model = caption_default if caption_default is not None else "Salesforce/blip2-opt-2.7b"
        caption_model = caption_model.strip()
        if caption_model.lower() in {"", "none", "null"}:
            caption_model = ""
        if do_ocr is None:
            do_ocr = os.getenv("KNOWLEDGE_OCR", "on").lower() not in {"0", "off", "false", "no"}

        title = (title or pdf_path.stem).strip() or pdf_path.stem
        slug = self._slugify(subject)
        subject_dir = self._subject_dir(subject, slug=slug)
        subject_dir.mkdir(parents=True, exist_ok=True)

        material_id = uuid.uuid4().hex
        doc_id = f"{slug}-{material_id[:8]}"
        out_dir = subject_dir / f"km_{material_id}"
        out_dir.mkdir(parents=True, exist_ok=False)
        items_path = out_dir / "items.jsonl"

        try:
            items = layout.extract_document(
                pdf_path,
                doc_id,
                out_dir,
                token_limit=token_limit,
                overlap_tokens=overlap_tokens,
                min_figure_area_ratio=min_figure_area_ratio,
                caption_model=caption_model,
                items_path=items_path,
                do_ocr=do_ocr,
            )
            items = [it for it in items if (getattr(it, "text", "") or "").strip()]
            if not items:
                raise RuntimeError("No extractable text found in knowledge material")

            with items_path.open("w", encoding="utf-8") as fh:
                for item in items:
                    fh.write(item.to_json() + "\n")

            embeddings = layout.embed_items(items, embed_model, embed_dim)
            if embeddings.shape[0] != len(items):
                raise RuntimeError("Embedding output mismatch")

            layout.np.save(out_dir / "embeddings.npy", embeddings)
            layout.build_faiss(embeddings, out_dir / "faiss.index")
            layout.build_sqlite(items, out_dir / "sqlite.db")

            pdf_bytes = pdf_path.read_bytes()
            doc_hash = hashlib.sha256(pdf_bytes).hexdigest()
            counts = Counter(getattr(it, "type", "") for it in items)
            page_count = max((getattr(it, "page", 0) for it in items), default=0)
            tokenizer_name = getattr(layout.ENCODER, "name", type(layout.ENCODER).__name__)
            fitz_version = getattr(layout, "fitz", None)
            fitz_version = getattr(fitz_version, "__doc__", None) if getattr(layout, "HAVE_FITZ", False) else None
            torch_version = getattr(getattr(layout, "torch", None), "__version__", None) if getattr(layout, "HAVE_VISION", False) else None
            meta = {
                "subject": subject,
                "source_pdf": pdf_path.name,
                "source_pdf_sha256": doc_hash,
                "model": embed_model,
                "dimensions": int(embeddings.shape[1]) if embeddings.ndim == 2 else embed_dim,
                "num_items": len(items),
                "counts_by_type": dict(counts),
                "page_count": page_count,
                "has_faiss": getattr(layout, "HAVE_FAISS", False),
                "has_ocr": bool(do_ocr and getattr(layout, "HAVE_TESS", False)),
                "caption_model": caption_model or None,
                "tokenizer": tokenizer_name,
                "token_limit": token_limit,
                "overlap_tokens": overlap_tokens,
                "min_figure_area_ratio": min_figure_area_ratio,
                "fitz_version": fitz_version,
                "torch_version": torch_version,
                "created_at": datetime.utcnow().isoformat() + "Z",
                "doc_titles": {doc_id: title},
                "aliases": {doc_id: title},
            }
            (out_dir / "meta.json").write_text(__import__("json").dumps(meta, indent=2), encoding="utf-8")

            # Index into pgvector schema (the only write path now)
            _index_items_to_pgvector(
                subject=subject,
                title=title,
                kind="textbook",
                items=items,
                source_markdown="\n\n".join(
                    (getattr(it, "text", "") or "") for it in items
                ),
                page_count=page_count,
                week=None,
                metadata={"source_pdf": pdf_path.name, "index_path": str(out_dir.resolve())},
            )

        except Exception:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise

        self._delete_ocr_pngs(out_dir)
        self._safe_unlink(pdf_path)

        return {
            "id": material_id,
            "subject": subject,
            "title": title,
            "doc_id": doc_id,
            "index_dir": out_dir.name,
            "index_path": str(out_dir.resolve()),
            "created_at": meta["created_at"],
            "model": embed_model,
            "dimensions": meta["dimensions"],
            "page_count": page_count,
            "source_sha256": doc_hash,
        }

    # ------------------------------------------------------------------
    # Internal utilities
    # ------------------------------------------------------------------
    def _default_index_path(self) -> Optional[Path]:
        env_path = os.getenv("INDEX_DIR")
        if env_path:
            candidate = Path(env_path)
            if candidate.exists():
                return candidate
        fallback = Path(__file__).resolve().parent / "text-embeder" / "my_book_index_aero"
        if fallback.exists():
            return fallback
        return None

    def _normalize_subject(self, subject: str, slug: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
        subject = subject.strip() or slug
        slug = slug or self._slugify(subject)
        stores: List[Dict[str, Any]] = []
        for entry in manifest.get("stores", []) or []:
            normalized = self._normalize_store_entry(subject, slug, entry)
            if normalized:
                stores.append(normalized)
        stores.sort(key=lambda s: (int(s.get("priority", 0) or 0), s.get("created_at", "")), reverse=True)
        return {"subject": subject, "slug": slug, "materials": [], "stores": stores}

    def _normalize_store_entry(self, subject: str, slug: str, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        index_path = entry.get("index_path")
        if not index_path:
            # For pgvector-only documents, return the entry without validating index dir
            kind = (entry.get("kind") or "other").strip().lower()
            return {
                "id": entry.get("id", ""),
                "subject": subject,
                "kind": kind,
                "title": entry.get("title", ""),
                "priority": int(entry.get("priority", _default_store_priority(kind)) or _default_store_priority(kind)),
                "index_path": "",
                "created_at": entry.get("created_at", ""),
            }
        p = Path(index_path)
        if not self._is_valid_index_dir(p):
            # Still return entry for pgvector-backed documents without local index
            kind = (entry.get("kind") or "other").strip().lower()
            return {
                "id": entry.get("id", ""),
                "subject": subject,
                "kind": kind,
                "title": entry.get("title", ""),
                "priority": int(entry.get("priority", _default_store_priority(kind)) or _default_store_priority(kind)),
                "index_path": str(p),
                "created_at": entry.get("created_at", ""),
            }
        kind = (entry.get("kind") or "other").strip().lower()
        return {
            "id": entry.get("id", ""),
            "subject": subject,
            "kind": kind,
            "title": entry.get("title", ""),
            "priority": int(entry.get("priority", _default_store_priority(kind)) or _default_store_priority(kind)),
            "index_path": str(p),
            "created_at": entry.get("created_at", ""),
        }

    @staticmethod
    def _is_valid_index_dir(path: Path) -> bool:
        if not path.exists() or not path.is_dir():
            return False
        required = ["meta.json", "items.jsonl", "embeddings.npy"]
        for name in required:
            if not (path / name).exists():
                return False
        return True

    def _delete_ocr_pngs(self, index_dir: Path) -> None:
        images_dir = index_dir / "images"
        if not images_dir.exists():
            return
        for png in images_dir.glob("*_ocr.png"):
            with suppress(Exception):
                png.unlink()
        has_files = any(images_dir.iterdir())
        if not has_files:
            with suppress(Exception):
                images_dir.rmdir()

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        with suppress(FileNotFoundError):
            path.unlink()

    # ------------------------------------------------------------------
    # Stores (textbook/slides/homework/exams) helpers
    # ------------------------------------------------------------------
    def list_stores(self, subject: str) -> List[Dict[str, Any]]:
        """Return normalized store entries for a subject."""
        manifest = self.load_manifest(subject)
        norm = self._normalize_subject(
            manifest.get("subject", subject),
            manifest.get("slug") or self._slugify(subject),
            manifest,
        )
        return norm.get("stores", [])

    def find_store_entry(self, index_path: Path) -> Optional[Dict[str, Any]]:
        """Locate a store entry across subjects by absolute index path."""
        return run_async(self._find_store_entry_async(index_path))

    async def _find_store_entry_async(self, index_path: Path) -> Optional[Dict[str, Any]]:
        normalized_path = str(index_path.resolve())
        async with get_async_session() as session:
            result = await session.execute(
                sa_select(AITADocument).where(
                    AITADocument.document_metadata["index_path"].astext == normalized_path
                )
            )
            doc = result.scalars().first()
            if not doc:
                return None

            # Look up the search space
            space_result = await session.execute(
                sa_select(SearchSpace).where(SearchSpace.id == doc.search_space_id)
            )
            space = space_result.scalars().first()
            subject_name = space.name if space else ""
            slug = space.slug if space else ""

            meta = doc.document_metadata or {}
            entry = {
                "id": str(doc.id),
                "document_id": doc.id,
                "kind": doc.material_kind or "other",
                "title": doc.title or "",
                "index_path": meta.get("index_path", ""),
                "priority": meta.get("priority", _default_store_priority(doc.material_kind or "other")),
                "created_at": str(doc.created_at or ""),
            }
            return self._normalize_store_entry(subject_name, slug, entry)

    def register_store(
        self,
        subject: str,
        *,
        kind: str,
        title: str,
        index_path: Path,
        priority: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Register an existing index directory as a document in the database."""
        subject = subject.strip()
        if not subject:
            raise ValueError("subject must be a non-empty string")
        kind_norm = (kind or "other").strip().lower()
        if kind_norm not in STORE_KINDS:
            raise ValueError("invalid kind; must be one of: " + ", ".join(sorted(STORE_KINDS)))
        if not index_path.exists():
            raise FileNotFoundError("index_path not found")
        if not self._is_valid_index_dir(index_path):
            raise RuntimeError("index_path does not look like a valid index directory")

        return run_async(self._register_store_async(subject, kind_norm, title, index_path, priority))

    async def _register_store_async(
        self,
        subject: str,
        kind_norm: str,
        title: str,
        index_path: Path,
        priority: Optional[int],
    ) -> Dict[str, Any]:
        space_row = self._get_or_create_search_space(subject)
        slug = space_row.get("slug") or self._slugify(subject)
        key_path = str(index_path.resolve())
        computed_priority = int(priority) if priority is not None else _default_store_priority(kind_norm)

        async with get_async_session() as session:
            # Check if document with this index_path already exists
            result = await session.execute(
                sa_select(AITADocument).where(
                    AITADocument.search_space_id == space_row["id"],
                    AITADocument.document_metadata["index_path"].astext == key_path,
                    AITADocument.material_kind == kind_norm,
                )
            )
            existing = result.scalars().first()

            if existing:
                if priority is not None:
                    meta = dict(existing.document_metadata or {})
                    meta["priority"] = int(priority)
                    existing.document_metadata = meta
                    await session.commit()
                    await session.refresh(existing)
                doc = existing
            else:
                content_hash = hashlib.sha256(f"{key_path}:{space_row['id']}".encode()).hexdigest()
                doc = AITADocument(
                    title=title,
                    document_type="EDUCATIONAL_FILE",
                    material_kind=kind_norm,
                    content=title,
                    content_hash=content_hash,
                    search_space_id=space_row["id"],
                    document_metadata={
                        "index_path": key_path,
                        "priority": computed_priority,
                    },
                    status=DocumentStatus.ready(),
                )
                session.add(doc)
                await session.commit()
                await session.refresh(doc)

            entry = {
                "id": str(doc.id),
                "document_id": doc.id,
                "kind": doc.material_kind or kind_norm,
                "title": doc.title or title,
                "index_path": key_path,
                "priority": computed_priority,
                "created_at": str(doc.created_at or ""),
            }

        normalized = self._normalize_store_entry(subject, slug, entry)
        if not normalized:
            raise RuntimeError("failed to normalize registered store entry")
        return normalized


__all__ = ["KnowledgeManager"]
