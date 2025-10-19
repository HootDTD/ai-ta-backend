from __future__ import annotations

"""Management helpers for subject-specific knowledge materials."""

import hashlib
import json
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

log = logging.getLogger(__name__)

_LAYOUT_MODULE = None


def _load_layout_module():
    """Dynamically load the layout embedder script as a module."""

    global _LAYOUT_MODULE
    if _LAYOUT_MODULE is not None:
        return _LAYOUT_MODULE

    script_path = Path(__file__).resolve().parent / "text-embeder" / "layout_multimodal_embedder.py"
    spec = spec_from_file_location("backend_layout_embedder", str(script_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to import layout embedder from {script_path}")

    module = module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[assignment]
    _LAYOUT_MODULE = module
    return module


STORE_KINDS = {"textbook", "slides", "homework", "exams", "other"}


def _default_store_priority(kind: str) -> int:
    k = (kind or "").strip().lower()
    # Keep textbook highest priority by default
    mapping = {
        "textbook": 100,
        "slides": 80,
        "exams": 70,
        "homework": 60,
        "other": 50,
    }
    return mapping.get(k, 50)


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
    # Manifest helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _slugify(subject: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", subject.strip().lower())
        slug = slug.strip("-")
        return slug or "subject"

    def _subject_dir(self, subject: str, *, slug: Optional[str] = None) -> Path:
        actual_slug = slug or self._slugify(subject)
        return self.base_dir / actual_slug

    def _manifest_path(self, subject: str, *, slug: Optional[str] = None) -> Path:
        return self._subject_dir(subject, slug=slug) / "manifest.json"

    def load_manifest(self, subject: str) -> Dict[str, Any]:
        subject = subject.strip()
        slug = self._slugify(subject)
        path = self._manifest_path(subject, slug=slug)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"Malformed manifest for subject {subject!r}: {exc}") from exc
            data.setdefault("subject", subject)
            data.setdefault("slug", slug)
            data.setdefault("materials", [])
            data.setdefault("stores", [])
            return data
        return {"subject": subject, "slug": slug, "materials": [], "stores": []}

    def _save_manifest(self, subject: str, manifest: Dict[str, Any]) -> None:
        slug = manifest.get("slug") or self._slugify(subject)
        path = self._manifest_path(subject, slug=slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------------
    # Public queries
    # ------------------------------------------------------------------
    def list_subjects(self) -> List[Dict[str, Any]]:
        subjects: List[Dict[str, Any]] = []
        if not self.base_dir.exists():
            return subjects
        for manifest_path in sorted(self.base_dir.glob("*/manifest.json")):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            subject_name = str(data.get("subject") or "").strip()
            slug = data.get("slug") or manifest_path.parent.name
            normalized = self._normalize_subject(subject_name or slug, slug, data)
            subjects.append(normalized)
        return subjects

    def get_subject(self, subject: str) -> Dict[str, Any]:
        manifest = self.load_manifest(subject)
        return self._normalize_subject(manifest.get("subject", subject), manifest.get("slug") or self._slugify(subject), manifest)

    def resolve_doc_sets(self, subject: str) -> List[Path]:
        subject = subject.strip()
        if not subject:
            return []
        normalized = self.get_subject(subject)
        doc_sets: List[Path] = []
        for entry in normalized.get("materials", []):
            path_str = entry.get("index_path")
            if not path_str:
                continue
            path = Path(path_str)
            if path.exists():
                doc_sets.append(path)

        # Include stores if present, sorted by priority (desc) then created_at desc
        stores = normalized.get("stores", []) or []
        try:
            stores_sorted = sorted(
                stores,
                key=lambda s: (
                    int(s.get("priority", 0) or 0),
                    (s.get("created_at") or ""),
                ),
                reverse=True,
            )
        except Exception:
            stores_sorted = stores  # best effort
        for st in stores_sorted:
            p = st.get("index_path")
            if not p:
                continue
            path = Path(p)
            if path.exists():
                doc_sets.append(path)
        if doc_sets:
            return doc_sets
        # Fallback for legacy single-index setups when subject matches the default
        default_subject = os.getenv("DEFAULT_SUBJECT_NAME", "course/textbook")
        if subject == default_subject:
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
            (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

            manifest = self.load_manifest(subject)
            manifest["subject"] = subject
            manifest["slug"] = slug
            entry = {
                "id": material_id,
                "title": title,
                "doc_id": doc_id,
                "index_dir": out_dir.name,
                "created_at": meta["created_at"],
                "model": embed_model,
                "dimensions": meta["dimensions"],
                "page_count": page_count,
                "source_sha256": doc_hash,
            }
            materials = [m for m in manifest.get("materials", []) if m.get("id") != material_id]
            materials.append(entry)
            materials.sort(key=lambda m: m.get("created_at", ""), reverse=True)
            manifest["materials"] = materials
            self._save_manifest(subject, manifest)

        except Exception:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise

        self._delete_ocr_pngs(out_dir)
        self._safe_unlink(pdf_path)
        normalized = self._normalize_material_entry(subject, slug, entry)
        if not normalized:
            raise RuntimeError("Embedded material is not accessible after registration")
        return normalized

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
        materials: List[Dict[str, Any]] = []
        for entry in manifest.get("materials", []):
            normalized = self._normalize_material_entry(subject, slug, entry)
            if normalized:
                materials.append(normalized)
        materials.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        stores: List[Dict[str, Any]] = []
        for entry in manifest.get("stores", []) or []:
            normalized = self._normalize_store_entry(subject, slug, entry)
            if normalized:
                stores.append(normalized)
        # Sort stores by priority desc then date desc
        stores.sort(key=lambda s: (int(s.get("priority", 0) or 0), s.get("created_at", "")), reverse=True)
        return {"subject": subject, "slug": slug, "materials": materials, "stores": stores}

    def _normalize_material_entry(self, subject: str, slug: str, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        index_dir = entry.get("index_dir")
        if not index_dir:
            return None
        idx_path = self._subject_dir(subject, slug=slug) / index_dir
        if not self._is_valid_index_dir(idx_path):
            return None
        return {
            "id": entry.get("id", ""),
            "subject": subject,
            "title": entry.get("title", ""),
            "doc_id": entry.get("doc_id", ""),
            "index_dir": index_dir,
            "index_path": str(idx_path),
            "created_at": entry.get("created_at", ""),
            "model": entry.get("model"),
            "dimensions": entry.get("dimensions"),
            "page_count": entry.get("page_count"),
            "source_sha256": entry.get("source_sha256"),
        }

    def _normalize_store_entry(self, subject: str, slug: str, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        index_path = entry.get("index_path")
        if not index_path:
            return None
        p = Path(index_path)
        if not self._is_valid_index_dir(p):
            return None
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
        # remove directory if empty after cleanup
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
        norm = self._normalize_subject(manifest.get("subject", subject), manifest.get("slug") or self._slugify(subject), manifest)
        return norm.get("stores", [])

    def find_store_entry(self, index_path: Path) -> Optional[Dict[str, Any]]:
        """Locate a store entry across subjects by absolute index path."""
        normalized_path = str(index_path.resolve())
        if not self.base_dir.exists():
            return None
        for manifest_path in sorted(self.base_dir.glob("*/manifest.json")):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            subject_name = str(data.get("subject") or "").strip()
            slug = data.get("slug") or manifest_path.parent.name
            stores = data.get("stores") or []
            if not isinstance(stores, list):
                continue
            for entry in stores:
                if not isinstance(entry, dict):
                    continue
                if entry.get("index_path") == normalized_path:
                    subject = subject_name or slug
                    return self._normalize_store_entry(subject, slug, entry)
        return None

    def register_store(
        self,
        subject: str,
        *,
        kind: str,
        title: str,
        index_path: Path,
        priority: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Register an existing index directory as a store entry in the manifest.

        Idempotent: if an identical index_path+kind+title exists, it will be updated
        (e.g., priority) rather than duplicated.
        """
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

        manifest = self.load_manifest(subject)
        slug = manifest.get("slug") or self._slugify(subject)
        manifest["subject"] = subject
        manifest["slug"] = slug
        stores = [s for s in manifest.get("stores", []) if isinstance(s, dict)]

        # Match by absolute path + kind + title
        key_path = str(index_path.resolve())
        found = None
        for s in stores:
            if (s.get("index_path") == key_path) and (s.get("kind") == kind_norm) and (s.get("title") == title):
                found = s
                break

        import uuid

        if found is None:
            entry = {
                "id": uuid.uuid4().hex,
                "kind": kind_norm,
                "title": title,
                "index_path": key_path,
                "priority": int(priority) if priority is not None else _default_store_priority(kind_norm),
                "created_at": datetime.utcnow().isoformat() + "Z",
            }
            stores.append(entry)
        else:
            # Update priority if provided
            if priority is not None:
                try:
                    found["priority"] = int(priority)
                except (TypeError, ValueError):
                    pass

        # Sort and persist
        stores.sort(key=lambda s: (int(s.get("priority", 0) or 0), s.get("created_at", "")), reverse=True)
        manifest["stores"] = stores
        self._save_manifest(subject, manifest)

        normalized = self._normalize_store_entry(subject, slug, stores[0] if found is None else found)
        if not normalized:
            raise RuntimeError("failed to normalize registered store entry")
        return normalized


__all__ = ["KnowledgeManager"]
