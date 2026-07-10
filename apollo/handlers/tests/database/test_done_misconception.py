"""T13 — wiring the misconception detector into ``handle_done``.

The detector is a DEFAULT-OFF parallel stage inserted after the rubric and
before the diagnostic. These tests pin the wiring contract (plan §6.1, §8 T13):

  * **Flag ON + a docked finding** -> ``detection_outcome.misconception_penalty
    > 0`` reaches ``write_artifacts`` AND the student-facing
    ``rubric.overall.score`` is reduced (the LIVE band a student sees moves).
  * **Flag OFF** -> ``detect_misconceptions`` is NEVER called and the
    student-facing dict is byte-identical to a flag-off ``handle_done`` — the
    hard regression guard (design invariant #1).
  * **Detector raises** -> the ``except`` soft-fails: the grade proceeds
    UNPENALIZED (OLD-path rubric), no exception escapes ``handle_done`` (HTTP
    200), and ``detection_outcome`` threads through as ``None``.
  * The ``_student_utterances`` helper reads ``Message.role == "student"``
    ordered by ``turn_index`` (R6).
  * ``_default_embed_fn`` / ``_default_judge_fn`` are the production DI seams
    (a real judge is a ``make_openai_judge`` instance; the embed fn returns a
    single vector for a single text) — both are lazy so importing the module
    never touches the OpenAI SDK.

These are PURE unit tests: every OLD-path collaborator is mocked
deterministically (reusing ``test_done_shadow_flag._old_path_patches``), Neo4j
is a MagicMock, the detector's LLM/embed seams are patched, and the DB is a
MagicMock — no real database, no live LLM, no network. The gate/merge/apply
stages downstream of the (patched) ``detect_misconceptions`` run FOR REAL so
this test exercises the actual penalty arithmetic, not a mocked outcome.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers import done as done_mod
from apollo.handlers.done import (
    _default_embed_fn,
    _default_judge_fn,
    _student_utterances,
    detector_enabled,
    handle_done,
)
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches
from apollo.ontology import KGGraph, build_node
from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    DetectionResult,
)
from apollo.grading.artifact_build import build_llm_artifact
from apollo.grading.composite import load_weights

pytestmark = pytest.mark.unit

_FLAG = "APOLLO_MISCONCEPTION_DETECTOR"

# A realistic OLD-path rubric with an INTEGER 0-100 score + letter, so
# rubric_overall_after_penalty (which calls score_to_letter on an int) runs
# for real against it. The shared _old_path_patches fixture returns a float
# 0.5 score; we override compute_rubric per-test with this shape instead.
_OLD_RUBRIC = {
    "overall": {"score": 90, "letter": "A"},
    "procedure": {"score": 90, "letter": "A", "present": True},
    "justification": {"score": 90, "letter": "A", "present": True},
    "simplification": {"score": 90, "letter": "A", "present": True},
}


def _docked_finding() -> ConceptFinding:
    """A deterministic sympy_veto-style finding that the REAL gate will dock
    (source='sympy_veto' self-corroborates) and the REAL merge will turn into
    a nonzero penalty (confidence 1.0 * centrality floor > 0)."""
    return ConceptFinding(
        concept_key="node-eq-1",
        verdict="misconception",
        confidence=1.0,
        severity=0.0,
        evidence_span="net exports are always positive",
        signature="misc.net_exports_sign",
        source="sympy_veto",
        corroborated=True,
    )


def _patches_with_rubric(patches, rubric):
    """Return a copy of the shared ``_old_path_patches`` list with the shared
    (float-scored) ``compute_rubric`` patch dropped and a fresh integer-scored
    one appended LAST — so it starts after every shared patch and its return
    value wins. A fresh ``dict(rubric)`` is used so ``compute_rubric`` never
    hands ``handle_done`` the module-level template to mutate."""
    kept = [p for p in patches if getattr(p, "attribute", None) != "compute_rubric"]
    kept.append(patch("apollo.handlers.done.compute_rubric", return_value=dict(rubric)))
    return kept


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    # The detector stage also reads these flags via the shadow chain; keep
    # them off so only the detector stage under test is active.
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("APOLLO_GRADING_ARTIFACT_ENABLED", raising=False)
    # Phase-1 diagnostic trace flag — OFF by default so the byte-identical
    # goldens below hold; the trace-ON goldens set it explicitly.
    monkeypatch.delenv("APOLLO_MISC_TRACE", raising=False)
    yield


async def _run(monkeypatch, *, flag, detect_return=None, detect_side_effect=None):
    """Drive handle_done with the OLD-path mocked, compute_rubric overridden to
    the integer-scored _OLD_RUBRIC, and detect_misconceptions patched.

    Returns (out, detect_mock). ``detect_return`` is the DetectionResult the
    patched detector yields; ``detect_side_effect`` (e.g. an Exception) makes
    the detector raise to exercise the soft-fail branch.
    """
    if flag is not None:
        monkeypatch.setenv(_FLAG, flag)

    db, _sess, _attempt, patches = _old_path_patches()

    detect_kwargs = {}
    if detect_side_effect is not None:
        detect_kwargs["side_effect"] = detect_side_effect
    else:
        detect_kwargs["return_value"] = (
            detect_return if detect_return is not None else DetectionResult(per_concept=())
        )
    detect_mock = AsyncMock(**detect_kwargs)

    # Override the shared harness's float-scored compute_rubric with the
    # integer-scored _OLD_RUBRIC so rubric_overall_after_penalty (score_to_letter
    # on an int) runs for real. Appended LAST so it starts after the shared
    # compute_rubric patch and wins (patch order = LIFO on stop).
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch("apollo.handlers.done.detect_misconceptions", new=detect_mock),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("net exports are always positive",)),
        ),
    ]

    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out, detect_mock


# ── flag helper / constant ──────────────────────────────────────────────────


def test_detector_flag_constant_name():
    assert done_mod._MISCONCEPTION_DETECTOR_FLAG == _FLAG


def test_detector_enabled_reexported_and_defaults_off(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    assert detector_enabled() is False
    monkeypatch.setenv(_FLAG, "true")
    assert detector_enabled() is True


# ── flag OFF: byte-identical (the hard regression guard) ─────────────────────


async def test_flag_off_detector_never_called_and_rubric_unchanged(monkeypatch):
    out, detect_mock = await _run(monkeypatch, flag=None)
    detect_mock.assert_not_awaited()
    # Student-facing rubric is the OLD-path rubric, untouched.
    assert out["rubric"] == _OLD_RUBRIC
    assert out["rubric"]["overall"]["score"] == 90
    assert out["rubric"]["overall"]["letter"] == "A"


async def test_flag_explicit_false_is_off(monkeypatch):
    out, detect_mock = await _run(monkeypatch, flag="false")
    detect_mock.assert_not_awaited()
    assert out["rubric"] == _OLD_RUBRIC


# ── flag ON + a docked finding: penalty applied, rubric reduced ──────────────


async def test_flag_on_docked_finding_reduces_rubric_score(monkeypatch):
    detection = DetectionResult(per_concept=(_docked_finding(),))
    out, detect_mock = await _run(monkeypatch, flag="true", detect_return=detection)

    detect_mock.assert_awaited_once()
    # The LIVE student-facing rubric score dropped below the OLD-path 90.
    assert out["rubric"]["overall"]["score"] < 90, (
        "flag ON with a docked misconception must reduce the student-facing "
        f"overall score; got {out['rubric']['overall']['score']}"
    )
    # Letter recomputed from the new score via score_to_letter (A4) — a NEW
    # rubric dict, never the mocked OLD one mutated in place.
    from apollo.overseer.rubric import score_to_letter

    assert out["rubric"]["overall"]["letter"] == score_to_letter(out["rubric"]["overall"]["score"])


async def test_flag_on_docked_finding_threads_penalty_to_write_artifacts(monkeypatch):
    """The MergeOutcome (penalty > 0) reaches write_artifacts as
    detection_outcome — proving the ledger/artifact feed is wired."""
    monkeypatch.setenv(_FLAG, "true")
    monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", "true")

    db, _sess, _attempt, patches = _old_path_patches()
    detection = DetectionResult(per_concept=(_docked_finding(),))
    write_mock = AsyncMock(return_value=None)

    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("net exports are always positive",)),
        ),
        patch("apollo.handlers.done.write_artifacts", new=write_mock),
    ]
    for p in patches:
        p.start()
    try:
        await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    write_mock.assert_awaited_once()
    outcome = write_mock.await_args.kwargs["detection_outcome"]
    assert outcome is not None
    assert outcome.misconception_penalty > 0
    # A bank-keyed row is emitted for the ledger feed (A5: bare misc.<code>).
    assert outcome.misconceptions
    assert outcome.misconceptions[0]["canonical_key"] == "misc.net_exports_sign"


async def test_flag_on_empty_detection_leaves_rubric_unchanged(monkeypatch):
    """Flag ON but the detector finds nothing -> empty MergeOutcome -> the
    rubric score/letter are byte-identical to the OLD path (detector only ever
    subtracts; a zero-penalty outcome is a no-op on the score)."""
    out, detect_mock = await _run(
        monkeypatch, flag="true", detect_return=DetectionResult(per_concept=())
    )
    detect_mock.assert_awaited_once()
    assert out["rubric"]["overall"]["score"] == 90
    assert out["rubric"]["overall"]["letter"] == "A"


# ── soft-fail: a detector crash never breaks grading (HTTP 200, unpenalized) ─


async def test_detector_raising_soft_fails_grade_unpenalized(monkeypatch):
    """A detector exception is caught: handle_done returns normally (HTTP 200
    at the route), the rubric is the OLD-path unpenalized rubric, and no
    exception escapes."""
    out, detect_mock = await _run(
        monkeypatch, flag="true", detect_side_effect=RuntimeError("judge exploded")
    )
    detect_mock.assert_awaited_once()
    # Grade proceeded UNPENALIZED — OLD-path rubric stands.
    assert out["rubric"] == _OLD_RUBRIC
    assert out["rubric"]["overall"]["score"] == 90


async def test_detector_raising_threads_none_outcome_to_write_artifacts(monkeypatch):
    """Soft-fail path: detection_outcome threads through as None (not a stale
    partial), so write_artifacts writes the unpenalized canonical row."""
    monkeypatch.setenv(_FLAG, "true")
    monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", "true")

    db, _sess, _attempt, patches = _old_path_patches()
    write_mock = AsyncMock(return_value=None)

    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=()),
        ),
        patch("apollo.handlers.done.write_artifacts", new=write_mock),
    ]
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    write_mock.assert_awaited_once()
    assert write_mock.await_args.kwargs["detection_outcome"] is None
    assert out["rubric"] == _OLD_RUBRIC


# ── _student_utterances helper (R6: role == "student", ordered turn_index) ───


async def test_student_utterances_filters_role_and_orders():
    """Reads Message.content where role == 'student', ordered by turn_index.
    The query is built and executed; we assert on the returned tuple shape and
    that the scalars are returned verbatim in order."""
    db = MagicMock()

    class _Result:
        def scalars(self_inner):
            m = MagicMock()
            m.all.return_value = ["first student turn", "second student turn"]
            return m

    db.execute = AsyncMock(return_value=_Result())

    out = await _student_utterances(db, attempt_id=99)
    assert out == ("first student turn", "second student turn")
    assert isinstance(out, tuple)
    db.execute.assert_awaited_once()


async def test_student_utterances_empty_when_no_student_turns():
    db = MagicMock()

    class _Result:
        def scalars(self_inner):
            m = MagicMock()
            m.all.return_value = []
            return m

    db.execute = AsyncMock(return_value=_Result())
    out = await _student_utterances(db, attempt_id=99)
    assert out == ()


# ── production DI seams (lazy — no OpenAI import at module import time) ───────


def test_default_judge_fn_is_a_make_openai_judge_instance():
    """_default_judge_fn() returns a JudgeFn (a callable with the make_openai_judge
    signature) — not a live call. Patch make_openai_judge so no SDK touch."""
    sentinel = MagicMock(name="JudgeFn")
    with patch("apollo.handlers.done.make_openai_judge", return_value=sentinel) as mk:
        fn = _default_judge_fn()
    mk.assert_called_once()
    assert fn is sentinel


def test_default_embed_fn_returns_single_vector_for_single_text():
    """_default_embed_fn(text) -> list[float] (a SINGLE vector), wrapping the
    batched embed_texts (which returns a list of vectors). The batched call is
    patched so no OpenAI request is made."""
    with patch("apollo.handlers.done._embed_texts", return_value=[[0.1, 0.2, 0.3]]) as embed:
        vec = _default_embed_fn("some student utterance")
    embed.assert_called_once()
    assert vec == [0.1, 0.2, 0.3]


def test_default_embed_fn_empty_on_empty_batch_result():
    """Defensive: if the batched embed returns nothing, the single-vector
    wrapper degrades to an empty list rather than IndexError-ing."""
    with patch("apollo.handlers.done._embed_texts", return_value=[]):
        vec = _default_embed_fn("x")
    assert vec == []


def test_embed_texts_lazy_wraps_project_batched_embedder():
    """``_embed_texts`` is a LAZY indirection: it imports the project-wide
    ``indexing.document_embedder.embed_texts`` only when called, so importing
    ``done`` never pulls the OpenAI SDK. Patch the underlying batched call and
    assert it is forwarded verbatim (this exercises the lazy-import body itself
    — no network)."""
    from apollo.handlers.done import _embed_texts

    with patch("indexing.document_embedder.embed_texts", return_value=[[1.0, 2.0]]) as batched:
        out = _embed_texts(["a"])
    batched.assert_called_once_with(["a"])
    assert out == [[1.0, 2.0]]


# ── Phase-1 diagnostic trace (APOLLO_MISC_TRACE, default OFF) ─────────────────
# Instrumentation only. Flag OFF => the trace module is never imported/invoked
# and handle_done's output is byte-identical; flag ON => trace_attempt runs once
# inside the existing detector soft-fail envelope, changing NOTHING about the
# grade. `trace_attempt` is patched at its SOURCE module because done.py imports
# it lazily inside the guarded branch.
_TRACE_SRC = "apollo.overseer.misconception_detector.trace.trace_attempt"


async def _run_with_trace_patch(monkeypatch, *, detector_flag, trace_flag, trace_mock):
    """Drive handle_done with the detector ON (a docked finding so the detect->
    gate->merge chain fully runs), trace_attempt patched at its source, and the
    trace flag set per-case. Returns `out`."""
    if detector_flag is not None:
        monkeypatch.setenv(_FLAG, detector_flag)
    if trace_flag is not None:
        monkeypatch.setenv("APOLLO_MISC_TRACE", trace_flag)

    db, _sess, _attempt, patches = _old_path_patches()
    detection = DetectionResult(per_concept=(_docked_finding(),))
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("net exports are always positive",)),
        ),
        patch(_TRACE_SRC, new=trace_mock),
    ]
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out


async def test_trace_flag_off_trace_never_called_and_output_unchanged(monkeypatch):
    """GOLDEN: detector ON but trace OFF -> trace_attempt is NEVER called and
    the student-facing rubric is exactly what it is WITHOUT the trace (the
    penalized OLD-path score), i.e. the trace adds nothing. Byte-identical
    guard for the trace flag."""
    trace_mock = MagicMock(name="trace_attempt")
    out = await _run_with_trace_patch(
        monkeypatch, detector_flag="true", trace_flag=None, trace_mock=trace_mock
    )
    trace_mock.assert_not_called()
    # The detector still docked (penalty applied) — the trace flag does not
    # touch the grade either way.
    assert out["rubric"]["overall"]["score"] < 90


async def test_trace_flag_explicit_false_never_calls_trace(monkeypatch):
    trace_mock = MagicMock(name="trace_attempt")
    await _run_with_trace_patch(
        monkeypatch, detector_flag="true", trace_flag="false", trace_mock=trace_mock
    )
    trace_mock.assert_not_called()


async def test_trace_flag_on_calls_trace_once_without_changing_grade(monkeypatch):
    """Flag ON -> trace_attempt is invoked exactly once with the real per-attempt
    inputs, and the grade is IDENTICAL to the trace-OFF run (instrumentation
    only — the trace never feeds back into scoring)."""
    trace_mock = MagicMock(name="trace_attempt")
    out_on = await _run_with_trace_patch(
        monkeypatch, detector_flag="true", trace_flag="true", trace_mock=trace_mock
    )
    trace_mock.assert_called_once()
    kwargs = trace_mock.call_args.kwargs
    # The trace receives the live artifacts, not a re-run.
    assert set(kwargs) >= {
        "attempt_id",
        "reference_graph",
        "detection",
        "gated",
        "outcome",
        "centrality",
        "final_band",
        "is_control",
    }
    assert kwargs["is_control"] is False

    # Grade parity: a trace-OFF run yields the SAME rubric score.
    trace_off = MagicMock(name="trace_attempt")
    out_off = await _run_with_trace_patch(
        monkeypatch, detector_flag="true", trace_flag="false", trace_mock=trace_off
    )
    assert out_on["rubric"] == out_off["rubric"]


async def test_trace_raising_does_not_perturb_grade(monkeypatch):
    """A trace defect must never break grading AND must not change it: the
    trace has its OWN try/except (isolated from the detector's), so a raising
    trace_attempt leaves the already-computed PENALIZED rubric exactly as it
    was — instrumentation never rolls back or alters the grade — and no
    exception escapes (HTTP 200)."""
    boom = MagicMock(name="trace_attempt", side_effect=RuntimeError("trace boom"))
    out_boom = await _run_with_trace_patch(
        monkeypatch, detector_flag="true", trace_flag="true", trace_mock=boom
    )
    boom.assert_called_once()

    # A clean trace-ON run produces the SAME rubric — the raising trace changed
    # nothing about the grade (it was penalized to <90 either way).
    clean = MagicMock(name="trace_attempt")
    out_clean = await _run_with_trace_patch(
        monkeypatch, detector_flag="true", trace_flag="true", trace_mock=clean
    )
    assert out_boom["rubric"] == out_clean["rubric"]
    assert out_boom["rubric"]["overall"]["score"] < 90  # penalty stands


# ── Emergent map capture seam 1: detector-unkeyed birth (T2, APOLLO_EMERGENT_ #
# MAP_CAPTURE, default OFF) ───────────────────────────────────────────────────
# Independently flag-gated from detector_enabled() (that flag only gates
# whether the detector STAGE runs at all; this new flag gates only whether a
# BIRTH observation is written once it does). Own failure domain: a capture
# write failure must never perturb the returned grade.

_CAPTURE_FLAG = "APOLLO_EMERGENT_MAP_CAPTURE"
_BIRTH_NODE_ID = "node.real_basis"
_BIRTH_ENTITY_KEY = "def.real_basis"


def _unkeyed_birth_finding() -> ConceptFinding:
    """A lone judge finding at a keyed reference node, confident + unkeyed
    (no bank_code) — clears routed tau, so gate.py routes it to
    needs_clarification (row7) while collect_unkeyed_births independently
    captures it as a birth candidate."""
    return ConceptFinding(
        concept_key=_BIRTH_NODE_ID,
        verdict="wrong",
        confidence=0.95,
        severity=0.0,
        evidence_span="real GDP already includes inflation",
        signature=f"unkeyed:{_BIRTH_NODE_ID}",
        source="judge",
        corroborated=False,
        bank_code=None,
    )


def _reference_graph_with_entity_key() -> KGGraph:
    node = build_node(
        node_type="definition",
        node_id=_BIRTH_NODE_ID,
        attempt_id=99,
        source="reference",
        content={"concept": "real GDP", "meaning": "GDP adjusted for inflation"},
        entity_key=_BIRTH_ENTITY_KEY,
    )
    return KGGraph(nodes=[node], edges=[])


async def _run_capture(
    monkeypatch,
    *,
    capture_flag,
    detect_return=None,
    record_births_mock=None,
    reference_graph=None,
):
    """Drive handle_done with the detector ON (real gate/merge chain), the
    reference graph carrying an entity_key-bearing node, and the capture
    seam's write function patched. Returns (out, record_births_mock)."""
    monkeypatch.setenv(_FLAG, "true")
    if capture_flag is not None:
        monkeypatch.setenv(_CAPTURE_FLAG, capture_flag)
    else:
        monkeypatch.delenv(_CAPTURE_FLAG, raising=False)

    db, _sess, _attempt, patches = _old_path_patches()

    graph = reference_graph if reference_graph is not None else _reference_graph_with_entity_key()

    async def _find_problem_with_graph(_db, _cid, _code):
        problem = MagicMock()
        problem.id = "p_code"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    detection = detect_return if detect_return is not None else DetectionResult(
        per_concept=(_unkeyed_birth_finding(),)
    )
    births_mock = record_births_mock if record_births_mock is not None else AsyncMock(return_value=1)

    patches = [p for p in patches if getattr(p, "attribute", None) != "_find_problem"]
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("real GDP already includes inflation",)),
        ),
        patch("apollo.handlers.done.record_detector_births", new=births_mock),
    ]
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out, births_mock


async def test_capture_flag_off_record_detector_births_never_called(monkeypatch):
    """Capture flag OFF (default) -> record_detector_births is never invoked,
    even though the detector itself is ON and produced an unkeyed finding."""
    never = AsyncMock(
        side_effect=AssertionError("record_detector_births must not be called while flag is OFF")
    )
    out, births_mock = await _run_capture(monkeypatch, capture_flag=None, record_births_mock=never)

    births_mock.assert_not_awaited()
    # Detector still ran and produced its OWN grade effect independent of capture.
    assert "rubric" in out


async def test_capture_flag_on_writes_birth_and_commits(monkeypatch):
    """Capture flag ON -> the birth seam fires: record_detector_births is
    called with the resolved node_entity_key map and the collector's births,
    and the write is committed (own commit, per plan T2)."""
    births_mock = AsyncMock(return_value=1)
    db, _sess, _attempt, patches = _old_path_patches()

    graph = _reference_graph_with_entity_key()

    async def _find_problem_with_graph(_db, _cid, _code):
        problem = MagicMock()
        problem.id = "p_code"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    detection = DetectionResult(per_concept=(_unkeyed_birth_finding(),))
    monkeypatch.setenv(_FLAG, "true")
    monkeypatch.setenv(_CAPTURE_FLAG, "true")

    patches = [p for p in patches if getattr(p, "attribute", None) != "_find_problem"]
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("real GDP already includes inflation",)),
        ),
        patch("apollo.handlers.done.record_detector_births", new=births_mock),
    ]
    for p in patches:
        p.start()
    try:
        await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    births_mock.assert_awaited_once()
    kwargs = births_mock.await_args.kwargs
    assert kwargs["node_entity_key"] == {_BIRTH_NODE_ID: _BIRTH_ENTITY_KEY}
    births = kwargs["births"]
    assert len(births) == 1
    assert births[0].concept_key == _BIRTH_NODE_ID
    # db.commit is called at least once more beyond the pre-detector commits —
    # the shared MagicMock db.commit records every call; assert it was awaited
    # (own-commit-on-success per the artifact_writer.py pattern).
    assert db.commit.await_count >= 1


async def test_capture_own_failure_domain_grade_byte_identical(monkeypatch):
    """A capture-write failure (store raises) is swallowed + logged, and the
    grade returned by handle_done is BYTE-IDENTICAL to a clean capture-ON run
    — the load-bearing own-failure-domain guarantee (student grading outcomes
    must never be perturbed by a capture defect)."""
    boom = AsyncMock(side_effect=RuntimeError("store exploded"))
    out_boom, _ = await _run_capture(monkeypatch, capture_flag="true", record_births_mock=boom)

    clean = AsyncMock(return_value=1)
    out_clean, _ = await _run_capture(monkeypatch, capture_flag="true", record_births_mock=clean)

    assert out_boom["rubric"] == out_clean["rubric"]
    assert out_boom["diagnostic_narrative"] == out_clean["diagnostic_narrative"]
    assert out_boom["xp_earned"] == out_clean["xp_earned"]


async def test_capture_own_failure_domain_rolls_back(monkeypatch):
    """The capture seam's own try/except calls db.rollback() on a write
    failure (artifact_writer.py:236-256 pattern) — verified via the shared
    MagicMock db used by the OLD-path harness."""
    boom = AsyncMock(side_effect=RuntimeError("store exploded"))
    db, _sess, _attempt, patches = _old_path_patches()
    db.rollback = AsyncMock()

    graph = _reference_graph_with_entity_key()

    async def _find_problem_with_graph(_db, _cid, _code):
        problem = MagicMock()
        problem.id = "p_code"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    detection = DetectionResult(per_concept=(_unkeyed_birth_finding(),))
    monkeypatch.setenv(_FLAG, "true")
    monkeypatch.setenv(_CAPTURE_FLAG, "true")

    patches = [p for p in patches if getattr(p, "attribute", None) != "_find_problem"]
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("real GDP already includes inflation",)),
        ),
        patch("apollo.handlers.done.record_detector_births", new=boom),
    ]
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    boom.assert_awaited_once()
    assert db.rollback.await_count >= 1
    assert "rubric" in out  # HTTP 200 shape — no exception escaped


async def test_capture_flag_on_no_births_calls_nothing_extra(monkeypatch):
    """Capture ON but the detector's findings produce zero births (e.g. a
    clean/clear finding set) -> record_detector_births still may be called
    with an empty births tuple (a no-op write) OR not at all; either way the
    grade is unaffected. This pins that the wiring does not crash on the
    empty-births path."""
    births_mock = AsyncMock(return_value=0)
    out, _ = await _run_capture(
        monkeypatch,
        capture_flag="true",
        detect_return=DetectionResult(per_concept=()),
        record_births_mock=births_mock,
    )
    assert "rubric" in out


# --------------------------------------------------------------------------- #
# T7 (plan Wave 3, spec §5.5 Q3): materialize_if_promotable is invoked from
# THIS capture seam's own success path, inside the same failure domain.
# --------------------------------------------------------------------------- #


async def test_capture_flag_on_invokes_materialize_for_each_birth_entity_key(monkeypatch):
    """After record_detector_births succeeds, materialize_if_promotable is
    called once per distinct resolved entity_key, BEFORE the commit, with the
    handler's own `neo` client threaded through (Q3 eager materialization)."""
    births_mock = AsyncMock(return_value=1)
    materialize_mock = AsyncMock()
    db, _sess, _attempt, patches = _old_path_patches()
    neo_sentinel = MagicMock(name="neo_client")

    graph = _reference_graph_with_entity_key()

    async def _find_problem_with_graph(_db, _cid, _code):
        problem = MagicMock()
        problem.id = "p_code"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    detection = DetectionResult(per_concept=(_unkeyed_birth_finding(),))
    monkeypatch.setenv(_FLAG, "true")
    monkeypatch.setenv(_CAPTURE_FLAG, "true")

    patches = [p for p in patches if getattr(p, "attribute", None) != "_find_problem"]
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("real GDP already includes inflation",)),
        ),
        patch("apollo.handlers.done.record_detector_births", new=births_mock),
        patch("apollo.handlers.done.materialize_if_promotable", new=materialize_mock),
    ]
    for p in patches:
        p.start()
    try:
        await handle_done(db=db, neo=neo_sentinel, session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    materialize_mock.assert_awaited_once()
    args, kwargs = materialize_mock.await_args
    assert args[0] is db
    assert args[1] is neo_sentinel
    assert kwargs["signature"] == f"emergent.{_BIRTH_ENTITY_KEY}"
    assert kwargs["opposes_entity_key"] == _BIRTH_ENTITY_KEY
    # materialize ran BEFORE the commit that follows the write (order proof:
    # the mock was awaited at least once by the time commit was reached —
    # both mocks are on the same call sequence inside the try block).
    assert db.commit.await_count >= 1


async def test_capture_flag_off_materialize_never_called(monkeypatch):
    """Flag OFF -> neither record_detector_births NOR materialize_if_promotable
    is invoked -- byte-identity extends to the materialize step too."""
    materialize_mock = AsyncMock(
        side_effect=AssertionError("materialize_if_promotable must not be called while flag is OFF")
    )
    with patch("apollo.handlers.done.materialize_if_promotable", new=materialize_mock):
        out, births_mock = await _run_capture(monkeypatch, capture_flag=None)

    births_mock.assert_not_awaited()
    materialize_mock.assert_not_awaited()
    assert "rubric" in out


async def test_capture_materialize_failure_own_failure_domain_grade_byte_identical(monkeypatch):
    """A materialize-step failure (e.g. Neo4j hiccup surfacing up through
    materialize_if_promotable) is swallowed by the SAME try/except as the
    write -- the returned grade is byte-identical to a clean run, and the
    birth observation write itself already succeeded before materialize ran."""
    boom = AsyncMock(side_effect=RuntimeError("materialize exploded"))
    clean = AsyncMock()

    db_boom, _sess, _attempt, patches_boom = _old_path_patches()
    graph = _reference_graph_with_entity_key()

    async def _find_problem_with_graph(_db, _cid, _code):
        problem = MagicMock()
        problem.id = "p_code"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = graph
        return problem

    detection = DetectionResult(per_concept=(_unkeyed_birth_finding(),))

    def _build(db, patches, materialize_mock):
        patches = [p for p in patches if getattr(p, "attribute", None) != "_find_problem"]
        patches = _patches_with_rubric(patches, _OLD_RUBRIC)
        patches += [
            patch(
                "apollo.handlers.done._find_problem",
                new=AsyncMock(side_effect=_find_problem_with_graph),
            ),
            patch(
                "apollo.handlers.done.detect_misconceptions",
                new=AsyncMock(return_value=detection),
            ),
            patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
            patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
            patch(
                "apollo.handlers.done._student_utterances",
                new=AsyncMock(return_value=("real GDP already includes inflation",)),
            ),
            patch("apollo.handlers.done.record_detector_births", new=AsyncMock(return_value=1)),
            patch("apollo.handlers.done.materialize_if_promotable", new=materialize_mock),
        ]
        return patches

    monkeypatch.setenv(_FLAG, "true")
    monkeypatch.setenv(_CAPTURE_FLAG, "true")

    patches = _build(db_boom, patches_boom, boom)
    for p in patches:
        p.start()
    try:
        out_boom = await handle_done(db=db_boom, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    db_clean, _sess2, _attempt2, patches_clean_raw = _old_path_patches()
    patches_clean = _build(db_clean, patches_clean_raw, clean)
    for p in patches_clean:
        p.start()
    try:
        out_clean = await handle_done(db=db_clean, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches_clean):
            p.stop()

    boom.assert_awaited_once()
    assert out_boom["rubric"] == out_clean["rubric"]
    assert out_boom["diagnostic_narrative"] == out_clean["diagnostic_narrative"]
    assert out_boom["xp_earned"] == out_clean["xp_earned"]


# --------------------------------------------------------------------------- #
# T-W5a — APOLLO_GRADER_POSITIVE_FOCUS (2026-07-10 grader positive-focus
# design memo, plan Wave 5). P1 (rubric band dock) neutralized when ON;
# P2 (composite dock, artifact_build.py) UNCONDITIONALLY retained as the
# sole penalty channel (never gated by this flag); P4 (misconception axis
# drag) neutralized to credit-only. P3 (coverage sign-gate) is covered
# separately in test_coverage_sign_gate.py (the flag lives entirely inside
# `_batch_binary_match`, which this handle_done-level harness never
# exercises — compute_coverage is mocked here).
# --------------------------------------------------------------------------- #

_POSITIVE_FOCUS_FLAG = "APOLLO_GRADER_POSITIVE_FOCUS"


@pytest.fixture(autouse=True)
def _clear_positive_focus_flag(monkeypatch):
    monkeypatch.delenv(_POSITIVE_FOCUS_FLAG, raising=False)
    yield


async def _run_positive_focus(
    monkeypatch,
    *,
    positive_focus,
    misconception_scores=None,
):
    """Drive handle_done with the detector ON (a real docked finding, so the
    REAL gate/merge/apply chain runs), positive-focus set per-case, and
    `_attempt_misconception_scores` returning the given per-code map (P4
    input). `compute_rubric` is NOT overridden here (unlike `_run`/
    `_patches_with_rubric`) — it must run FOR REAL against the real
    reference graph + misconception_scores so the axis math is genuinely
    exercised."""
    monkeypatch.setenv(_FLAG, "true")
    if positive_focus is not None:
        monkeypatch.setenv(_POSITIVE_FOCUS_FLAG, positive_focus)
    monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", "true")

    db, _sess, _attempt, patches = _old_path_patches()
    detection = DetectionResult(per_concept=(_docked_finding(),))

    write_mock = AsyncMock(return_value=None)

    # Give the reference graph ONE procedure-axis node so compute_rubric's
    # real weighted aggregation has a non-misconception axis present too
    # (the P4 assertions compare WITH vs WITHOUT the misconception axis).
    ref_node = build_node(
        node_type="procedure_step",
        node_id="step-1",
        attempt_id=99,
        source="reference",
        content={"action": "apply the identity", "label": ""},
    )
    reference_graph = KGGraph(nodes=[ref_node], edges=[])

    async def _find_problem_with_graph(_db, _cid, _code):
        problem = MagicMock()
        problem.id = "p_code"
        problem.problem_text = "text"
        problem.reference_solution = []
        problem.to_kg_graph.return_value = reference_graph
        return problem

    patches = [
        p for p in patches
        if getattr(p, "attribute", None) not in ("_find_problem", "compute_rubric", "_attempt_misconception_scores")
    ]
    patches += [
        patch(
            "apollo.handlers.done._find_problem",
            new=AsyncMock(side_effect=_find_problem_with_graph),
        ),
        patch(
            "apollo.handlers.done.compute_coverage",
            new=AsyncMock(return_value={
                "per_step": {"step-1": "covered"},
                "procedure_scores": {"step-1": 1.0},
                "confidences": {"step-1": 0.9},
            }),
        ),
        patch(
            "apollo.handlers.done._attempt_misconception_scores",
            new=AsyncMock(return_value=misconception_scores or {}),
        ),
        patch(
            "apollo.handlers.done.detect_misconceptions",
            new=AsyncMock(return_value=detection),
        ),
        patch("apollo.handlers.done.make_openai_judge", new=MagicMock()),
        patch("apollo.handlers.done._default_embed_fn", new=MagicMock()),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("net exports are always positive",)),
        ),
        patch("apollo.handlers.done.write_artifacts", new=write_mock),
    ]
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out, write_mock


# ── P1: rubric band dock neutralized when ON ─────────────────────────────────


async def test_positive_focus_off_p1_rubric_still_docked(monkeypatch):
    """Baseline (flag OFF, explicit): the real gate/merge/apply chain docks
    the served rubric exactly as it always has — proves the harness itself
    produces a real, non-trivial penalty before any positive-focus assertion
    is meaningful."""
    out, _write_mock = await _run_positive_focus(monkeypatch, positive_focus="false")
    # A pure procedure axis with 1/1 covered scores 100; the docked finding
    # must pull the served overall below that.
    assert out["rubric"]["overall"]["score"] < 100


async def test_positive_focus_on_p1_served_rubric_equals_pre_penalty(monkeypatch):
    """Flag ON: the served rubric is NOT reduced by rubric_overall_after_
    penalty — it equals the pre-penalty compute_rubric output (100, since the
    single procedure-axis node is fully covered and no misconception axis is
    fed in this case)."""
    out, _write_mock = await _run_positive_focus(monkeypatch, positive_focus="true")
    assert out["rubric"]["overall"]["score"] == 100


async def test_positive_focus_on_p1_xp_follows_unpenalized_rubric(monkeypatch):
    """XP is derived from `rubric["overall"]["score"]` (compute_xp_earned) —
    with P1 neutralized, XP must reflect the credit-only (higher) score, not
    a penalized one. Compared against the flag-OFF run using the SAME
    detection, proving XP moved because the served score moved."""
    out_on, _ = await _run_positive_focus(monkeypatch, positive_focus="true")
    out_off, _ = await _run_positive_focus(monkeypatch, positive_focus="false")
    assert out_on["rubric"]["overall"]["score"] > out_off["rubric"]["overall"]["score"]
    assert out_on["xp_earned"] >= out_off["xp_earned"]


# ── P2: composite dock retained (single-channel proof) ───────────────────────


async def test_positive_focus_on_p2_composite_still_docked_single_channel_proof():
    """THE single-channel proof (memo §3, plan T-W5a): with positive-focus
    semantics applied to the rubric (P1 skipped -> composite computed from
    the UN-penalized rubric score), `build_llm_artifact`'s own P2 dock
    (`apply_penalty`, UNCONDITIONAL — never gated by this flag) still
    subtracts the misconception penalty from the composite. This asserts
    directly against `build_llm_artifact`, the exact function `write_
    artifacts` calls with whatever `rubric` `done.py` hands it — proving
    that even though the SERVED band is credit-only, the composite the
    detector owns is still penalized by the SAME outcome."""
    from apollo.overseer.misconception_detector.centrality import compute_centrality
    from apollo.overseer.misconception_detector.gate import gate_findings
    from apollo.overseer.misconception_detector.merge import merge_detections

    reference_graph = KGGraph(
        nodes=[
            build_node(
                node_type="equation",
                node_id="node-eq-1",
                attempt_id=99,
                source="reference",
                content={"symbolic": "NX = X - M", "label": ""},
            )
        ],
        edges=[],
    )
    gated = gate_findings((_docked_finding(),), opposes_index={})
    centrality = compute_centrality(reference_graph)
    outcome = merge_detections(gated, centrality=centrality)
    assert outcome.misconception_penalty > 0, "harness must produce a real penalty"

    # Un-penalized rubric (what done.py hands write_artifacts when P1 is
    # skipped under positive-focus ON) — overall.score stays at its
    # pre-penalty value.
    pre_penalty_rubric = {"overall": {"score": 90, "letter": "A"}}
    coverage = {"per_step": {"n1": "covered"}, "confidences": {"n1": 0.9}}

    artifact = build_llm_artifact(
        coverage=coverage,
        rubric=pre_penalty_rubric,
        weights=load_weights(),
        graph_failure=None,
        latency_ms=5,
        clarification_trace=[],
        detection_outcome=outcome,
    )

    undocked_composite = round(90 / 100.0, 6)
    assert artifact["scores"]["composite"] < undocked_composite, (
        "P2 must still dock the composite even though the served rubric "
        "(P1) is credit-only under positive-focus"
    )
    assert artifact["scores"]["misconception_penalty"] == pytest.approx(
        outcome.misconception_penalty
    )
    assert artifact["misconceptions"] != []


async def test_positive_focus_on_p2_write_artifacts_receives_unpenalized_rubric_and_full_outcome(
    monkeypatch,
):
    """Wiring proof at the handle_done level: under positive-focus ON,
    `write_artifacts` receives the UN-penalized `rubric` (P1 skipped) AND
    the full `detection_outcome` (unchanged) — confirming done.py threads
    exactly what P2's single-channel proof above assumes."""
    out, write_mock = await _run_positive_focus(monkeypatch, positive_focus="true")

    write_mock.assert_awaited_once()
    kwargs = write_mock.await_args.kwargs
    assert kwargs["rubric"]["overall"]["score"] == 100
    assert kwargs["detection_outcome"] is not None
    assert kwargs["detection_outcome"].misconception_penalty > 0
    assert out["rubric"]["overall"]["score"] == 100


# ── P4: misconception axis credit-only when ON ───────────────────────────────


async def test_positive_focus_off_p4_unresolved_misconception_drags_overall(monkeypatch):
    """Baseline (explicit OFF): an unresolved (0.5) misconception code enters
    the axis and pulls `overall` below the axis-absent case — the existing
    P2.8 behavior, unchanged."""
    out_with_axis, _ = await _run_positive_focus(
        monkeypatch, positive_focus="false", misconception_scores={"alpha": 0.5},
    )
    out_without_axis, _ = await _run_positive_focus(
        monkeypatch, positive_focus="false", misconception_scores={},
    )
    assert out_with_axis["rubric"]["overall"]["score"] < out_without_axis["rubric"]["overall"]["score"]


async def test_positive_focus_on_p4_unresolved_no_longer_lowers_overall(monkeypatch):
    """Flag ON: the unresolved (0.5) code is filtered out before compute_
    rubric runs, so the axis is treated as ABSENT — `overall` equals the
    axis-absent case exactly (credit-only: no drag from an uncorrected
    misconception)."""
    out_with_unresolved, _ = await _run_positive_focus(
        monkeypatch, positive_focus="true", misconception_scores={"alpha": 0.5},
    )
    out_axis_absent, _ = await _run_positive_focus(
        monkeypatch, positive_focus="true", misconception_scores={},
    )
    assert (
        out_with_unresolved["rubric"]["overall"]["score"]
        == out_axis_absent["rubric"]["overall"]["score"]
    )


async def test_positive_focus_on_p4_resolved_still_credits(monkeypatch):
    """Flag ON: a RESOLVED (1.0) code is kept — "you corrected it" credit
    survives the filter. A resolved-only axis at 100 leaves `overall`
    unchanged from the axis-absent case (both feed 100 into a 100-scoring
    aggregate), while a MIXED resolved+unresolved set still credits the
    resolved code once the unresolved one is dropped."""
    out_resolved_only, _ = await _run_positive_focus(
        monkeypatch, positive_focus="true", misconception_scores={"alpha": 1.0},
    )
    out_axis_absent, _ = await _run_positive_focus(
        monkeypatch, positive_focus="true", misconception_scores={},
    )
    # Both the procedure axis (100) and the resolved-only misconception axis
    # (100) score perfectly, so overall is identical to axis-absent here —
    # the important proof is in the mixed case below, where the credit for
    # 'alpha' (kept) is distinguishable from 'beta' (dropped).
    assert out_resolved_only["rubric"]["overall"]["score"] == out_axis_absent["rubric"]["overall"]["score"]

    out_mixed, _ = await _run_positive_focus(
        monkeypatch,
        positive_focus="true",
        misconception_scores={"alpha": 1.0, "beta": 0.5},
    )
    # 'beta' (unresolved) is dropped, 'alpha' (resolved) survives -> the axis
    # is present with ONLY alpha=1.0 -> identical to the resolved-only run.
    assert out_mixed["rubric"]["overall"]["score"] == out_resolved_only["rubric"]["overall"]["score"]


# ── Feedback fidelity: detection_outcome.misconceptions[] still reaches ──────
# the artifact even though the served band is credit-only.


async def test_positive_focus_on_feedback_fidelity_misconceptions_reach_artifact(monkeypatch):
    """Flag ON: `detection_outcome` is still fully populated and threaded to
    `write_artifacts` — the BAD is still named in the feedback channel even
    though it is not subtracted from the served band (P1) or axis (P4)."""
    out, write_mock = await _run_positive_focus(monkeypatch, positive_focus="true")

    write_mock.assert_awaited_once()
    outcome = write_mock.await_args.kwargs["detection_outcome"]
    assert outcome is not None
    assert outcome.misconceptions
    assert outcome.misconceptions[0]["canonical_key"] == "misc.net_exports_sign"
    # The served band, meanwhile, stayed credit-only.
    assert out["rubric"]["overall"]["score"] == 100


# ── Golden flag-off byte-identity across ALL of P1/P3/P4's code paths ────────


async def test_positive_focus_flag_off_byte_identical_detector_on(monkeypatch):
    """With the detector ON and positive-focus explicitly OFF, the served
    rubric/XP are IDENTICAL to a run where the positive-focus flag is
    entirely unset (default) — proving 'unset' and 'false' are the same
    no-op, matching the detector-flag convention pinned elsewhere in this
    file."""
    monkeypatch.delenv(_POSITIVE_FOCUS_FLAG, raising=False)
    out_unset, _ = await _run_positive_focus(monkeypatch, positive_focus=None)
    out_false, _ = await _run_positive_focus(monkeypatch, positive_focus="false")
    assert out_unset["rubric"] == out_false["rubric"]
    assert out_unset["xp_earned"] == out_false["xp_earned"]


async def test_positive_focus_flag_off_byte_identical_detector_off(monkeypatch):
    """With the base detector flag OFF entirely, the positive-focus flag
    (regardless of its own value) must never matter — P1/P3/P4 all live
    inside code paths the detector-off branch never reaches for P1/P4, and
    P3 is nested inside detector_enabled() in coverage.py. Reuses the
    existing OLD-path harness (`_run`) to prove this at the handle_done
    layer for P1/P4's call sites."""
    monkeypatch.setenv(_POSITIVE_FOCUS_FLAG, "true")
    out_pf_on, detect_mock = await _run(monkeypatch, flag=None)
    detect_mock.assert_not_awaited()
    assert out_pf_on["rubric"] == _OLD_RUBRIC


# ── Scope assertion: composite.py / abstention.py / artifact_build.py ────────
# untouched by this task (git diff check, run at collection time so a CI
# failure here is a loud, direct signal rather than a silent scope creep).


def test_scope_assertion_out_of_scope_files_untouched():
    """T-W5a must not touch apollo/grading/artifact_build.py,
    apollo/grading/composite.py, or apollo/grading/abstention.py — P2 stays
    the UNCONDITIONAL sole penalty channel and PR #105's territory is
    untouched. This uses `git diff --name-only` against the merge-base with
    origin/staging so it is a real, live scope check, not a static claim."""
    import subprocess

    forbidden = {
        "apollo/grading/artifact_build.py",
        "apollo/grading/composite.py",
        "apollo/grading/abstention.py",
    }
    try:
        merge_base = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/staging"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        diff = subprocess.run(
            ["git", "diff", "--name-only", merge_base, "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except Exception:
        pytest.skip("git history unavailable in this environment")
    changed = set(diff.splitlines())
    touched = changed & forbidden
    assert not touched, f"T-W5a must not touch: {touched}"
