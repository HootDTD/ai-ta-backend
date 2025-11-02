from __future__ import annotations

"""Class workspace resolution with Supabase-backed isolation.

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

import requests

from .store_weights import WEIGHT_KINDS, WEIGHT_MIN, WEIGHT_MAX, clamp_weight

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


class SupabaseWorkspaceRepository(WorkspaceRepository):
    """Repository fetching workspace definitions from Supabase REST."""

    def __init__(
        self,
        *,
        supabase_url: str,
        service_key: str,
        table: Optional[str] = None,
        select: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        base = supabase_url.rstrip("/")
        if not base:
            raise WorkspaceConfigError("Supabase URL cannot be empty")
        self._base_url = base
        self._service_key = service_key.strip()
        if not self._service_key:
            raise WorkspaceConfigError("Supabase service role key cannot be empty")
        self._table = (table or os.getenv("SUPABASE_CLASS_WORKSPACES_TABLE") or "class_workspaces").strip()
        self._select = (select or os.getenv("SUPABASE_CLASS_WORKSPACES_SELECT") or
                        "id,slug,name,subject,materials,weights,metadata").strip()
        self._timeout = float(timeout or os.getenv("SUPABASE_HTTP_TIMEOUT", "20"))
        self._session = requests.Session()
        self._headers = {
            "apikey": self._service_key,
            "Authorization": f"Bearer {self._service_key}",
            "Accept": "application/json",
        }
        self._index_root = _default_index_root()

    # ------------------------------ HTTP helpers ------------------------------

    def _request(self, params: Dict[str, str]) -> List[Dict[str, Any]]:
        url = f"{self._base_url}/rest/v1/{self._table}"
        response = self._session.get(url, headers=self._headers, params=params, timeout=self._timeout)
        if response.status_code == 404:
            return []
        if response.status_code == 401:
            raise WorkspaceConfigError("Supabase workspace request unauthorized; check service role key.")
        if response.status_code == 403:
            raise WorkspaceConfigError("Supabase workspace request forbidden; verify Row Level Security policies.")
        if not response.ok:
            detail = response.text.strip() or f"status={response.status_code}"
            raise WorkspaceError(f"Supabase workspace fetch failed: {detail}")
        try:
            payload = response.json()
        except Exception as exc:  # pragma: no cover - defensive
            raise WorkspaceError(f"Supabase returned non-JSON payload: {exc}") from exc
        if not isinstance(payload, list):
            raise WorkspaceError("Supabase workspace response must be a JSON array.")
        return payload

    def _query_variants(self, identifier: str) -> List[Dict[str, str]]:
        ident = identifier.strip()
        variants: List[Dict[str, str]] = []
        if not ident:
            return variants
        if _is_uuid(ident):
            variants.append({"id": f"eq.{ident}"})
        slug = _slugify(ident)
        variants.append({"slug": f"eq.{slug}"})
        variants.append({"name": f"eq.{ident}"})
        # Allow looking up legacy subject field for compatibility.
        variants.append({"subject": f"eq.{ident}"})
        return variants

    # ----------------------------- Repo interface -----------------------------

    def load_workspace(self, identifier: str) -> ClassWorkspace:
        queries = self._query_variants(identifier)
        if not queries:
            raise WorkspaceNotFound("class identifier is empty")

        errors: List[str] = []
        for params in queries:
            params = dict(params)
            params["limit"] = "1"
            params["select"] = self._select
            try:
                rows = self._request(params)
            except WorkspaceError as exc:
                errors.append(str(exc))
                continue
            if not rows:
                continue
            row = rows[0]
            try:
                return self._parse_workspace(row)
            except WorkspaceError as exc:
                errors.append(str(exc))
                continue

        if errors:
            joined = "; ".join(errors[-3:])
            raise WorkspaceNotFound(f"No workspace found for {identifier!r}: {joined}")
        raise WorkspaceNotFound(f"No workspace found for {identifier!r}")

    def _parse_workspace(self, row: Mapping[str, Any]) -> ClassWorkspace:
        class_id = str(row.get("id") or "").strip()
        if not class_id:
            raise WorkspaceConfigError("Workspace row missing 'id'")
        name = str(row.get("name") or row.get("title") or row.get("slug") or class_id).strip()
        slug = str(row.get("slug") or _slugify(name)).strip()
        subject = str(row.get("subject") or name).strip()
        metadata = _ensure_mapping(row.get("metadata"))

        materials_raw = _ensure_list(row.get("materials"))
        if not materials_raw:
            raise WorkspaceConfigError(f"Workspace {slug!r} has no materials configured.")

        materials: List[WorkspaceMaterial] = []
        for entry in materials_raw:
            material = self._parse_material(slug, entry)
            materials.append(material)

        weights_raw = _ensure_mapping(row.get("weights"))
        weight_overrides = _normalize_weight_map(weights_raw)

        return ClassWorkspace(
            class_id=class_id,
            class_name=name,
            slug=slug,
            subject_name=subject,
            materials=materials,
            weight_overrides=weight_overrides,
            metadata=metadata,
        )

    def _parse_material(self, slug: str, entry: Any) -> WorkspaceMaterial:
        if not isinstance(entry, Mapping):
            raise WorkspaceConfigError(f"Material entry for {slug!r} must be an object, got {type(entry)}")
        material_id = str(entry.get("id") or uuid.uuid4())
        kind = str(entry.get("kind") or "other").strip().lower()
        title = str(entry.get("title") or kind.title())
        priority = int(entry.get("priority", 0) or 0)

        raw_index = entry.get("index_path") or entry.get("index") or entry.get("path")
        if not raw_index:
            raise WorkspaceConfigError(f"Material {material_id!r} missing index_path")
        index_path = self._resolve_index_path(slug, str(raw_index))

        weight_override = _coerce_float(entry.get("weight_override"))
        if weight_override is not None:
            weight_override = clamp_weight(weight_override, minimum=WEIGHT_MIN, maximum=WEIGHT_MAX)

        metadata = _ensure_mapping(entry.get("metadata"))
        material = WorkspaceMaterial(
            id=material_id,
            kind=kind,
            title=title,
            index_path=index_path,
            priority=priority,
            weight_override=weight_override,
            metadata=metadata,
        )
        return material

    def _resolve_index_path(self, slug: str, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = (self._index_root / slug / candidate).resolve()
        else:
            candidate = candidate.resolve()
        try:
            candidate.relative_to(self._index_root)
        except ValueError as exc:
            raise WorkspaceConfigError(
                f"Material index path {candidate} escapes index root {self._index_root}"
            ) from exc
        if not candidate.exists():
            log.warning("Workspace index path missing on disk: %s", candidate)
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
    """Factory that builds a workspace manager using environment settings."""

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    fallback_repo: Optional[WorkspaceRepository] = None
    if static_config:
        fallback_repo = StaticWorkspaceRepository(static_config)

    if supabase_url and supabase_service_key:
        primary_repo = SupabaseWorkspaceRepository(
            supabase_url=supabase_url,
            service_key=supabase_service_key,
        )
    elif fallback_repo is not None:
        primary_repo = fallback_repo
        fallback_repo = None
        log.warning(
            "SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY not set; using static workspace config only."
        )
    else:
        raise WorkspaceConfigError(
            "Supabase credentials missing and no static workspace fallback provided."
        )

    return WorkspaceManager(primary_repo, fallback=fallback_repo)


__all__ = [
    "WorkspaceMaterial",
    "ClassWorkspace",
    "WorkspaceError",
    "WorkspaceNotFound",
    "WorkspaceConfigError",
    "WorkspaceManager",
    "build_workspace_manager",
]

