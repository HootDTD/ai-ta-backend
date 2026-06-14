"""Tests for the zero-width artifact sanitizer in format_answer.

gpt-5 models sporadically emit literal "&#8203;" (zero-width space) entities
before citation markers; answers are plain text, so clients render the entity
as visible garbage. format_answer is the final gate and must strip them.
"""

from __future__ import annotations

import pytest

from ai.main_ai import _strip_zero_width, format_answer
from config.contracts import (
    BundleSnippet,
    ProposedSolution,
    ResearchBundle,
    ResearchMetadata,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("dirty", "clean"),
    [
        ("sum.&#8203;[Textbook, p. 404]", "sum.[Textbook, p. 404]"),
        ("sum.&#8203[Textbook, p. 404]", "sum.[Textbook, p. 404]"),  # no semicolon
        ("a&#x200B;b", "ab"),
        ("a&#X200b;b", "ab"),  # hex marker is case-insensitive
        ("a\u200bb\u200cc\u200dd\ufeffe", "abcde"),  # real characters
        ("&#8204;&#8205;&#65279;", ""),  # ZWNJ / ZWJ / BOM entities
        ("no artifacts here [Textbook, p. 12]", "no artifacts here [Textbook, p. 12]"),
    ],
)
def test_strip_zero_width(dirty: str, clean: str):
    assert _strip_zero_width(dirty) == clean


@pytest.mark.unit
def test_format_answer_strips_artifacts_and_keeps_citations():
    snippet = BundleSnippet(
        id="1",
        type="body",
        page=404,
        section_path="9.1",
        text="An infinite series is a sum of infinitely many terms.",
        figure_id=None,
        why="hit",
        source_path="",
        doc_title="Calc Textbook",
        doc_short="Calc Textbook",
        citation_marker="[Textbook, p. 404]",
        final_score={"final": 0.5},
        metadata={},
    )
    bundle = ResearchBundle(
        metadata=ResearchMetadata(final_query="q", original_query="q"),
        snippets=[snippet],
        allowed_markers=["[Textbook, p. 404]"],
    )
    solution = ProposedSolution(
        steps=(
            "An infinite series is a sum of infinitely many terms."
            "&#8203;[Textbook, p. 404] Its value is the limit of partial sums."
            "&#8203[Textbook, p. 404]"
        ),
        final_answers={},
        equations_used=[],
        assumptions=[],
        code=None,
        code_output=None,
        code_hash=None,
        vars_created=[],
    )

    final = format_answer(solution, bundle, include_background=False)

    assert "&#8203" not in final.text
    assert "\u200b" not in final.text
    assert "[Textbook, p. 404]" in final.text
