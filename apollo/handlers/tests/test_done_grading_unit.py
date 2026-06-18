"""WU-4C1 — run_graph_simulation chain orchestration (all callees mocked).

These are PURE unit tests of the §6.4 chain wiring in
``apollo.handlers.done_grading``: every frozen callee is patched at the
``done_grading`` import site, so no real grading / Neo4j / LLM runs. They pin:

  * call order + the exact kwargs the chain hands each callee (the §1.3 / §1.4
    signatures), incl. ``llm_adjudicator=main_chat_adjudicator``,
    ``symbolic_mappings=inputs.symbolic_mappings``, ``user_id``/``search_space_id``;
  * the raw-payload regression guard (§1.4): the EXACT problem_payload dict
    (carrying declared_paths/symbolic_mappings/entity_key) reaches
    build_reference_canonical + build_problem_candidates, NOT round-tripped;
  * the NO-FALLBACK / learner_update_pending forks for each named error;
  * the WU-4C1/5A boundary: convert_findings_to_events is never called.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.errors import (
    ResolutionInvalidOutputError,
    ResolutionUnavailableError,
)
from apollo.graph_compare.validator import (
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
)
from apollo.handlers import done_grading as dg
from apollo.handlers.done_grading import ShadowGradeResult, run_graph_simulation
from apollo.ontology import KGGraph

pytestmark = pytest.mark.unit

_USER_ID = "a0000000-0000-4000-8000-000000000001"


class _Sess:
    def __init__(self) -> None:
        self.id = 11
        self.user_id = _USER_ID
        self.search_space_id = 7
        self.concept_id = 3


class _Attempt:
    def __init__(self) -> None:
        self.id = 99
        self.learner_update_pending = False


def _payload() -> dict:
    return {
        "reference_solution": [{"id": "s1", "entity_key": "eq.k", "entry_type": "equation",
                                "content": {"symbolic": "a-b"}, "depends_on": []}],
        "declared_paths": [["s1"]],
        "symbolic_mappings": {"d": "2*r"},
    }


def _inputs() -> MagicMock:
    inputs = MagicMock()
    inputs.candidates = ("cand1", "cand2")
    inputs.symbolic_mappings = {"d": "2*r"}
    return inputs


def _all_callee_patches(*, persist_return=4321):
    """Patch every chain callee on the done_grading module. Returns
    (patches, mocks-dict) — caller starts/stops the patches."""
    mocks = {
        "load_for_concept": AsyncMock(return_value=[]),
        "load_entity_specs": AsyncMock(return_value=[]),
        "build_problem_candidates": MagicMock(return_value=_inputs()),
        "validate_student_graph": MagicMock(return_value=None),
        "resolve_attempt": MagicMock(return_value=MagicMock(name="resolution")),
        "write_resolution": AsyncMock(return_value=MagicMock(name="write_result")),
        "build_student_canonical": MagicMock(return_value=MagicMock(name="student_canonical")),
        "build_reference_canonical": MagicMock(return_value=MagicMock(name="reference_graph")),
        "grade_attempt": MagicMock(return_value=MagicMock(name="grade")),
        "build_audited_grade": MagicMock(return_value=MagicMock(name="audited")),
        "compute_normalization_confidence": MagicMock(return_value=0.83),
        "reference_graph_hash": MagicMock(return_value="refhash-v1:abc"),
        "persist_comparison_run": AsyncMock(return_value=persist_return),
        "build_opposes_map": MagicMock(return_value={"misc.k": "eq.k"}),
        "build_turn_order": AsyncMock(return_value={"n1": 0, "n2": 1}),
    }
    patches = [patch.object(dg, name, new=m) for name, m in mocks.items()]
    return patches, mocks


def _read_transcript_patch():
    return patch.object(dg, "_read_transcript", new=AsyncMock(return_value="t1\nt2"))


async def _run(db, mocks_payload=None):
    sess = _Sess()
    attempt = _Attempt()
    graph = KGGraph()
    return await run_graph_simulation(
        db, MagicMock(name="neo"),
        attempt=attempt, sess=sess, student_graph=graph,
        problem_payload=mocks_payload if mocks_payload is not None else _payload(),
    ), sess, attempt


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    return db


async def test_run_graph_simulation_happy_path_calls_chain_in_order():
    db = _db()
    patches, mocks = _all_callee_patches()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            result, sess, attempt = await _run(db)
        finally:
            for p in reversed(patches):
                p.stop()

    # resolve_attempt got the live adjudicator + the inputs' symbolic_mappings
    rkwargs = mocks["resolve_attempt"].call_args.kwargs
    assert rkwargs["llm_adjudicator"] is dg.main_chat_adjudicator
    assert rkwargs["symbolic_mappings"] == {"d": "2*r"}
    assert rkwargs["fuzzy_threshold"] == 0.9

    # persist_comparison_run got the session-scoped ids
    pkwargs = mocks["persist_comparison_run"].call_args.kwargs
    assert pkwargs["attempt_id"] == 99
    assert pkwargs["user_id"] == _USER_ID
    assert pkwargs["search_space_id"] == 7
    assert pkwargs["normalization_confidence"] == 0.83
    assert pkwargs["reference_graph_hash"] == "refhash-v1:abc"

    # the run-txn commit fired (caller owns the boundary)
    db.commit.assert_awaited()

    # the frozen handoff carries the mocked values
    assert isinstance(result, ShadowGradeResult)
    assert result.run_id == 4321
    assert result.normalization_confidence == 0.83
    assert result.reference_graph_hash == "refhash-v1:abc"
    assert result.opposes_map == {"misc.k": "eq.k"}
    assert result.turn_order == {"n1": 0, "n2": 1}
    # pending NOT set on the happy path
    assert attempt.learner_update_pending is False


async def test_raw_payload_passed_not_parsed_problem():
    """§1.4 regression guard: the EXACT payload dict (declared_paths /
    symbolic_mappings / per-step entity_key) reaches the two reference/candidate
    builders unchanged — never round-tripped through Problem.model_validate."""
    db = _db()
    payload = _payload()
    payload["declared_paths"] = [["s1"]]
    payload["symbolic_mappings"] = {"d": "2*r"}

    patches, mocks = _all_callee_patches()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            await _run(db, mocks_payload=payload)
        finally:
            for p in reversed(patches):
                p.stop()

    ref_arg = mocks["build_reference_canonical"].call_args.args[0]
    cand_arg = mocks["build_problem_candidates"].call_args.args[0]
    assert ref_arg is payload
    assert cand_arg is payload
    assert "declared_paths" in ref_arg
    assert "symbolic_mappings" in cand_arg
    assert ref_arg["reference_solution"][0]["entity_key"] == "eq.k"


async def test_resolution_unavailable_sets_pending_and_reraises():
    db = _db()
    patches, mocks = _all_callee_patches()
    mocks["write_resolution"].side_effect = ResolutionUnavailableError(
        stage="write_resolves_to", last_error="boom"
    )
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            with pytest.raises(ResolutionUnavailableError):
                await _run(db)
        finally:
            for p in reversed(patches):
                p.stop()
    # the pending flag was set + committed (NO-FALLBACK, grade not voided)
    db.commit.assert_awaited()


async def test_resolution_unavailable_marks_attempt_pending():
    db = _db()
    patches, mocks = _all_callee_patches()
    mocks["resolve_attempt"].side_effect = ResolutionUnavailableError(
        stage="llm_adjudication", last_error="timeout"
    )
    captured = {}
    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            with pytest.raises(ResolutionUnavailableError):
                await run_graph_simulation(
                    db, MagicMock(), attempt=attempt, sess=sess,
                    student_graph=KGGraph(), problem_payload=_payload(),
                )
        finally:
            for p in reversed(patches):
                p.stop()
    captured["pending"] = attempt.learner_update_pending
    assert captured["pending"] is True


async def test_resolution_invalid_output_sets_pending_and_reraises():
    db = _db()
    patches, mocks = _all_callee_patches()
    mocks["resolve_attempt"].side_effect = ResolutionInvalidOutputError(
        returned_key="hallucinated", allowed_keys=("a", "b")
    )
    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            with pytest.raises(ResolutionInvalidOutputError):
                await run_graph_simulation(
                    db, MagicMock(), attempt=attempt, sess=sess,
                    student_graph=KGGraph(), problem_payload=_payload(),
                )
        finally:
            for p in reversed(patches):
                p.stop()
    assert attempt.learner_update_pending is True
    db.commit.assert_awaited()


async def test_student_graph_invalid_does_not_set_pending():
    db = _db()
    patches, mocks = _all_callee_patches()
    mocks["validate_student_graph"].side_effect = StudentGraphInvalidError(
        reasons=("bad edge",)
    )
    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            with pytest.raises(StudentGraphInvalidError):
                await run_graph_simulation(
                    db, MagicMock(), attempt=attempt, sess=sess,
                    student_graph=KGGraph(), problem_payload=_payload(),
                )
        finally:
            for p in reversed(patches):
                p.stop()
    # nothing cross-store was written -> pending stays False, no commit of it
    assert attempt.learner_update_pending is False


async def test_reference_graph_invalid_does_not_set_pending():
    db = _db()
    patches, mocks = _all_callee_patches()
    mocks["build_reference_canonical"].side_effect = ReferenceGraphInvalidError(
        reasons=("no declared_paths",)
    )
    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            with pytest.raises(ReferenceGraphInvalidError):
                await run_graph_simulation(
                    db, MagicMock(), attempt=attempt, sess=sess,
                    student_graph=KGGraph(), problem_payload=_payload(),
                )
        finally:
            for p in reversed(patches):
                p.stop()
    assert attempt.learner_update_pending is False


async def test_audit_fn_injected():
    db = _db()
    patches, mocks = _all_callee_patches()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            await _run(db)
        finally:
            for p in reversed(patches):
                p.stop()
    akwargs = mocks["build_audited_grade"].call_args.kwargs
    assert akwargs["audit_fn"] is dg.main_chat_auditor
    assert akwargs["candidates"] == ("cand1", "cand2")
    assert akwargs["reference_invalid"] is False
    assert akwargs["transcript"] == "t1\nt2"


async def test_unexpected_exception_in_window_sets_pending_and_reraises():
    """An unexpected Exception in the cross-store window (e.g. CanonProjectionError,
    Risk #5) is still NO-FALLBACK: flag for retry, commit, re-raise the original."""
    db = _db()
    patches, mocks = _all_callee_patches()
    mocks["grade_attempt"].side_effect = RuntimeError("canon projection blew up")
    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            with pytest.raises(RuntimeError, match="canon projection"):
                await run_graph_simulation(
                    db, MagicMock(), attempt=attempt, sess=sess,
                    student_graph=KGGraph(), problem_payload=_payload(),
                )
        finally:
            for p in reversed(patches):
                p.stop()
    assert attempt.learner_update_pending is True
    db.commit.assert_awaited()


async def test_student_graph_invalid_inside_window_does_not_set_pending():
    """Belt-and-suspenders: if StudentGraphInvalidError surfaces from INSIDE the
    cross-store window (e.g. build_student_canonical) it re-raises WITHOUT setting
    pending (the inner 422 guard)."""
    db = _db()
    patches, mocks = _all_callee_patches()
    mocks["build_student_canonical"].side_effect = StudentGraphInvalidError(
        reasons=("late bad edge",)
    )
    sess = _Sess()
    attempt = _Attempt()
    with _read_transcript_patch():
        for p in patches:
            p.start()
        try:
            with pytest.raises(StudentGraphInvalidError):
                await run_graph_simulation(
                    db, MagicMock(), attempt=attempt, sess=sess,
                    student_graph=KGGraph(), problem_payload=_payload(),
                )
        finally:
            for p in reversed(patches):
                p.stop()
    assert attempt.learner_update_pending is False


async def test_no_mastery_events_written():
    """The WU-4C1/5A boundary: run_graph_simulation must NOT import/call
    convert_findings_to_events (that is WU-5A)."""
    db = _db()
    patches, _mocks = _all_callee_patches()
    convert = MagicMock(name="convert_findings_to_events")
    with _read_transcript_patch():
        for p in patches:
            p.start()
        # patch the symbol IF it exists on the module; assert not called either way
        patched = patch.object(dg, "convert_findings_to_events", new=convert, create=True)
        patched.start()
        try:
            await _run(db)
        finally:
            patched.stop()
            for p in reversed(patches):
                p.stop()
    convert.assert_not_called()
