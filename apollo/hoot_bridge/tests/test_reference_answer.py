"""Unit coverage for the stateless INTERACTION4 reference-answer bridge.

Every Hoot collaborator is mocked: these tests exercise the bridge's scope
and leakage boundaries without a database, retrieval service, or LLM.
"""

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai.prompts.apollo_aside import apollo_aside_prompt
from apollo.hoot_bridge.reference_answer import (
    ReferenceAsideResult,
    _document_id_of,
    _excluded_document_ids,
    _filter_leaked_snippets,
    _strip_trailing_citations_block,
    _structured_citations,
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
    relevance.assert_called_once()
    assert relevance.call_args.args[0] == "Where should I go this weekend?"
    retrieval.assert_not_awaited()


def test_interaction4_defaults_off(monkeypatch):
    monkeypatch.delenv("INTERACTION4", raising=False)

    assert is_enabled() is False


def test_malformed_document_id_is_treated_as_unknown():
    snippet = _snippet(snippet_id="bad-id", document_id=10, marker="[Textbook p. 1]")
    snippet.metadata["document_id"] = "not-an-integer"

    assert _document_id_of(snippet) is None
    assert _filter_leaked_snippets([snippet], {10}) == [snippet]


def test_structured_citations_maps_only_used_markers():
    used = _snippet(snippet_id="used", document_id=10, marker="[Textbook p. 1]")
    used.metadata.update(
        {
            "bbox": [1, 2, 3, 4],
            "ocr_confidence": 0.98,
            "ocr_provider": "test-ocr",
            "page_asset": "page-1.png",
            "raw_latex": "x^2",
            "teacher_upload_id": 55,
            "week": 3,
        }
    )
    unused = _snippet(snippet_id="unused", document_id=11, marker="[Textbook p. 2]")
    formatter = MagicMock(return_value=("formatted", [{"label": "Textbook p. 1"}]))

    with patch("citations.formatter.format_citations", new=formatter):
        citations = _structured_citations(
            SimpleNamespace(snippets=[used, unused]),
            [" [Textbook p. 1] ", None],  # type: ignore[list-item]
        )

    assert citations == [{"label": "Textbook p. 1"}]
    entries, id_to_row, store_meta = formatter.call_args.args
    assert entries == [{"id": "used", "snippet": used}]
    assert id_to_row["used"] == {
        "store_key": "55",
        "store_kind": "other",
        "source_path": "document-10.pdf",
        "page": 1,
        "bbox": [1, 2, 3, 4],
    }
    assert store_meta["55"] == {
        "kind": "other",
        "week": 3,
        "average_confidence": 0.98,
        "ocr_provider": "test-ocr",
        "teacher_upload_id": 55,
        "page_asset": "page-1.png",
        "raw_latex": "x^2",
    }


def test_structured_citations_ignores_non_string_markers():
    assert _structured_citations(SimpleNamespace(snippets=[]), [None, "  "]) == []  # type: ignore[list-item]


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


async def test_partial_relevance_uses_on_topic_portion_and_normalizes_keywords():
    extraction = MagicMock(
        return_value=(
            "context",
            [
                " network effect ",
                {"keyword": "adoption"},
                {"name": "value"},
                {"term": "  "},
                42,
            ],
        )
    )
    retrieval = AsyncMock(return_value=([], {}))

    with (
        patch(
            "ai.main_ai.check_question_relevance",
            return_value={
                "relevance": "partial",
                "on_topic_portion": "How do network effects work?",
            },
        ),
        patch("ai.main_ai.extract_and_filter_keywords", new=extraction),
        patch("retrieval.pipeline.retrieve_for_question", new=retrieval),
        patch(
            "apollo.hoot_bridge.reference_answer._excluded_document_ids",
            new=AsyncMock(return_value=set()),
        ),
    ):
        result = await answer_reference_question(
            db=MagicMock(),
            course_id=9,
            question="How do network effects work, and what is for lunch?",
            problem=MagicMock(),
        )

    assert result.text == "Not found in the approved materials."
    extraction.assert_called_once()
    assert extraction.call_args.args[0] == "How do network effects work?"
    assert retrieval.await_args.kwargs["keywords"] == ["network effect", "adoption", "value"]


async def test_retrieval_errors_propagate_to_the_caller():
    with (
        patch(
            "ai.main_ai.check_question_relevance",
            return_value={"relevance": "full"},
        ),
        patch("ai.main_ai.extract_and_filter_keywords", return_value=("", [])),
        patch(
            "retrieval.pipeline.retrieve_for_question",
            new=AsyncMock(side_effect=RuntimeError("retrieval unavailable")),
        ),
    ):
        with pytest.raises(RuntimeError, match="retrieval unavailable"):
            await answer_reference_question(
                db=MagicMock(),
                course_id=9,
                question="What is a network effect?",
                problem=MagicMock(),
            )


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


async def test_aside_answer_uses_the_compact_refresher_system_prompt():
    """The aside lane must swap the tutor prompt for apollo_aside_prompt() —
    the override is what reaches the solver, never the standalone-chat prompt."""
    snippet = _snippet(snippet_id="safe", document_id=10, marker="[Safe p. 1]")
    retrieval = AsyncMock(
        return_value=([snippet], {"combined_query": "network effect", "hit_count_sem": 1})
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
                return_value=FinalAnswer(text="A network effect.", citations=[]),
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
                new=AsyncMock(return_value=set()),
            )
        )

        await answer_reference_question(
            db=MagicMock(),
            course_id=9,
            question="What is a network effect?",
            problem=MagicMock(),
        )

    assert solve.call_args.kwargs["system_prompt_override"] == apollo_aside_prompt()


# ---------------------------------------------------------------------------
# Trailing "Citations: [..]" strip (aside-lane-only post-process)
# ---------------------------------------------------------------------------

# The exact live-stack shape the user rejected: body + claim markers, a
# hand-back line, then format_answer's redundant trailing enumeration.
_REJECTED_ASIDE_TEXT = (
    "Intellectual capital is the information that forms the foundation from which "
    "human beings craft their lives and secure their dignity. This course material "
    "states that information forms the intellectual capital from which human beings "
    "craft their lives, and describes intellectual capital as the foundation from "
    "which we craft our lives and secure our dignity. When our information is stolen, "
    "denied, exposed, or corrupted, our dignity itself is threatened, which means our "
    "intellectual capital is being harmed.[Mason 1986, p. 2][Module 9 Ethics Slides, p. 2]"
    "\n\nNow try putting that definition into your own words as you explain it to Apollo. "
    "[Module 9 Ethics Slides, p. 2]"
    "\n\nCitations: [Mason 1986, p. 2], [Module 9 Ethics Slides, p. 2]"
)


def test_strip_trailing_citations_block_removes_the_redundant_enumeration():
    """The trailing "Citations: [..]" block is dropped; body + claim-level
    citations + the hand-back line survive verbatim."""
    stripped = _strip_trailing_citations_block(_REJECTED_ASIDE_TEXT)

    assert "\n\nCitations:" not in stripped
    assert not stripped.endswith("Citations: [Mason 1986, p. 2], [Module 9 Ethics Slides, p. 2]")
    assert stripped.endswith(
        "Now try putting that definition into your own words as you explain it to Apollo. "
        "[Module 9 Ethics Slides, p. 2]"
    )
    # Claim-level markers inside the body are untouched.
    assert "[Mason 1986, p. 2]" in stripped
    assert "[Module 9 Ethics Slides, p. 2]" in stripped


def test_strip_trailing_citations_block_passes_clean_text_through():
    """Text with no trailing enumeration is returned byte-identical."""
    clean = (
        "Intellectual capital is the information you build your life on "
        "[Mason 1986, p. 2].\n\nNow put that into your own words for Apollo."
    )

    assert _strip_trailing_citations_block(clean) == clean


def test_strip_trailing_citations_block_only_touches_the_trailing_block():
    """A "Citations:" that is NOT the trailing block (no leading blank line) is
    left alone — only format_answer's exact trailing shape is stripped."""
    text = "See Citations: below for details.\n\nThe answer is X [A, p. 1]."

    assert _strip_trailing_citations_block(text) == text


# ---------------------------------------------------------------------------
# Course-subject + topic-hint wiring into the relevance guard
# ---------------------------------------------------------------------------


def _ethics_problem() -> SimpleNamespace:
    return SimpleNamespace(
        concept_id="ethics",
        problem_text=(
            "A city deploys license plate readers across downtown. "
            "Evaluate the passive-observation concerns this raises."
        ),
        database_id=None,
    )


class _FirstRow:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


async def test_guard_receives_course_subject_and_current_topic():
    """The guard must classify against the course's subject and the problem's
    topic hint — never the bare question alone (the 2026-07-30 out-of-scope bug)."""
    relevance = MagicMock(
        return_value={
            "relevance": "none",
            "on_topic_portion": "",
            "off_topic_portion": "surveillance tools",
            "reason": "outside the course",
        }
    )

    with (
        patch("ai.main_ai.check_question_relevance", new=relevance),
        patch("retrieval.pipeline.retrieve_for_question", new=AsyncMock()),
        patch(
            "apollo.hoot_bridge.reference_answer._course_subject",
            new=AsyncMock(return_value="Management Information Systems"),
        ),
    ):
        await answer_reference_question(
            db=MagicMock(),
            course_id=9,
            question="What are some examples of surveillance tools?",
            problem=_ethics_problem(),
        )

    kwargs = relevance.call_args.kwargs
    assert relevance.call_args.args[0] == "What are some examples of surveillance tools?"
    assert kwargs["subject"] == "Management Information Systems"
    assert kwargs["current_topic"].startswith("ethics")
    assert "license plate readers" in kwargs["current_topic"]


async def test_keyword_extraction_receives_course_subject():
    """extract_and_filter_keywords must filter against the same course subject."""
    extraction = MagicMock(return_value=("", []))

    with (
        patch(
            "ai.main_ai.check_question_relevance",
            return_value={"relevance": "full", "on_topic_portion": ""},
        ),
        patch("ai.main_ai.extract_and_filter_keywords", new=extraction),
        patch("retrieval.pipeline.retrieve_for_question", new=AsyncMock(return_value=([], {}))),
        patch(
            "apollo.hoot_bridge.reference_answer._excluded_document_ids",
            new=AsyncMock(return_value=set()),
        ),
        patch(
            "apollo.hoot_bridge.reference_answer._course_subject",
            new=AsyncMock(return_value="Management Information Systems"),
        ),
    ):
        await answer_reference_question(
            db=MagicMock(),
            course_id=9,
            question="What are some examples of surveillance tools?",
            problem=_ethics_problem(),
        )

    assert extraction.call_args.kwargs["subject"] == "Management Information Systems"


async def test_course_subject_prefers_subject_name_then_name():
    from apollo.hoot_bridge.reference_answer import _course_subject

    db = MagicMock()
    db.execute = AsyncMock(return_value=_FirstRow(("Management Information Systems", "MGMT 38200")))
    assert await _course_subject(db, course_id=1) == "Management Information Systems"

    db.execute = AsyncMock(return_value=_FirstRow(("  ", "MGMT 38200: Intro to MIS")))
    assert await _course_subject(db, course_id=1) == "MGMT 38200: Intro to MIS"


async def test_course_subject_falls_back_to_global_when_course_missing():
    from apollo.hoot_bridge.reference_answer import _course_subject

    db = MagicMock()
    db.execute = AsyncMock(return_value=_FirstRow(None))

    with patch("config.settings.get_subject_name", return_value="course/textbook"):
        assert await _course_subject(db, course_id=404) == "course/textbook"


async def test_course_subject_swallows_db_errors_into_global_fallback():
    """Subject is an advisory input like keywords — a lookup failure must not
    kill the aside; it degrades to the global subject."""
    from apollo.hoot_bridge.reference_answer import _course_subject

    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db unavailable"))

    with patch("config.settings.get_subject_name", return_value="course/textbook"):
        assert await _course_subject(db, course_id=1) == "course/textbook"


def test_topic_hint_composes_slug_and_problem_text():
    from apollo.hoot_bridge.reference_answer import _topic_hint

    hint = _topic_hint(
        SimpleNamespace(
            concept_id="network-effects",
            problem_text="Why do platforms get more valuable as more people join?",
        )
    )
    assert hint is not None
    assert hint.startswith("network effects")
    assert "more valuable as more people join" in hint


def test_topic_hint_truncates_long_problem_text():
    from apollo.hoot_bridge.reference_answer import _topic_hint

    hint = _topic_hint(SimpleNamespace(concept_id="ethics", problem_text="x" * 2000))
    assert hint is not None
    assert len(hint) < 400


def test_topic_hint_is_none_for_malformed_problem():
    from apollo.hoot_bridge.reference_answer import _topic_hint

    assert _topic_hint(MagicMock()) is None
    assert _topic_hint(SimpleNamespace(concept_id="", problem_text="")) is None


async def test_aside_answer_strips_trailing_citations_block_end_to_end():
    """End-to-end: format_answer's in-text "Citations:" enumeration is stripped
    from the aside card text, while the structured citations payload is intact."""
    snippet = _snippet(snippet_id="safe", document_id=10, marker="[Mason 1986, p. 2]")
    retrieval = AsyncMock(
        return_value=([snippet], {"combined_query": "intellectual capital", "hit_count_sem": 1})
    )
    answer_with_block = FinalAnswer(
        text=(
            "Intellectual capital is the information you build your life on "
            "[Mason 1986, p. 2].\n\nNow put that into your own words for Apollo."
            "\n\nCitations: [Mason 1986, p. 2]"
        ),
        citations=["[Mason 1986, p. 2]"],
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "ai.main_ai.check_question_relevance",
                return_value={"relevance": "full", "on_topic_portion": "intellectual capital"},
            )
        )
        stack.enter_context(
            patch("ai.main_ai.extract_and_filter_keywords", return_value=("context", []))
        )
        stack.enter_context(patch("ai.main_ai.parse_question", return_value=MagicMock()))
        stack.enter_context(
            patch("ai.main_ai.solve_with_bundle", new=MagicMock(return_value=MagicMock()))
        )
        stack.enter_context(patch("ai.main_ai.format_answer", return_value=answer_with_block))
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
                new=AsyncMock(return_value=set()),
            )
        )
        stack.enter_context(
            patch(
                "apollo.hoot_bridge.reference_answer._structured_citations",
                return_value=[{"label": "Mason 1986, p. 2"}],
            )
        )

        result = await answer_reference_question(
            db=MagicMock(),
            course_id=9,
            question="What does Mason mean by intellectual capital?",
            problem=MagicMock(),
        )

    assert "\n\nCitations:" not in result.text
    assert result.text.endswith("Now put that into your own words for Apollo.")
    assert "[Mason 1986, p. 2]" in result.text  # claim-level citation preserved
    assert result.citations == [{"label": "Mason 1986, p. 2"}]
