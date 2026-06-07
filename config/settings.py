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
    # Neo4j (ApolloV3)
    "get_neo4j_uri",
    "get_neo4j_username",
    "get_neo4j_password",
    "get_neo4j_database",
    "neo4j_configured",
]

# ---------------------------------------------------------------------------
# Textbook problem-index ingest (apollo/textbook_ingest).
# Module-level constants, matching this file's existing globals style
# (settings.py is not a pydantic BaseSettings). Tune from data; see
# docs/superpowers/specs/2026-06-02-apollo-textbook-problem-index-design.md §12.
# ---------------------------------------------------------------------------
# Accessed via settings.TEXTBOOK_* attribute access; deliberately not added to __all__.
TEXTBOOK_EMBEDDING_MODEL = get_embedding_model()
TEXTBOOK_EMBEDDING_DIM = get_embedding_dim()
TEXTBOOK_DEDUP_EMBEDDING_CUTOFF = 0.85          # cosine >= this -> matched_existing
TEXTBOOK_DEDUP_LLM_JUDGE_LOW = 0.75             # [LOW, HIGH) band triggers llm-judge
TEXTBOOK_DEDUP_LLM_JUDGE_HIGH = 0.85
TEXTBOOK_PROBLEM_DETECTOR_ACCEPT_THRESHOLD = 0.60   # detector confidence floor
TEXTBOOK_CLASSIFIER_ACCEPT_THRESHOLD = 0.60         # extraction concept-tag floor
TEXTBOOK_LLM_MAX_RETRIES = 2                     # per-call retries (timeout/malformed)
TEXTBOOK_TIER2_MAX_REJECT_RATE = 0.50           # synthetic smoke reject ceiling
TEXTBOOK_TIER3_MAX_REJECT_RATE = 0.40           # real-textbook release reject ceiling
