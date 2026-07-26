"""Citation-only course-material pointers for weak Apollo grade topics."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apollo.overseer.topic_score import TopicCredit, TopicScoreResult
from citations.formatter import DOC_TYPE_LABELS
from config.contracts import BundleSnippet
from config.settings import get_citation_label
from retrieval import retrieve_for_question

MAX_WEAK_TOPICS = 3
POINTERS_PER_TOPIC = 3
REMEDIATION_TOKEN_BUDGET = 800

_SOLUTION_DOC_KINDS = frozenset(
    {
        "answer_key",
        "authored_solution",
        "solution",
        "solution_manual",
        "solutions",
    }
)


def select_weak_topics(
    topic_score: TopicScoreResult,
    *,
    cap: int = MAX_WEAK_TOPICS,
) -> tuple[TopicCredit, ...]:
    """Reuse the scorecard's bands: ``partial``/``missing`` are weak."""
    return tuple(topic for topic in topic_score.topics if topic.status in {"partial", "missing"})[
        :cap
    ]


def _as_mapping(snippet: BundleSnippet | dict[str, Any]) -> dict[str, Any]:
    if isinstance(snippet, dict):
        return snippet
    if is_dataclass(snippet):
        return asdict(snippet)
    raise TypeError("remediation snippets must be BundleSnippet values or dictionaries")


def _is_solution_bearing(snippet: BundleSnippet | dict[str, Any]) -> bool:
    metadata = _as_mapping(snippet).get("metadata") or {}
    if not isinstance(metadata, dict):
        return False

    authored_role = str(metadata.get("authored_role") or "").strip().casefold()
    if authored_role == "solution":
        return True
    return any(
        str(metadata.get(key) or "").strip().casefold() in _SOLUTION_DOC_KINDS
        for key in ("doc_kind", "document_kind", "document_role", "kind", "material_kind")
    )


def _pointer(snippet: BundleSnippet | dict[str, Any]) -> dict[str, Any]:
    item = _as_mapping(snippet)
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    page = item.get("page")
    page = page if isinstance(page, int) and not isinstance(page, bool) and page > 0 else None

    doc_id = metadata.get("document_id") or metadata.get("doc_id") or item.get("id")
    if doc_id in (None, ""):
        raise ValueError("remediation snippet has no document identifier")

    label = str(item.get("citation_marker") or "").strip()
    if not label:
        kind = (
            str(metadata.get("kind") or metadata.get("material_kind") or "textbook")
            .strip()
            .casefold()
        )
        doc_label = DOC_TYPE_LABELS.get(kind, get_citation_label())
        label = f"[{doc_label}, p. {page}]" if page is not None else f"[{doc_label}]"

    return {"doc_id": doc_id, "label": label, "page": page}


def _citation_pointers(
    snippets: list[BundleSnippet | dict[str, Any]],
) -> list[dict[str, Any]]:
    pointers: list[dict[str, Any]] = []
    seen: set[tuple[Any, str, int | None]] = set()
    for snippet in snippets:
        if _is_solution_bearing(snippet):
            continue
        pointer = _pointer(snippet)
        key = (pointer["doc_id"], pointer["label"], pointer["page"])
        if key in seen:
            continue
        seen.add(key)
        pointers.append(pointer)
        if len(pointers) == POINTERS_PER_TOPIC:
            break
    return pointers


def _feedback_items_by_key(feedback: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = feedback.get("topic_feedback")
    if not isinstance(items, list):
        raise ValueError("structured feedback has no topic_feedback list")

    by_key: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("canonical_key"), str):
            raise ValueError("structured feedback contains an invalid topic item")
        by_key[item["canonical_key"]] = item
    return by_key


async def add_remediation_reviews(
    *,
    db: AsyncSession,
    search_space_id: int,
    topic_score: TopicScoreResult,
    feedback: dict[str, Any],
    grounding_bundle: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a fully decorated copy, or ``None`` when no complete pass exists.

    A non-null bundle is the only snippet source for that pass. Fresh per-topic
    retrieval is used only when the bundle is null. The caller owns the single
    failure-domain catch so exceptions can never escape into grading.
    """
    weak_topics = select_weak_topics(topic_score)
    if not weak_topics:
        return None

    decorated = deepcopy(feedback)
    feedback_by_key = _feedback_items_by_key(decorated)
    reviews: dict[str, list[dict[str, Any]]] = {}

    if grounding_bundle is not None:
        raw_snippets = grounding_bundle.get("snippets")
        if not isinstance(raw_snippets, list):
            raise ValueError("grounding bundle has no snippets list")
        pointers = _citation_pointers(raw_snippets)
        if not pointers:
            return None
        for topic in weak_topics:
            reviews[topic.canonical_key] = deepcopy(pointers)
    else:
        async with db.begin_nested():
            for topic in weak_topics:
                item = feedback_by_key.get(topic.canonical_key)
                if item is None:
                    raise ValueError("weak topic has no structured feedback item")
                label = topic.display_name or topic.canonical_key.replace("_", " ").replace(
                    ".", " "
                )
                snippets, _ = await retrieve_for_question(
                    query=f"{label}. {item['note']}",
                    keywords=[label],
                    search_space_id=search_space_id,
                    db_session=db,
                    top_k=POINTERS_PER_TOPIC,
                    token_budget=REMEDIATION_TOKEN_BUDGET,
                    citation_label=get_citation_label(),
                )
                pointers = _citation_pointers(snippets)
                if not pointers:
                    return None
                reviews[topic.canonical_key] = pointers

    for canonical_key, pointers in reviews.items():
        item = feedback_by_key.get(canonical_key)
        if item is None:
            raise ValueError("weak topic has no structured feedback item")
        item["review"] = pointers
    return decorated


__all__ = [
    "MAX_WEAK_TOPICS",
    "add_remediation_reviews",
    "select_weak_topics",
]
