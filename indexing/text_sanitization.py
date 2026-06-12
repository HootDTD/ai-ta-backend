"""Strip NUL bytes from text headed for Postgres.

Postgres TEXT/VARCHAR and JSONB reject ``\\x00`` outright
(``CharacterNotInRepertoireError``), but PDF text layers (PyMuPDF) and OCR
output can contain it. Every string the indexing pipeline persists must pass
through here first — the chokepoints are ``AITAConnectorDocument`` (document
fields + metadata) and ``items_to_chunk_texts`` (chunk content).
"""

from __future__ import annotations

from typing import Any

_NUL = "\x00"


def strip_nul(text: str) -> str:
    """Return ``text`` with every NUL byte removed."""
    return text.replace(_NUL, "")


def sanitize_jsonable(value: Any) -> Any:
    """Return a copy of a JSON-able structure with NUL bytes stripped from all strings.

    Recurses through dicts (keys and values) and lists/tuples; non-string
    scalars pass through unchanged. The input is never mutated.
    """
    if isinstance(value, str):
        return strip_nul(value)
    if isinstance(value, dict):
        return {sanitize_jsonable(key): sanitize_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_jsonable(item) for item in value]
    return value


__all__ = ["sanitize_jsonable", "strip_nul"]
