from __future__ import annotations

"""Lightweight runtime configuration helpers."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


_WIRE = os.getenv("RETRIEVAL_WIRE_LOG", "off").lower() not in {"0", "off", "false", "no"}
_PRIORITY = {"default": 0, "meta": 1, "env": 2, "cli": 3}

_SUBJECT_NAME: Optional[str] = None
_SUBJECT_SOURCE: str = "default"
_SUBJECT_PRIORITY: int = -1
_SUBJECT_LOGGED = False
_CITATION_LABEL: Optional[str] = None
_RUNTIME_DIR: Optional[Path] = None


def _sanitize_subject(name: str | None) -> str:
    if not isinstance(name, str):
        return ""
    cleaned = " ".join(name.strip().split())
    if not cleaned:
        return ""
    if len(cleaned) > 50:
        cleaned = cleaned[:50].rstrip()
    return cleaned


def _log_subject() -> None:
    global _SUBJECT_LOGGED
    if not _WIRE or _SUBJECT_NAME is None or _SUBJECT_LOGGED:
        return
    source = _SUBJECT_SOURCE.upper() if _SUBJECT_SOURCE else "DEFAULT"
    print(f'[Config] subject="{_SUBJECT_NAME}" (source={source})', flush=True)
    _SUBJECT_LOGGED = True


def _apply_default() -> None:
    global _SUBJECT_NAME, _SUBJECT_SOURCE, _SUBJECT_PRIORITY
    if _SUBJECT_NAME is None:
        _SUBJECT_NAME = "course/textbook"
        _SUBJECT_SOURCE = "default"
        _SUBJECT_PRIORITY = _PRIORITY["default"]
        _log_subject()


def set_subject_name(name: str | None, source: str) -> None:
    """Set the active subject name honoring precedence."""

    global _SUBJECT_NAME, _SUBJECT_SOURCE, _SUBJECT_PRIORITY, _SUBJECT_LOGGED

    src_norm = (source or "default").lower()
    priority = _PRIORITY.get(src_norm, 0)
    cleaned = _sanitize_subject(name)

    if not cleaned:
        if src_norm == "default":
            _apply_default()
        return

    if priority < _SUBJECT_PRIORITY:
        return

    if priority == _SUBJECT_PRIORITY and _SUBJECT_NAME == cleaned:
        return

    _SUBJECT_NAME = cleaned
    _SUBJECT_SOURCE = src_norm
    _SUBJECT_PRIORITY = priority
    _SUBJECT_LOGGED = False
    _log_subject()


def get_subject_name() -> str:
    """Return the active subject, applying environment/default fallbacks."""

    global _SUBJECT_NAME, _SUBJECT_PRIORITY

    if _SUBJECT_NAME is None:
        env_val = os.getenv("TEXTBOOK_SUBJECT")
        if env_val:
            set_subject_name(env_val, "env")
        else:
            _apply_default()
    return _SUBJECT_NAME or "course/textbook"


def get_subject_source() -> str:
    get_subject_name()
    return _SUBJECT_SOURCE


def get_subject_priority() -> int:
    get_subject_name()
    return _SUBJECT_PRIORITY


def get_citation_label() -> str:
    """Return the configured citation label, defaulting to ``"Textbook"``."""

    global _CITATION_LABEL

    if _CITATION_LABEL is not None:
        return _CITATION_LABEL

    raw = os.getenv("CITATION_LABEL", "Textbook")
    if isinstance(raw, str):
        cleaned = " ".join(raw.strip().split())
    else:  # pragma: no cover - defensive, env vars are strings
        cleaned = ""

    if not cleaned:
        cleaned = "Textbook"

    _CITATION_LABEL = cleaned
    return _CITATION_LABEL


def get_runtime_dir() -> Path:
    """Return the runtime directory, defaulting to repo-root ./runtime."""

    global _RUNTIME_DIR

    if _RUNTIME_DIR is not None:
        return _RUNTIME_DIR

    raw = os.getenv("RUNTIME_DIR", "runtime")
    base = Path(__file__).resolve().parents[1]
    path = Path(raw)
    if not path.is_absolute():
        path = (base / path).resolve()
    _RUNTIME_DIR = path
    return _RUNTIME_DIR


_REQ_PRIORITY = {"default": 0, "meta": 1, "env": 2, "cli": 3, "server": 3}


@dataclass
class RequestConfig:
    """Per-request configuration. Thread-safe replacement for module globals.

    The HTTP server creates one per ``/ask`` request so concurrent requests
    never share subject state.  CLI callers can use ``from_env()`` or leave
    the existing module-level helpers in place.
    """

    subject_name: str = "course/textbook"
    subject_source: str = "default"
    subject_priority: int = -1
    citation_label: str = "Textbook"
    runtime_dir: Optional[Path] = None

    @classmethod
    def from_env(cls) -> "RequestConfig":
        """Create a config seeded from environment variables."""
        cfg = cls()

        env_subject = os.getenv("TEXTBOOK_SUBJECT")
        if env_subject:
            cfg.set_subject(env_subject, "env")

        raw_label = os.getenv("CITATION_LABEL", "Textbook")
        if isinstance(raw_label, str):
            cleaned = " ".join(raw_label.strip().split())
        else:
            cleaned = ""
        cfg.citation_label = cleaned or "Textbook"

        raw_dir = os.getenv("RUNTIME_DIR", "runtime")
        base = Path(__file__).resolve().parents[1]
        path = Path(raw_dir)
        if not path.is_absolute():
            path = (base / path).resolve()
        cfg.runtime_dir = path
        return cfg

    def set_subject(self, name: str | None, source: str) -> None:
        """Set subject name honoring precedence (mirrors ``set_subject_name``)."""
        src_norm = (source or "default").lower()
        priority = _REQ_PRIORITY.get(src_norm, 0)
        cleaned = _sanitize_subject(name)
        if not cleaned:
            return
        if priority < self.subject_priority:
            return
        if priority == self.subject_priority and self.subject_name == cleaned:
            return
        self.subject_name = cleaned
        self.subject_source = src_norm
        self.subject_priority = priority


# ---------------------------------------------------------------------------
# pgvector / SurfSense integration settings
# ---------------------------------------------------------------------------

def use_pgvector_retrieval() -> bool:
    """Return True when the new pgvector retrieval path is enabled."""
    return os.getenv("USE_PGVECTOR_RETRIEVAL", "false").lower() not in {
        "0", "false", "off", "no"
    }


def get_embedding_dim() -> int:
    """Vector dimension for embeddings (must match the model used at index time)."""
    return int(os.getenv("EMBEDDING_DIM", "3072"))


def get_embedding_model() -> str:
    """OpenAI embedding model name."""
    return os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")


def get_supabase_db_url() -> str:
    """Async PostgreSQL connection string (asyncpg) for SQLAlchemy."""
    return os.getenv("SUPABASE_DB_URL", "")


# ---------------------------------------------------------------------------
# Neo4j (ApolloV3 KG layer)
# ---------------------------------------------------------------------------

def get_neo4j_uri() -> str:
    return os.getenv("NEO4J_URI", "")


def get_neo4j_username() -> str:
    return os.getenv("NEO4J_USERNAME", "")


def get_neo4j_password() -> str:
    return os.getenv("NEO4J_PASSWORD", "")


def get_neo4j_database() -> str:
    return os.getenv("NEO4J_DATABASE", "")


def neo4j_configured() -> bool:
    """True when all four NEO4J_* vars are present (used by health checks/tests)."""
    return all([
        get_neo4j_uri(),
        get_neo4j_username(),
        get_neo4j_password(),
        get_neo4j_database(),
    ])


# ---------------------------------------------------------------------------
# Apollo NLI resolution flags
# ---------------------------------------------------------------------------


def apollo_nli_misc_positive_certify() -> bool:
    """DEFAULT-OFF flag ``APOLLO_NLI_MISC_POSITIVE_CERTIFY``.

    When ON, the NLI resolution tier POSITIVELY resolves a student utterance to
    the ``misc.*`` candidate whose hypothesis it entails at/above the
    misconception-veto threshold (instead of only vetoing reference credit and
    returning no match). Reference credit stays blocked in that case regardless
    of the flag — this only adds the positive resolution. Unset or any
    non-truthy value means OFF (byte-identical veto-only behavior); truthy is
    ``1``/``true``/``yes`` (same acceptance set as ``APOLLO_NLI_ENABLED``).
    Flipping this ON is a grading-behavior change — a human decision."""
    raw = os.getenv("APOLLO_NLI_MISC_POSITIVE_CERTIFY")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes")


def apollo_abstention_composite_enabled() -> bool:
    """DEFAULT-OFF flag ``APOLLO_ABSTENTION_COMPOSITE`` (spec §10).

    When ON, the graph-grader abstention gate is replaced by the CONTENT-based
    composite signal: grade iff the resolver credited >=
    :func:`apollo_composite_coverage_min` of the problem's expected reference
    set (``GradeResult.node_coverage_score`` — already reference-denominated),
    regardless of ``unresolved_rate``/``normalization_confidence`` (the volume
    signals the §10 decision memo proved cannot separate strong attempts from
    misconception controls). Detected contradiction findings are recorded in
    the artifact's ``abstention.composite`` block for audit but do NOT force
    abstention on their own — a detected misconception is informative
    feedback, not grading uncertainty.

    Unset or any non-truthy value means OFF (byte-identical to the existing
    ``unresolved_rate``/``normalization_confidence`` gates); truthy is
    ``1``/``true``/``yes`` (same acceptance set as the other ``APOLLO_*``
    flags). Flipping this ON is a grading-behavior change — a human decision."""
    raw = os.getenv("APOLLO_ABSTENTION_COMPOSITE")
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true", "yes")


def apollo_composite_coverage_min() -> float:
    """The §10 composite gate's coverage threshold (``APOLLO_COMPOSITE_COVERAGE_MIN``).

    Grade iff ``node_coverage_score >= this value`` (or >=1 contradiction
    finding was detected) when :func:`apollo_abstention_composite_enabled` is
    True. Default ``0.1`` — the 2026-07-07 F1c-corpus calibration: every
    correct-persona attempt's resolver-only coverage sat at >= 0.20 and one
    resolved node on the longest declared path (7 nodes) is ~0.14, so 0.1
    reads "the resolver credited essentially nothing" (the memo's a-priori 0.6
    abstained 19/31 gradeable attempts). MUST stay equal to
    ``apollo.grading.abstention.COMPOSITE_DEFAULT_COVERAGE_MIN`` (this module
    cannot import apollo — a test pins the equality). Malformed/missing values
    fall back to the default."""
    raw = os.getenv("APOLLO_COMPOSITE_COVERAGE_MIN")
    if raw is None:
        return 0.1
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.1


def rerankers_enabled() -> bool:
    """Return True when the optional reranking step is active."""
    return os.getenv("RERANKERS_ENABLED", "false").lower() not in {
        "0", "false", "off", "no"
    }


def get_reranker_model() -> str:
    return os.getenv("RERANKER_MODEL", "cross-encoder")


__all__ = [
    "set_subject_name",
    "get_subject_name",
    "get_subject_source",
    "get_subject_priority",
    "get_citation_label",
    "get_runtime_dir",
    "RequestConfig",
    # pgvector settings
    "use_pgvector_retrieval",
    "get_embedding_dim",
    "get_embedding_model",
    "get_supabase_db_url",
    "rerankers_enabled",
    "get_reranker_model",
    # Apollo NLI resolution flags
    "apollo_nli_misc_positive_certify",
    # Apollo abstention composite gate (spec §10)
    "apollo_abstention_composite_enabled",
    "apollo_composite_coverage_min",
    # Neo4j (ApolloV3)
    "get_neo4j_uri",
    "get_neo4j_username",
    "get_neo4j_password",
    "get_neo4j_database",
    "neo4j_configured",
]
