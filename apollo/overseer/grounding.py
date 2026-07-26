"""Course-grounded evidence for the Apollo grading path (INTERACTION2).

Turns the session's cached retrieval bundle (`TutoringSession.grounding_bundle`,
written by the INTERACTION1 session-init path) into ONE capped, citation-marked
evidence block that the transcript adjudicator and the topic narrator can read.

Pure module: no IO, no LLM call, no DB. Every entry point is total — a missing,
malformed, or partially-corrupt bundle yields ``None`` rather than raising, so
grounding can never introduce a hard failure ahead of the
``CoverageGradingError`` -> 503 invariant that owns the grading path.

Two rails this module enforces, both deliberately explicit rather than
incidental:

* **Budget.** The block is capped at :data:`EVIDENCE_TOKEN_CAP` tokens and is
  trimmed by DROPPING whole trailing snippets. The transcript is never touched:
  the evidence is the only thing in the adjudication prompt this module can
  shrink, so evidence is always the first (and only) thing truncated.
* **Leakage.** Solution-bearing snippets are dropped here even though
  session-init already filters them before persisting. The read side owns its
  own gate so a bundle written by an older/looser builder — or a phase-2
  grader-only augmentation — can never reach a prompt whose output is served to
  the student.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_LOG = logging.getLogger(__name__)

# ~2k tokens, the upper end of the brief's 1.5-2k window. Characters, not
# tokens, are the unit actually enforced: this module must stay pure (no
# tokenizer import on the grading path), and a 4-chars-per-token estimate is
# the same approximation `retrieval/context_packer.py` budgets with.
EVIDENCE_TOKEN_CAP = 2000
_CHARS_PER_TOKEN = 4

# The maximum number of snippets rendered even when they all fit the budget —
# a runaway bundle should not swamp the rubric items with course prose.
MAX_EVIDENCE_SNIPPETS = 8

_TRUNCATION_MARKER = " …[truncated]"

# Mirrors the session-init writer's own solution filter. Kept as an independent
# copy on purpose: this is the read-side leakage gate, and it must hold even if
# the write side changes or a bundle predates it.
_SOLUTION_DOC_KINDS = frozenset(
    {
        "answer_key",
        "authored_solution",
        "solution",
        "solution_manual",
        "solutions",
    }
)
_SOLUTION_METADATA_KEYS = (
    "doc_kind",
    "document_kind",
    "document_role",
    "kind",
    "material_kind",
)


@dataclass(frozen=True)
class CourseEvidence:
    """One rendered evidence block plus the provenance it is accountable for."""

    block: str
    snippet_count: int
    doc_ids: tuple[str, ...]
    truncated: bool


def is_solution_bearing(snippet: Mapping[str, Any]) -> bool:
    """True when a bundle snippet looks like it carries worked-solution text."""
    metadata = snippet.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    if str(metadata.get("authored_role") or "").strip().casefold() == "solution":
        return True
    return any(
        str(metadata.get(key) or "").strip().casefold() in _SOLUTION_DOC_KINDS
        for key in _SOLUTION_METADATA_KEYS
    )


def extract_snippets(bundle: Any) -> list[dict[str, Any]]:
    """Student-safe snippet dicts from a persisted grounding bundle.

    Tolerates every shape the column can legitimately hold: ``None`` (no bundle
    was built), a non-dict (hand-edited/corrupt row), a dict with no or a
    non-list ``snippets`` key, and individual entries that are not dicts or
    carry no usable ``text``. Anything unusable is skipped, never raised on.
    """
    if not isinstance(bundle, Mapping):
        return []
    raw = bundle.get("snippets")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []

    snippets: list[dict[str, Any]] = []
    dropped_solution = 0
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if is_solution_bearing(item):
            dropped_solution += 1
            continue
        snippets.append(dict(item))

    if dropped_solution:
        _LOG.warning(
            "apollo_grounding_solution_snippets_dropped count=%d",
            dropped_solution,
        )
    return snippets


def _citation_marker(snippet: Mapping[str, Any]) -> str:
    marker = snippet.get("citation_marker")
    if isinstance(marker, str) and marker.strip():
        return marker.strip()
    # The packer always emits a marker; a bundle missing one still needs a
    # stable label so the narrator has something legitimate to cite.
    doc_short = snippet.get("doc_short") or snippet.get("doc_title")
    label = str(doc_short).strip() if isinstance(doc_short, str) and doc_short.strip() else "Course"
    return f"[{label}]"


def _doc_id(snippet: Mapping[str, Any]) -> str | None:
    """The most stable document identifier this snippet carries."""
    metadata = snippet.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("document_id", "doc_id", "file_id"):
            value = metadata.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    for key in ("source_path", "doc_title", "doc_short"):
        value = snippet.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _render_entry(snippet: Mapping[str, Any]) -> str:
    marker = _citation_marker(snippet)
    section = snippet.get("section_path")
    header = marker
    if isinstance(section, str) and section.strip():
        header = f"{marker} — {section.strip()}"
    text = str(snippet["text"]).strip()
    return f"{header}\n{text}"


def build_course_evidence(
    bundle: Any,
    *,
    token_cap: int = EVIDENCE_TOKEN_CAP,
    max_snippets: int = MAX_EVIDENCE_SNIPPETS,
) -> CourseEvidence | None:
    """Render a session's grounding bundle into a capped evidence block.

    Returns ``None`` — never an empty block — when there is nothing usable to
    show, so callers can pass ``course_evidence=None`` and get prompts that are
    byte-identical to the pre-feature build.

    Trimming is snippet-granular and ordered: entries are appended in bundle
    (retrieval-rank) order until the next one would exceed ``token_cap``, then
    the rest are dropped. The single exception is a first entry that alone
    exceeds the cap — its text is hard-truncated so a bundle of one huge chunk
    still contributes something rather than silently vanishing.
    """
    snippets = extract_snippets(bundle)
    if not snippets:
        return None

    char_cap = max(0, int(token_cap)) * _CHARS_PER_TOKEN
    entries: list[str] = []
    doc_ids: list[str] = []
    used = 0
    truncated = False

    for snippet in snippets[: max(0, int(max_snippets))]:
        entry = _render_entry(snippet)
        # +2 for the blank line joining this entry to the previous one.
        cost = len(entry) + (2 if entries else 0)
        if used + cost > char_cap:
            if entries:
                truncated = True
                break
            keep = char_cap - len(_TRUNCATION_MARKER)
            if keep <= 0:
                return None
            entry = entry[:keep] + _TRUNCATION_MARKER
            cost = len(entry)
            truncated = True
        entries.append(entry)
        used += cost
        doc_id = _doc_id(snippet)
        if doc_id is not None and doc_id not in doc_ids:
            doc_ids.append(doc_id)
        if truncated:
            break

    if len(snippets) > len(entries):
        truncated = True
    if not entries:
        return None

    return CourseEvidence(
        block="\n\n".join(entries),
        snippet_count=len(entries),
        doc_ids=tuple(doc_ids),
        truncated=truncated,
    )


def evidence_block(evidence: CourseEvidence | None) -> str | None:
    """The prompt-ready block, or ``None`` for the ungrounded prompt build."""
    return evidence.block if evidence is not None else None


def grounding_provenance(evidence: CourseEvidence | None) -> dict[str, Any]:
    """The additive ``grading_provenance["grounding"]` sub-object.

    Always present once INTERACTION2 ships (``used: false`` when the flag is
    off, the bundle is NULL, or nothing survived the filters) so the replay eval
    can diff grounded against ungrounded runs on the artifact alone.
    """
    if evidence is None:
        return {"used": False, "snippet_count": 0, "doc_ids": []}
    return {
        "used": True,
        "snippet_count": evidence.snippet_count,
        "doc_ids": list(evidence.doc_ids),
    }


__all__ = [
    "EVIDENCE_TOKEN_CAP",
    "MAX_EVIDENCE_SNIPPETS",
    "CourseEvidence",
    "build_course_evidence",
    "evidence_block",
    "extract_snippets",
    "grounding_provenance",
    "is_solution_bearing",
]
