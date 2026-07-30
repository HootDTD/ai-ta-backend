from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from apollo.overseer.remediation import (
    _as_mapping,
    _citation_pointers,
    _is_solution_bearing,
    _pointer,
    add_remediation_reviews,
    select_weak_topics,
)
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult
from config.contracts import BundleSnippet

pytestmark = pytest.mark.unit


class _DB:
    @asynccontextmanager
    async def begin_nested(self):
        yield


def _topic(
    key: str,
    status: Literal["covered", "partial", "missing"],
    *,
    display_name: str | None = None,
) -> TopicCredit:
    credits = {"covered": 1.0, "partial": 0.4, "missing": 0.0}
    return TopicCredit(
        canonical_key=key,
        display_name=display_name,
        credit=credits[status],
        status=status,
        weight=0.2,
        misconceptions=(),
    )


def _score(*topics: TopicCredit) -> TopicScoreResult:
    return TopicScoreResult(
        score=40,
        letter="F",
        coverage_component=0.4,
        misconception_dock=0.0,
        topics=topics,
    )


def _feedback(*keys: str) -> dict:
    return {
        "headline": "Keep building.",
        "topic_feedback": [
            {"canonical_key": key, "note": f"Review {key}.", "quote": None} for key in keys
        ],
        "recap": [],
        "next_step": "Try the missing step.",
    }


def _snippet(
    snippet_id: str,
    *,
    document_id: int | None = None,
    metadata: dict | None = None,
) -> BundleSnippet:
    merged_metadata = dict(metadata or {})
    if document_id is not None:
        merged_metadata["document_id"] = document_id
    return BundleSnippet(
        id=snippet_id,
        type="body",
        page=12,
        section_path="3.2",
        text=f"Course material {snippet_id}",
        figure_id=None,
        why="hit",
        source_path="course.pdf",
        doc_title="Course Text",
        doc_short="Course Text",
        citation_marker="[Textbook, p. 12]",
        final_score={"final": 0.9},
        metadata=merged_metadata,
    )


def test_weak_topic_selection_reuses_scorecard_bands_and_caps_at_three():
    topics = (
        _topic("covered", "covered"),
        _topic("partial", "partial"),
        _topic("missing-1", "missing"),
        _topic("missing-2", "missing"),
        _topic("missing-3", "missing"),
    )

    selected = select_weak_topics(_score(*topics))

    assert [topic.canonical_key for topic in selected] == [
        "partial",
        "missing-1",
        "missing-2",
    ]


def test_mapping_and_pointer_edge_cases():
    raw = {
        "id": "slides-chunk",
        "page": False,
        "citation_marker": "",
        "metadata": {"kind": "slides"},
    }

    assert _as_mapping(raw) is raw
    assert _pointer(raw) == {
        "doc_id": "slides-chunk",
        "label": "[Slides]",
        "page": None,
        "upload_id": None,
    }
    assert _is_solution_bearing({"id": "odd", "metadata": "not-a-mapping"}) is False

    with pytest.raises(TypeError, match="BundleSnippet values or dictionaries"):
        _as_mapping(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="no document identifier"):
        _pointer({"id": "", "page": 1, "metadata": {}})


def test_pointer_carries_upload_id_from_chunk_metadata():
    """`teacher_upload_id` (stringified app.uploads.id in chunk metadata)
    surfaces as an int `upload_id` so the UI can link the chip to the stored
    source PDF; junk values degrade to None instead of raising."""
    raw = {
        "id": "chunk-1",
        "page": 4,
        "citation_marker": "[Notes, p. 4]",
        "metadata": {"document_id": 91, "teacher_upload_id": "17"},
    }
    assert _pointer(raw) == {
        "doc_id": 91,
        "label": "[Notes, p. 4]",
        "page": 4,
        "upload_id": 17,
    }

    raw["metadata"]["teacher_upload_id"] = "not-a-number"
    assert _pointer(raw)["upload_id"] is None


def test_pointer_kind_fallback_deduplicates_and_caps_results(monkeypatch):
    monkeypatch.setattr("apollo.overseer.remediation.get_citation_label", lambda: "Course Pack")
    snippets = [
        {"id": "one", "page": 2, "citation_marker": "", "metadata": {"kind": "custom"}},
        {"id": "one", "page": 2, "citation_marker": "", "metadata": {"kind": "custom"}},
        {"id": "two", "page": 3, "citation_marker": "[Notes, p. 3]", "metadata": {}},
        {"id": "three", "page": 4, "citation_marker": "[Notes, p. 4]", "metadata": {}},
        {"id": "four", "page": 5, "citation_marker": "[Notes, p. 5]", "metadata": {}},
    ]

    assert _citation_pointers(snippets) == [
        {"doc_id": "one", "label": "[Course Pack, p. 2]", "page": 2, "upload_id": None},
        {"doc_id": "two", "label": "[Notes, p. 3]", "page": 3, "upload_id": None},
        {"doc_id": "three", "label": "[Notes, p. 4]", "page": 4, "upload_id": None},
    ]


@pytest.mark.asyncio
async def test_no_weak_topics_returns_none():
    assert (
        await add_remediation_reviews(
            db=_DB(),  # type: ignore[arg-type]
            search_space_id=7,
            topic_score=_score(_topic("covered", "covered")),
            feedback=_feedback("covered"),
            grounding_bundle=None,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feedback", "message"),
    [
        ({}, "no topic_feedback list"),
        ({"topic_feedback": ["invalid"]}, "invalid topic item"),
    ],
)
async def test_invalid_feedback_shape_raises(feedback, message):
    with pytest.raises(ValueError, match=message):
        await add_remediation_reviews(
            db=_DB(),  # type: ignore[arg-type]
            search_space_id=7,
            topic_score=_score(_topic("weak", "missing")),
            feedback=feedback,
            grounding_bundle=None,
        )


@pytest.mark.asyncio
async def test_present_bundle_requires_snippets_list():
    with pytest.raises(ValueError, match="grounding bundle has no snippets list"):
        await add_remediation_reviews(
            db=_DB(),  # type: ignore[arg-type]
            search_space_id=7,
            topic_score=_score(_topic("weak", "missing")),
            feedback=_feedback("weak"),
            grounding_bundle={},
        )


@pytest.mark.asyncio
async def test_fresh_retrieval_requires_feedback_for_every_weak_topic():
    with pytest.raises(ValueError, match="weak topic has no structured feedback item"):
        await add_remediation_reviews(
            db=_DB(),  # type: ignore[arg-type]
            search_space_id=7,
            topic_score=_score(_topic("missing-from-feedback", "missing")),
            feedback=_feedback("another-topic"),
            grounding_bundle=None,
        )


@pytest.mark.asyncio
async def test_bundle_decoration_requires_feedback_for_every_weak_topic():
    with pytest.raises(ValueError, match="weak topic has no structured feedback item"):
        await add_remediation_reviews(
            db=_DB(),  # type: ignore[arg-type]
            search_space_id=7,
            topic_score=_score(_topic("missing-from-feedback", "missing")),
            feedback=_feedback("another-topic"),
            grounding_bundle={"snippets": [_snippet("chunk-1")]},
        )


@pytest.mark.asyncio
async def test_bundle_reuse_adds_citation_only_pointers_without_fresh_retrieval(monkeypatch):
    retrieve = AsyncMock()
    monkeypatch.setattr("apollo.overseer.remediation.retrieve_for_question", retrieve)
    feedback = _feedback("covered", "weak")

    decorated = await add_remediation_reviews(
        db=_DB(),  # type: ignore[arg-type]
        search_space_id=7,
        topic_score=_score(
            _topic("covered", "covered"),
            _topic("weak", "missing"),
        ),
        feedback=feedback,
        grounding_bundle={"snippets": [_snippet("chunk-1", document_id=91)]},
    )

    assert decorated is not None
    assert "review" not in decorated["topic_feedback"][0]
    assert decorated["topic_feedback"][1]["review"] == [
        {"doc_id": 91, "label": "[Textbook, p. 12]", "page": 12, "upload_id": None}
    ]
    assert set(decorated["topic_feedback"][1]["review"][0]) == {
        "doc_id",
        "label",
        "page",
        "upload_id",
    }
    assert "review" not in feedback["topic_feedback"][1]
    retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_present_bundle_never_falls_back_to_fresh_retrieval(monkeypatch):
    retrieve = AsyncMock(return_value=([_snippet("fresh")], {}))
    monkeypatch.setattr("apollo.overseer.remediation.retrieve_for_question", retrieve)
    feedback = _feedback("weak")
    before = deepcopy(feedback)

    decorated = await add_remediation_reviews(
        db=_DB(),  # type: ignore[arg-type]
        search_space_id=7,
        topic_score=_score(_topic("weak", "missing")),
        feedback=feedback,
        grounding_bundle={"snippets": []},
    )

    assert decorated is None
    assert feedback == before
    retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_retrieval_is_capped_at_three_weak_topics(monkeypatch):
    retrieve = AsyncMock(return_value=([_snippet("chunk-1")], {}))
    monkeypatch.setattr("apollo.overseer.remediation.retrieve_for_question", retrieve)
    keys = tuple(f"weak-{index}" for index in range(5))

    decorated = await add_remediation_reviews(
        db=_DB(),  # type: ignore[arg-type]
        search_space_id=7,
        topic_score=_score(*(_topic(key, "missing", display_name=key) for key in keys)),
        feedback=_feedback(*keys),
        grounding_bundle=None,
    )

    assert decorated is not None
    assert retrieve.await_count == 3
    assert [
        item["canonical_key"] for item in decorated["topic_feedback"] if "review" in item
    ] == list(keys[:3])


@pytest.mark.asyncio
async def test_empty_retrieval_returns_no_decoration_and_preserves_feedback(monkeypatch):
    retrieve = AsyncMock(return_value=([], {}))
    monkeypatch.setattr("apollo.overseer.remediation.retrieve_for_question", retrieve)
    feedback = _feedback("weak")
    before = deepcopy(feedback)

    decorated = await add_remediation_reviews(
        db=_DB(),  # type: ignore[arg-type]
        search_space_id=7,
        topic_score=_score(_topic("weak", "missing")),
        feedback=feedback,
        grounding_bundle=None,
    )

    assert decorated is None
    assert feedback == before


@pytest.mark.asyncio
async def test_later_empty_retrieval_discards_all_topic_decorations(monkeypatch):
    retrieve = AsyncMock(side_effect=[([_snippet("first")], {}), ([], {})])
    monkeypatch.setattr("apollo.overseer.remediation.retrieve_for_question", retrieve)
    feedback = _feedback("weak-1", "weak-2")
    before = deepcopy(feedback)

    decorated = await add_remediation_reviews(
        db=_DB(),  # type: ignore[arg-type]
        search_space_id=7,
        topic_score=_score(
            _topic("weak-1", "missing"),
            _topic("weak-2", "partial"),
        ),
        feedback=feedback,
        grounding_bundle=None,
    )

    assert decorated is None
    assert feedback == before
    assert all("review" not in item for item in feedback["topic_feedback"])


@pytest.mark.asyncio
async def test_retrieval_failure_never_mutates_feedback(monkeypatch):
    monkeypatch.setattr(
        "apollo.overseer.remediation.retrieve_for_question",
        AsyncMock(side_effect=RuntimeError("retrieval unavailable")),
    )
    feedback = _feedback("weak")
    before = deepcopy(feedback)

    with pytest.raises(RuntimeError, match="retrieval unavailable"):
        await add_remediation_reviews(
            db=_DB(),  # type: ignore[arg-type]
            search_space_id=7,
            topic_score=_score(_topic("weak", "missing")),
            feedback=feedback,
            grounding_bundle=None,
        )

    assert feedback == before


@pytest.mark.asyncio
async def test_solution_documents_never_surface_in_review():
    decorated = await add_remediation_reviews(
        db=_DB(),  # type: ignore[arg-type]
        search_space_id=7,
        topic_score=_score(_topic("weak", "missing")),
        feedback=_feedback("weak"),
        grounding_bundle={
            "snippets": [
                _snippet("solution", metadata={"authored_role": "solution"}),
                _snippet("answer-key", metadata={"doc_kind": "answer_key"}),
                _snippet("safe"),
            ]
        },
    )

    assert decorated is not None
    assert decorated["topic_feedback"][0]["review"] == [
        {"doc_id": "safe", "label": "[Textbook, p. 12]", "page": 12, "upload_id": None}
    ]


@pytest.mark.asyncio
async def test_remediation_preserves_the_hoot_assisted_flag(monkeypatch):
    """INTERACTION5: the additive ``hoot_assisted`` key on a feedback item must
    survive the copy-on-success decoration (the pass only ADDS a ``review`` key)."""
    retrieve = AsyncMock()
    monkeypatch.setattr("apollo.overseer.remediation.retrieve_for_question", retrieve)
    feedback = {
        "headline": "Keep building.",
        "topic_feedback": [
            {
                "canonical_key": "weak",
                "note": "Review weak.",
                "quote": None,
                "hoot_assisted": True,
            }
        ],
        "recap": [],
        "next_step": "Teach it yourself.",
    }

    decorated = await add_remediation_reviews(
        db=_DB(),  # type: ignore[arg-type]
        search_space_id=7,
        topic_score=_score(_topic("weak", "missing")),
        feedback=feedback,
        grounding_bundle={"snippets": [_snippet("chunk-1", document_id=91)]},
    )

    assert decorated is not None
    item = decorated["topic_feedback"][0]
    assert item["hoot_assisted"] is True
    assert item["review"] == [
        {"doc_id": 91, "label": "[Textbook, p. 12]", "page": 12, "upload_id": None}
    ]
    retrieve.assert_not_awaited()
