"""Deterministic problem-to-solution label matching for authored sets (WU-AAS).

Maps a scraped problem's printed label to the corresponding labelled block in
the paired solution doc. Ambiguity returns ``None`` so callers fall through to
doc-scoped retrieval instead of guessing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = [
    "SolutionChunk",
    "normalize_label",
    "extract_problem_label",
    "build_solution_label_index",
    "match_solution_label",
]

SolutionChunk = tuple[int, str, int | None]

_LABEL_RE = re.compile(
    r"\b(?:problem|prob|question|ques|q|exercise|ex|solution|sol|ans(?:wer)?)"
    r"\s*\.?\s*(\d{1,3})\s*(\([a-z]\)|[a-z]\b)?",
    re.IGNORECASE,
)
_LEADING_NUM_RE = re.compile(r"^\s*(\d{1,3})\s*(\([a-z]\)|[a-z])?\s*[.)]")


def normalize_label(raw: str | None) -> str | None:
    """Return a canonical label key like ``3`` or ``4a``."""
    if not raw:
        return None
    m = _LABEL_RE.search(raw) or _LEADING_NUM_RE.match(raw)
    if not m:
        return None
    num = m.group(1)
    sub = (m.group(2) or "").strip("()").lower()
    return f"{num}{sub}" if sub else num


def extract_problem_label(candidate) -> str | None:
    """Prefer the scraped label, then fall back to a leading problem-text label."""
    scraped = normalize_label(getattr(candidate, "label", None))
    if scraped:
        return scraped
    return normalize_label(getattr(candidate, "problem_text", "") or "")


def build_solution_label_index(
    chunks: Sequence[SolutionChunk],
) -> dict[str, list[SolutionChunk]]:
    """Map normalized labels to the distinct solution chunks that carry them."""
    index: dict[str, list[SolutionChunk]] = {}
    for chunk in chunks:
        _chunk_id, content, _page = chunk
        seen_here: set[str] = set()
        for match in _LABEL_RE.finditer(content or ""):
            key = normalize_label(match.group(0))
            if key and key not in seen_here:
                seen_here.add(key)
                index.setdefault(key, []).append(chunk)
    return index


def match_solution_label(
    label: str | None, index: dict[str, list[SolutionChunk]]
) -> list[SolutionChunk] | None:
    """Return hits only when exactly one distinct chunk carries ``label``."""
    key = label or None
    if not key:
        return None
    key = normalize_label(key) or key
    hits = index.get(key)
    if not hits:
        return None
    if len({chunk[0] for chunk in hits}) != 1:
        return None
    return hits
