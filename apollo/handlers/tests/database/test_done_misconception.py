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
from apollo.overseer.misconception_detector.types import (
    ConceptFinding,
    DetectionResult,
)

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
