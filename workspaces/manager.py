from __future__ import annotations

"""Class workspace resolution with DB-backed isolation.

This module centralizes how the backend maps an incoming class identifier
to the scoped retrieval configuration for that course. Each workspace owns
its own materials (embedding directories), weighting bias, and metadata.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..config.weights import WEIGHT_KINDS, WEIGHT_MIN, WEIGHT_MAX, clamp_weight

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceMaterial:
    """A single retrievable material (embedded index) for a workspace."""

    id: str
    kind: str
    title: str
    index_path: Path
    priority: int = 0
    weight_override: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassWorkspace:
    """Resolved workspace describing one class's retrieval scope."""

    class_id: str
    class_name: str
    slug: str
    subject_name: str
    materials: List[WorkspaceMaterial]
    weight_overrides: Dict[str, float]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def sorted_materials(self) -> List[WorkspaceMaterial]:
        """Return materials sorted by priority (desc) then title."""

        def _sort_key(item: WorkspaceMaterial) -> Tuple[int, str]:
            return (-int(item.priority or 0), item.title.lower())

        return sorted(self.materials, key=_sort_key)

    def doc_sets(self) -> List[Path]:
        """Return the ordered list of embedding directories for the class."""

        return [material.index_path for material in self.sorted_materials()]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WorkspaceError(RuntimeError):
    """Base error for workspace resolution failures."""


class WorkspaceNotFound(WorkspaceError):
    """Raised when no workspace could be resolved for a class identifier."""


class WorkspaceConfigError(WorkspaceError):
    """Raised when a workspace record is malformed or incomplete."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    slug = "-".join(part for part in cleaned.split("-") if part)
    return slug or "class"


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except Exception:
        return False


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    return [value]


def _ensure_mapping(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _default_index_root() -> Path:
    configured = os.getenv("CLASS_INDEX_ROOT")
    if configured:
        try:
            return Path(configured).expanduser().resolve()
        except Exception as exc:
            raise WorkspaceConfigError(f"Invalid CLASS_INDEX_ROOT: {configured}") from exc
    return (Path(__file__).resolve().parent / "text-embeder").resolve()


# ---------------------------------------------------------------------------
# Repository base classes
# ---------------------------------------------------------------------------


class WorkspaceRepository:
    """Interface for resolving a class workspace."""

    def load_workspace(self, identifier: str) -> ClassWorkspace:
        raise NotImplementedError


class StaticWorkspaceRepository(WorkspaceRepository):
    """Repository reading from an in-memory mapping (legacy fallback)."""

    def __init__(self, config: Mapping[str, Mapping[str, Any]]):
        self._config = dict(config)
        self._index_root = _default_index_root()

    def load_workspace(self, identifier: str) -> ClassWorkspace:
        lookup = identifier.strip()
        if not lookup:
            raise WorkspaceNotFound("class identifier is empty")
        lower_lookup = lookup.lower()
        slug_lookup = _slugify(lookup)

        for key, record in self._config.items():
            if key.lower() == lower_lookup or _slugify(key) == slug_lookup:
                return self._build_workspace(key, record)

        raise WorkspaceNotFound(f"Static workspace not found for {identifier!r}")

    def _build_workspace(self, class_name: str, record: Mapping[str, Any]) -> ClassWorkspace:
        subject = str(record.get("subject") or record.get("textbook") or class_name).strip()
        slug = record.get("slug") or _slugify(class_name)
        class_id = record.get("id") or f"local-{slug}"
        raw_doc_sets: Iterable[Any] = record.get("doc_sets") or []

        materials: List[WorkspaceMaterial] = []
        for idx, raw_path in enumerate(raw_doc_sets):
            path_str = str(raw_path)
            index_path = self._resolve_index_path(slug, path_str)
            material = WorkspaceMaterial(
                id=f"{class_id}-material-{idx}",
                kind=str(record.get("kind") or "textbook"),
                title=str(record.get("title") or subject),
                index_path=index_path,
                priority=int(record.get("priority", 0) or 0),
            )
            materials.append(material)

        if not materials:
            raise WorkspaceConfigError(f"No doc_sets configured for {class_name!r}")

        weights_raw = _ensure_mapping(record.get("weights"))
        weight_overrides = _normalize_weight_map(weights_raw)

        return ClassWorkspace(
            class_id=str(class_id),
            class_name=class_name,
            slug=slug,
            subject_name=subject,
            materials=materials,
            weight_overrides=weight_overrides,
            metadata=dict(record.get("metadata") or {}),
        )

    def _resolve_index_path(self, slug: str, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (self._index_root / slug / candidate).resolve()
        else:
            candidate = candidate.resolve()
        return candidate


# ---------------------------------------------------------------------------
# Weight normalization
# ---------------------------------------------------------------------------


def _normalize_weight_map(values: Mapping[str, Any]) -> Dict[str, float]:
    overrides: Dict[str, float] = {}
    for key, value in values.items():
        norm_key = str(key).strip().lower()
        if norm_key not in WEIGHT_KINDS:
            continue
        coerced = _coerce_float(value)
        if coerced is None:
            continue
        overrides[norm_key] = clamp_weight(coerced, minimum=WEIGHT_MIN, maximum=WEIGHT_MAX)
    return overrides


# ---------------------------------------------------------------------------
# Workspace manager facade
# ---------------------------------------------------------------------------


class WorkspaceManager:
    """High-level facade that caches resolved workspaces."""

    def __init__(
        self,
        primary: WorkspaceRepository,
        *,
        fallback: Optional[WorkspaceRepository] = None,
        cache_ttl_seconds: Optional[int] = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._cache: Dict[str, Tuple[ClassWorkspace, float]] = {}
        self._ttl = int(cache_ttl_seconds or os.getenv("CLASS_WORKSPACE_CACHE_TTL", "300"))

    def get(self, identifier: str) -> ClassWorkspace:
        key = identifier.strip().lower()
        cached = self._cache.get(key)
        if cached and not self._is_expired(cached[1]):
            return cached[0]

        try:
            workspace = self._primary.load_workspace(identifier)
        except WorkspaceNotFound:
            workspace = self._load_from_fallback(identifier)
        except WorkspaceError:
            workspace = self._load_from_fallback(identifier, propagate=True)

        self._store_cache_aliases(workspace)
        return workspace

    def _is_expired(self, cached_at: float) -> bool:
        if self._ttl <= 0:
            return False
        return (time.time() - cached_at) > self._ttl

    def _store_cache_aliases(self, workspace: ClassWorkspace) -> None:
        timestamp = time.time()
        aliases = {
            workspace.class_name.lower(),
            workspace.slug.lower(),
            workspace.class_id.lower(),
        }
        for alias in aliases:
            self._cache[alias] = (workspace, timestamp)

    def _load_from_fallback(self, identifier: str, propagate: bool = False) -> ClassWorkspace:
        if self._fallback is None:
            raise WorkspaceNotFound(f"No workspace found for {identifier!r}")
        try:
            workspace = self._fallback.load_workspace(identifier)
        except WorkspaceError:
            if propagate:
                raise
            raise WorkspaceNotFound(f"No workspace found for {identifier!r}")
        return workspace


def build_workspace_manager(static_config: Optional[Mapping[str, Mapping[str, Any]]] = None) -> WorkspaceManager:
    """Factory that builds a workspace manager using environment settings.

    Returns a manager backed by the aita_search_spaces table
    (DBWorkspaceRepository) with an optional static config fallback.
    """
    from .db import DBWorkspaceRepository

    fallback_repo: Optional[WorkspaceRepository] = None
    if static_config:
        fallback_repo = StaticWorkspaceRepository(static_config)

    db_repo = DBWorkspaceRepository()
    return WorkspaceManager(db_repo, fallback=fallback_repo)


__all__ = [
    "WorkspaceMaterial",
    "ClassWorkspace",
    "WorkspaceError",
    "WorkspaceNotFound",
    "WorkspaceConfigError",
    "WorkspaceManager",
    "build_workspace_manager",
]

