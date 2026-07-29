"""Unit coverage for ``apollo/overseer/grounding.py`` (INTERACTION2).

The module is the ONLY thing standing between a persisted retrieval bundle and
the grade of record, so the cases that matter are the degenerate ones: a NULL
column, a corrupt row, a solution-marked snippet, and a bundle far larger than
the evidence budget.
"""

from __future__ import annotations

import pytest

from apollo.overseer.grounding import (
    EVIDENCE_TOKEN_CAP,
    MAX_EVIDENCE_SNIPPETS,
    build_course_evidence,
    evidence_block,
    extract_snippets,
    grounding_provenance,
    is_solution_bearing,
)

pytestmark = pytest.mark.unit


def _snippet(
    *,
    text: str = "Bernoulli's equation holds along a streamline.",
    marker: str = "[Lecture 4, p. 12]",
    section: str = "2.1 Streamlines",
    source_path: str = "docs/lecture4.pdf",
    metadata: dict | None = None,
) -> dict:
    return {
        "id": "chunk-1",
        "type": "body",
        "page": 12,
        "section_path": section,
        "text": text,
        "figure_id": None,
        "why": "hit",
        "source_path": source_path,
        "doc_title": "Lecture 4",
        "doc_short": "Lecture 4",
        "citation_marker": marker,
        "final_score": {"final": 0.9},
        "metadata": metadata if metadata is not None else {},
    }


def _bundle(*snippets: dict) -> dict:
    return {
        "snippets": list(snippets),
        "diag": {},
        "built_at": "2026-07-26T00:00:00+00:00",
        "retrieval_version": "retrieve_for_question:v1",
    }


# --------------------------------------------------------------------------
# Degenerate bundles never raise and never produce an empty block
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bundle",
    [
        None,
        {},
        {"snippets": None},
        {"snippets": "not-a-list"},
        {"snippets": []},
        {"snippets": [None, 3, "text"]},
        {"snippets": [{"text": "   "}]},
        {"snippets": [{"citation_marker": "[X]"}]},  # no text at all
        "corrupt-json-decoded-to-a-string",
        [1, 2, 3],
    ],
)
def test_unusable_bundles_yield_none(bundle):
    """Every shape the JSONB column can hold degrades to ``None``, not an
    exception and not an empty block — an empty block would change the prompt."""
    assert build_course_evidence(bundle) is None
    assert evidence_block(build_course_evidence(bundle)) is None


def test_extract_skips_unusable_entries_but_keeps_the_rest():
    bundle = {"snippets": [None, _snippet(text="keep me"), {"text": ""}, 7]}
    assert [s["text"] for s in extract_snippets(bundle)] == ["keep me"]


# --------------------------------------------------------------------------
# Leakage gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [
        {"authored_role": "solution"},
        {"authored_role": "SOLUTION"},
        {"doc_kind": "answer_key"},
        {"document_kind": "Solution_Manual"},
        {"document_role": "solutions"},
        {"kind": "authored_solution"},
        {"material_kind": " solution "},
    ],
)
def test_solution_bearing_snippets_are_detected(metadata):
    assert is_solution_bearing(_snippet(metadata=metadata)) is True


@pytest.mark.parametrize(
    "metadata",
    [{}, {"material_kind": "textbook"}, {"authored_role": "problem"}, {"doc_kind": "slides"}],
)
def test_student_safe_snippets_are_not_flagged(metadata):
    assert is_solution_bearing(_snippet(metadata=metadata)) is False


def test_solution_text_never_reaches_the_evidence_block():
    """The read side owns its own leakage gate even though session-init already
    filtered: a bundle written by a looser builder must still not leak."""
    bundle = _bundle(
        _snippet(text="SAFE COURSE TEXT"),
        _snippet(text="WORKED SOLUTION: v = 12.4 m/s", metadata={"authored_role": "solution"}),
    )
    evidence = build_course_evidence(bundle)

    assert evidence is not None
    assert "SAFE COURSE TEXT" in evidence.block
    assert "WORKED SOLUTION" not in evidence.block
    assert evidence.snippet_count == 1


def test_bundle_of_only_solution_snippets_is_none():
    bundle = _bundle(_snippet(text="answer: 42", metadata={"doc_kind": "answer_key"}))
    assert build_course_evidence(bundle) is None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_block_carries_citation_markers_and_section_paths():
    evidence = build_course_evidence(_bundle(_snippet()))

    assert evidence is not None
    assert evidence.block.startswith("[Lecture 4, p. 12] — 2.1 Streamlines\n")
    assert "Bernoulli's equation holds along a streamline." in evidence.block


def test_missing_marker_degrades_to_a_document_label():
    evidence = build_course_evidence(_bundle(_snippet(marker="", section="")))

    assert evidence is not None
    assert evidence.block.startswith("[Lecture 4]\n")


def test_entries_are_blank_line_separated_in_bundle_order():
    evidence = build_course_evidence(
        _bundle(_snippet(text="first", section=""), _snippet(text="second", section=""))
    )

    assert evidence is not None
    assert evidence.block == "[Lecture 4, p. 12]\nfirst\n\n[Lecture 4, p. 12]\nsecond"


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------


def test_block_is_capped_by_dropping_trailing_snippets():
    big = _snippet(text="x" * 4000, section="")
    evidence = build_course_evidence(_bundle(big, big, big, big, big))

    assert evidence is not None
    assert len(evidence.block) <= EVIDENCE_TOKEN_CAP * 4
    assert evidence.snippet_count < 5
    assert evidence.truncated is True


def test_a_single_oversized_snippet_is_hard_truncated_not_dropped():
    evidence = build_course_evidence(
        _bundle(_snippet(text="y" * 50_000, section="")), token_cap=100
    )

    assert evidence is not None
    assert evidence.snippet_count == 1
    assert len(evidence.block) <= 400
    assert evidence.block.endswith("…[truncated]")
    assert evidence.truncated is True


def test_snippet_count_is_capped_even_when_everything_fits():
    small = _snippet(text="tiny", section="")
    evidence = build_course_evidence(_bundle(*[small] * (MAX_EVIDENCE_SNIPPETS + 3)))

    assert evidence is not None
    assert evidence.snippet_count == MAX_EVIDENCE_SNIPPETS
    assert evidence.truncated is True


def test_a_bundle_inside_the_budget_is_not_marked_truncated():
    evidence = build_course_evidence(_bundle(_snippet(text="short"), _snippet(text="also short")))

    assert evidence is not None
    assert evidence.snippet_count == 2
    assert evidence.truncated is False


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


def test_provenance_for_no_evidence():
    assert grounding_provenance(None) == {"used": False, "snippet_count": 0, "doc_ids": []}


def test_provenance_reports_deduped_doc_ids_in_order():
    evidence = build_course_evidence(
        _bundle(
            _snippet(source_path="docs/lecture4.pdf"),
            _snippet(source_path="docs/lecture4.pdf"),
            _snippet(source_path="docs/textbook.pdf"),
        )
    )

    assert grounding_provenance(evidence) == {
        "used": True,
        "snippet_count": 3,
        "doc_ids": ["docs/lecture4.pdf", "docs/textbook.pdf"],
    }


def test_metadata_document_id_wins_over_source_path():
    evidence = build_course_evidence(_bundle(_snippet(metadata={"document_id": 17})))

    assert grounding_provenance(evidence)["doc_ids"] == ["17"]
