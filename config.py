from __future__ import annotations

"""Lightweight runtime configuration helpers."""

import os
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


__all__ = [
    "set_subject_name",
    "get_subject_name",
    "get_subject_source",
    "get_subject_priority",
    "get_citation_label",
    "get_runtime_dir",
]
