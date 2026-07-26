"""Unit coverage for the stateless INTERACTION4 reference-answer bridge.

Every Hoot collaborator is mocked: these tests exercise the bridge's scope
and leakage boundaries without a database, retrieval service, or LLM.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.hoot_bridge.reference_answer import (
    ReferenceAsideResult,
    _excluded_document_ids,
    _filter_leaked_snippets,
    answer_reference_question,
    is_enabled,
)
from config.contracts import BundleSnippet, FinalAnswer

pytestmark = pytest.mark.unit


def _snippet(*, snippet_id: str, document_id: int, marker: str) -> BundleSnippet:
    return BundleSnippet(
        id=snippet_id,
        type="text",
        page=1,
        section_path="",
        text=f"content from document {document_id}",
        figure_id=None,
        why="relevant",
        source_path=f"document-{document_id}.pdf",
        doc_title=f"Document {document_id}",
        doc_short=f"doc-{document_id}",
        citation_marker=marker,
        metadata={"document_id": document_id, "material_kind": "other"},
    )


class _ScalarRows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _ScalarOne:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


async def test_solution_bearing_document_snippets_are_excluded_course_wide():
    """All solution-document ids registered for the course are leakage ids."""
    db = MagicMock()
    db.execute = AsyncMock(return_value=_ScalarRows([71, 72, None]))
    db.get = AsyncMock()
    problem = SimpleNamespace(database_id=None)

    excluded = await _excluded_document_ids(db, course_id=9, problem=problem)
    snippets = [
        _snippet(snippet_id="safe", document_id=10, marker="[Safe p. 1]"),
        _snippet(snippet_id="solution-a", document_id=71, marker="[Solution A p. 1]"),
        _snippet(snippet_id="solution-b", document_id=72, marker="[Solution B p. 1]"),
    ]

    assert excluded == {71, 72}
    assert [snippet.id for snippet in _filter_leaked_snippets(snippets, excluded)] == ["safe"]
    db.get.assert_not_awaited()


async def test_current_problems_paired_solution_document_is_excluded_by_id():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_ScalarRows([]), _ScalarOne(88)])
    db.get = AsyncMock(return_value=SimpleNamespace(provenance={"document_id": 44}))
    problem = SimpleNamespace(database_id=12)

    excluded = await _excluded_document_ids(db, course_id=9, problem=problem)

    assert excluded == {88}
    db.get.assert_awaited_once()


async def test_out_of_scope_question_returns_in_scope_refusal_aside():
    relevance = MagicMock(
        return_value={
            "relevance": "none",
            "on_topic_portion": "",
            "off_topic_portion": "weekend plans",
            "reason": "outside the course",
        }
    )
    retrieval = AsyncMock()

    with (
        patch("ai.main_ai.check_question_relevance", new=relevance),
        patch("retrieval.pipeline.retrieve_for_question", new=retrieval),
    ):
        result = await answer_reference_question(
            db=MagicMock(),
            course_id=9,
            question="Where should I go this weekend?",
            problem=MagicMock(),
        )

    assert result == ReferenceAsideResult(
        in_scope=False,
        text="That's outside what's covered in this course's materials.",
        citations=[],
    )
    relevance.assert_called_once_with("Where should I go this weekend?")
    retrieval.assert_not_awaited()


def test_interaction4_defaults_off(monkeypatch):
    monkeypatch.delenv("INTERACTION4", raising=False)

    assert is_enabled() is False


async def test_keyword_extraction_failure_is_swallowed():
    """Keyword hints are optional; retrieval still runs with an empty list."""
    retrieval = AsyncMock(return_value=([], {"combined_query": "network effect"}))

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "ai.main_ai.check_question_relevance",
                return_value={
                    "relevance": "full",
                    "on_topic_portion": "network effect",
                    "off_topic_portion": "",
                    "reason": "in scope",
                },
            )
        )
        stack.enter_context(
            patch(
                "ai.main_ai.extract_and_filter_keywords",
                side_effect=RuntimeError("optional keyword model unavailable"),
            )
        )
        stack.enter_context(patch("retrieval.pipeline.retrieve_for_question", new=retrieval))
        stack.enter_context(
            patch(
                "apollo.hoot_bridge.reference_answer._excluded_document_ids",
                new=AsyncMock(return_value=set()),
            )
        )

        result = await answer_reference_question(
            db=MagicMock(),
            course_id=9,
            question="What is a network effect?",
            problem=MagicMock(),
        )

    assert result == ReferenceAsideResult(
        in_scope=True,
        text="Not found in the approved materials.",
        citations=[],
    )
    assert retrieval.await_args.kwargs["keywords"] == []


async def test_excluded_solution_snippets_never_reach_answer_llm():
    safe = _snippet(snippet_id="safe", document_id=10, marker="[Safe p. 1]")
    leaked = _snippet(snippet_id="solution", document_id=88, marker="[Solution p. 1]")
    retrieval = AsyncMock(
        return_value=(
            [safe, leaked],
            {"combined_query": "network effect", "hit_count_sem": 2},
        )
    )
    solve = MagicMock(return_value=MagicMock())

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "ai.main_ai.check_question_relevance",
                return_value={
                    "relevance": "full",
                    "on_topic_portion": "network effect",
                    "off_topic_portion": "",
                    "reason": "in scope",
                },
            )
        )
        stack.enter_context(
            patch(
                "ai.main_ai.extract_and_filter_keywords",
                return_value=("context", [{"term": "network effect"}]),
            )
        )
        stack.enter_context(patch("ai.main_ai.parse_question", return_value=MagicMock()))
        stack.enter_context(patch("ai.main_ai.solve_with_bundle", new=solve))
        stack.enter_context(
            patch(
                "ai.main_ai.format_answer",
                return_value=FinalAnswer(
                    text="A network effect grows with adoption.", citations=[]
                ),
            )
        )
        stack.enter_context(patch("retrieval.pipeline.retrieve_for_question", new=retrieval))
        stack.enter_context(
            patch(
                "retrieval.context_packer._summarize_snippets",
                return_value=([], [], [], {}),
            )
        )
        stack.enter_context(
            patch(
                "apollo.hoot_bridge.reference_answer._excluded_document_ids",
                new=AsyncMock(return_value={88}),
            )
        )

        result = await answer_reference_question(
            db=MagicMock(),
            course_id=9,
            question="What is a network effect?",
            problem=MagicMock(),
        )

    answer_bundle = solve.call_args.args[1]
    assert [snippet.id for snippet in answer_bundle.snippets] == ["safe"]
    assert answer_bundle.allowed_markers == ["[Safe p. 1]"]
    assert result.text == "A network effect grows with adoption."
