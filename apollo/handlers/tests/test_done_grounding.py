"""INTERACTION2 grounding wiring in ``apollo/handlers/done.py``.

Same no-Docker harness as the other Done unit tests (``_old_path_patches``):
every collaborator is mocked, so these assert the WIRING — which prompt argument
the grader and the narrator receive, and what lands in
``grading_provenance["grounding"]`` — not model behavior.

The load-bearing cases are the failure ones. Grounding runs AHEAD of the sole
grading lane, so a flag that is off, a NULL bundle, a corrupt bundle, or an
exploding builder must all produce the ungrounded call and a completed Done,
never a new hard failure ahead of ``CoverageGradingError`` -> 503.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.errors import CoverageGradingError
from apollo.handlers.tests._done_fixtures import _old_path_patches

pytestmark = pytest.mark.unit

_VALID_VERDICT = {
    "per_step": {},
    "procedure_scores": {},
    "confidences": {},
    "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
}

_SAFE_TEXT = "In this course the head form is p/(rho g) + v^2/(2g) + z = H."
_SOLUTION_TEXT = "WORKED SOLUTION: v_2 = 12.4 m/s."


def _snippet(text: str, *, marker: str = "[Lecture 4, p. 12]", metadata: dict | None = None):
    return {
        "id": "chunk-1",
        "type": "body",
        "page": 12,
        "section_path": "2.1 Streamlines",
        "text": text,
        "figure_id": None,
        "why": "hit",
        "source_path": "docs/lecture4.pdf",
        "doc_title": "Lecture 4",
        "doc_short": "Lecture 4",
        "citation_marker": marker,
        "final_score": {"final": 0.9},
        "metadata": metadata or {},
    }


def _bundle(*snippets):
    return {"snippets": list(snippets), "diag": {}, "retrieval_version": "v1"}


def _drop(patches, *attributes):
    return [p for p in patches if getattr(p, "attribute", None) not in attributes]


async def _run(*, flag_on: bool, bundle, coverage_mock=None, diagnostic_mock=None):
    """Drive ``handle_done`` with the grounding flag and session bundle set."""
    db, sess, _attempt, patches = _old_path_patches()
    sess.grounding_bundle = bundle
    coverage_mock = coverage_mock or AsyncMock(return_value=(_VALID_VERDICT, {}))
    diagnostic_mock = diagnostic_mock or MagicMock(return_value=("narrative", None))
    patches = _drop(patches, "compute_transcript_coverage_with_spans", "generate_diagnostic")

    from apollo.handlers.done import handle_done

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "apollo.handlers.done.interaction2_enabled",
                new=MagicMock(return_value=flag_on),
            )
        )
        stack.enter_context(
            patch(
                "apollo.handlers.done.compute_transcript_coverage_with_spans",
                new=coverage_mock,
            )
        )
        stack.enter_context(patch("apollo.handlers.done.generate_diagnostic", new=diagnostic_mock))
        for p in patches:
            stack.enter_context(p)
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    return out, coverage_mock, diagnostic_mock


# --------------------------------------------------------------------------
# Flag off / bundle absent / bundle corrupt => the ungrounded call
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag_on", "bundle"),
    [
        pytest.param(False, _bundle(_snippet(_SAFE_TEXT)), id="flag-off-with-a-bundle"),
        pytest.param(True, None, id="flag-on-null-bundle"),
        pytest.param(True, "not-json-at-all", id="flag-on-corrupt-bundle-string"),
        pytest.param(True, {"snippets": "corrupt"}, id="flag-on-corrupt-snippets"),
        pytest.param(True, {"snippets": []}, id="flag-on-empty-bundle"),
        pytest.param(
            True,
            _bundle(_snippet(_SOLUTION_TEXT, metadata={"authored_role": "solution"})),
            id="flag-on-solution-only-bundle",
        ),
    ],
)
async def test_ungrounded_paths_pass_none_and_report_unused(flag_on, bundle):
    """The grader and the narrator are called with ``course_evidence=None`` —
    the argument value that makes both prompts byte-identical to pre-feature."""
    out, coverage_mock, diagnostic_mock = await _run(flag_on=flag_on, bundle=bundle)

    assert coverage_mock.await_args.kwargs["course_evidence"] is None
    assert diagnostic_mock.call_args.kwargs["course_evidence"] is None
    assert out["grading_provenance"]["grounding"] == {
        "used": False,
        "snippet_count": 0,
        "doc_ids": [],
    }


async def test_builder_exception_degrades_instead_of_failing_the_done():
    """An unexpected builder failure must never surface ahead of the grading
    lane's own 503 contract."""
    with patch(
        "apollo.handlers.done.build_course_evidence",
        side_effect=RuntimeError("boom"),
    ):
        out, coverage_mock, _ = await _run(flag_on=True, bundle=_bundle(_snippet(_SAFE_TEXT)))

    assert coverage_mock.await_args.kwargs["course_evidence"] is None
    assert out["grading_provenance"]["grounding"]["used"] is False


# --------------------------------------------------------------------------
# Flag on with a usable bundle
# --------------------------------------------------------------------------


async def test_bundle_present_grounds_both_prompts_and_provenance():
    out, coverage_mock, diagnostic_mock = await _run(
        flag_on=True, bundle=_bundle(_snippet(_SAFE_TEXT))
    )

    block = coverage_mock.await_args.kwargs["course_evidence"]
    assert block is not None
    assert _SAFE_TEXT in block
    assert "[Lecture 4, p. 12]" in block
    # The narrator receives the SAME student-safe block.
    assert diagnostic_mock.call_args.kwargs["course_evidence"] == block

    assert out["grading_provenance"]["grounding"] == {
        "used": True,
        "snippet_count": 1,
        "doc_ids": ["docs/lecture4.pdf"],
    }


async def test_solution_snippets_never_reach_the_served_payload():
    """Two-lane evidence rule: a solution-marked snippet is dropped before the
    prompts, so nothing it contains can be echoed into the served response."""
    out, coverage_mock, diagnostic_mock = await _run(
        flag_on=True,
        bundle=_bundle(
            _snippet(_SAFE_TEXT),
            _snippet(_SOLUTION_TEXT, metadata={"doc_kind": "answer_key"}),
        ),
    )

    block = coverage_mock.await_args.kwargs["course_evidence"]
    assert _SOLUTION_TEXT not in block
    assert _SOLUTION_TEXT not in diagnostic_mock.call_args.kwargs["course_evidence"]
    assert _SOLUTION_TEXT not in repr(out)
    assert out["grading_provenance"]["grounding"]["snippet_count"] == 1


# --------------------------------------------------------------------------
# The 503 invariant is untouched
# --------------------------------------------------------------------------


async def test_coverage_grading_error_still_propagates_when_grounded():
    """Grounding must not swallow, wrap, or pre-empt the retryable 503."""
    coverage_mock = AsyncMock(
        side_effect=CoverageGradingError(stage="transcript_adjudication", last_error="429")
    )

    with pytest.raises(CoverageGradingError):
        await _run(
            flag_on=True,
            bundle=_bundle(_snippet(_SAFE_TEXT)),
            coverage_mock=coverage_mock,
        )
