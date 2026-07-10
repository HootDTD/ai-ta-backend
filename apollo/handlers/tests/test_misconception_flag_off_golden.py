"""T15 — flag-OFF byte-identical regression suite (the hard regression guard).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 7 (T15), section 8's T15 RED assertion, design invariant #1 (repeated
throughout T1-T14's docstrings): while ``APOLLO_MISCONCEPTION_DETECTOR`` is
OFF (the only prod state today), ``handle_done`` and ``build_llm_artifact``
must produce output byte-identical to the pre-detector behavior — penalty
0.0, ``misconceptions: []``, composite unchanged, and (at the ``handle_done``
layer) ``detect_misconceptions`` never even invoked.

This suite locks that guarantee at BOTH layers, across BOTH a strong-control
persona and a misconception-labeled persona (real fixtures per A2 —
``campaign/cast/personas/macroeconomics/strong__gdp_identity.json`` and
``misconception__gdp_identity.json``). The persona's own `expected` block is
irrelevant here — the entire point of the flag-OFF guard is that the
detector never runs, so the CONTENT of what the student said (correct or a
genuine misconception) must have ZERO effect on the composite/penalty/
misconceptions fields. If a captured golden ever drifts, it means the
default-OFF flag stopped being a true no-op — a real regression.

Pure/offline: the ``build_llm_artifact`` half takes no IO at all. The
``handle_done`` half reuses the exact mocked OLD-path harness from
``test_done_shadow_flag._old_path_patches`` (MagicMock DB, Neo4j, and every
OLD-path collaborator patched) — no real database, no live LLM, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from apollo.grading.artifact_build import build_llm_artifact
from apollo.grading.composite import load_weights
from apollo.handlers.done import handle_done
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches

pytestmark = pytest.mark.unit

_FLAG = "APOLLO_MISCONCEPTION_DETECTOR"
_PERSONAS_DIR = (
    Path(__file__).resolve().parents[3] / "campaign" / "cast" / "personas" / "macroeconomics"
)


def _load_persona(name: str) -> dict:
    """Real persona fixture (A2 — no synthetic/invented persona names)."""
    return json.loads((_PERSONAS_DIR / name).read_text(encoding="utf-8"))


def _persona_utterance(persona: dict) -> str:
    """The persona's own scripted content, used as the ``student_utterances``
    stand-in for the ``handle_done``-layer golden. Flag OFF means this text
    (correct beats for the strong persona, a genuine wrong belief for the
    misconception persona) must never reach any grading math."""
    beats = persona["scripted_beats"]
    return beats[-1]


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("APOLLO_GRADING_ARTIFACT_ENABLED", raising=False)
    yield


# --------------------------------------------------------------------------- #
# Layer 1: build_llm_artifact — pure, no detection_outcome threaded (the
# flag-OFF caller shape: done.py never constructs a MergeOutcome when the
# flag is off, so this builder is always called with the default None).
# --------------------------------------------------------------------------- #

_COVERAGE = {
    "per_step": {"k1": "covered", "k2": "missing"},
    "confidences": {"k1": 0.9, "k2": 0.0},
}
_RUBRIC = {"overall": {"score": 71}}


def _artifact_kwargs(**overrides) -> dict:
    kwargs = dict(
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        weights=load_weights(),
        graph_failure=None,
        latency_ms=5,
        clarification_trace=[],
    )
    kwargs.update(overrides)
    return kwargs


def _captured_pre_detector_golden() -> dict:
    """The frozen pre-T11 golden: build_llm_artifact called with NO detector
    kwarg at all (the exact call shape every caller used before T11 added
    ``detection_outcome``). This is the reference the flag-OFF path must
    never drift from, no matter which persona's transcript text is involved."""
    return build_llm_artifact(**_artifact_kwargs())


@pytest.mark.parametrize(
    "persona_file",
    ["strong__gdp_identity.json", "misconception__gdp_identity.json"],
)
def test_build_llm_artifact_flag_off_byte_identical_across_personas(persona_file):
    """Regardless of whether the underlying transcript is a clean strong
    control or a genuine misconception persona, the flag-OFF artifact
    (detection_outcome=None, the only value done.py ever passes when the
    flag is off) is byte-identical to the captured pre-detector golden."""
    persona = _load_persona(persona_file)
    # The persona's content only affects _COVERAGE/_RUBRIC in a live run;
    # flag OFF means it is never even read into this builder — proven by
    # calling with the SAME coverage/rubric regardless of persona, and
    # asserting the golden output is unaffected by which persona "would
    # have" produced this coverage/rubric.
    assert persona["persona"] in {"strong", "misconception"}

    golden = _captured_pre_detector_golden()
    art_default = build_llm_artifact(**_artifact_kwargs())
    art_explicit_none = build_llm_artifact(**_artifact_kwargs(), detection_outcome=None)

    assert art_default == golden
    assert art_explicit_none == golden
    assert art_default["scores"]["misconception_penalty"] == 0.0
    assert art_default["misconceptions"] == []
    assert art_default["scores"]["composite"] == pytest.approx(0.71)


def test_build_llm_artifact_golden_shape_pinned():
    """Lock the exact golden dict shape/values so ANY drift in the flag-OFF
    default path (not just the two persona-flavored calls above) is caught."""
    golden = _captured_pre_detector_golden()

    assert golden["scores"]["misconception_penalty"] == 0.0
    assert golden["scores"]["composite"] == pytest.approx(0.71)
    assert golden["scores"]["node_coverage"] == pytest.approx(0.5)
    assert golden["scores"]["edge_coverage"] == 0.0
    assert golden["misconceptions"] == []
    assert golden["edge_ledger"] == []
    assert golden["clarification_trace"] == []


_NEW_CORROBORATION_FIELD_NAMES = {
    "bank_code",
    "bank_match_above_floor",
    "ceiling_eligible",
}


def _scan_for_forbidden_keys(value, forbidden: set[str]) -> None:
    """Recursively assert none of ``forbidden`` appear as a dict key anywhere
    in ``value`` (the corroboration-redesign fields are internal to the
    detector chain and must never be serialized into a flag-OFF artifact)."""
    if isinstance(value, dict):
        for key, sub in value.items():
            assert key not in forbidden, f"forbidden key {key!r} found in flag-OFF artifact"
            _scan_for_forbidden_keys(sub, forbidden)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _scan_for_forbidden_keys(item, forbidden)


def test_build_llm_artifact_flag_off_never_serializes_new_corroboration_fields():
    """The A10-A12 ConceptFinding fields (bank_code, bank_match_above_floor,
    ceiling_eligible) added by the corroboration/keying redesign are internal
    to the detector chain and must NEVER appear in any flag-OFF
    artifact/rubric dict — they simply never reach build_llm_artifact while
    detection_outcome is None."""
    golden = _captured_pre_detector_golden()
    _scan_for_forbidden_keys(golden, _NEW_CORROBORATION_FIELD_NAMES)


# --------------------------------------------------------------------------- #
# Layer 2: handle_done — mocked OLD-path harness, flag OFF, across both
# personas' utterance content (proving the content never reaches the grade).
# --------------------------------------------------------------------------- #

_OLD_RUBRIC = {
    "overall": {"score": 90, "letter": "A"},
    "procedure": {"score": 90, "letter": "A", "present": True},
    "justification": {"score": 90, "letter": "A", "present": True},
    "simplification": {"score": 90, "letter": "A", "present": True},
}


def _patches_with_rubric(patches, rubric):
    """Same helper pattern as T13's test harness: drop the shared float-scored
    ``compute_rubric`` patch and append a fresh integer-scored one last."""
    kept = [p for p in patches if getattr(p, "attribute", None) != "compute_rubric"]
    kept.append(patch("apollo.handlers.done.compute_rubric", return_value=dict(rubric)))
    return kept


async def _run_flag_off(monkeypatch, *, utterance: str):
    """Drive handle_done with the flag OFF and the OLD path fully mocked.

    ``detect_misconceptions`` is patched with a mock that would raise if
    called at all (flag OFF must never invoke it) rather than merely
    asserting non-invocation after the fact, so a wiring regression fails
    loudly even if a future refactor forgets the assertion.
    """
    monkeypatch.delenv(_FLAG, raising=False)

    db, _sess, _attempt, patches = _old_path_patches()

    detect_mock = AsyncMock(
        side_effect=AssertionError(
            "detect_misconceptions must never be called while the flag is OFF"
        )
    )

    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch("apollo.handlers.done.detect_misconceptions", new=detect_mock),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=(utterance,)),
        ),
    ]

    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=AsyncMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out, detect_mock


def _captured_pre_detector_done_golden(out: dict) -> None:
    """The frozen pre-T13 golden shape for the student-facing ``handle_done``
    payload (mirrors ``test_done_shadow_flag``'s frozen-golden assertions)."""
    assert out["rubric"] == _OLD_RUBRIC
    assert out["rubric"]["overall"]["score"] == 90
    assert out["rubric"]["overall"]["letter"] == "A"
    assert out["diagnostic_narrative"] == "narrative"
    assert out["coverage"] == {}
    assert out["xp_earned"] == 10
    assert out["xp_before"] == 0
    assert out["xp_after"] == 10
    assert out["level_up"] is False


@pytest.mark.parametrize(
    "persona_file",
    ["strong__gdp_identity.json", "misconception__gdp_identity.json"],
)
async def test_handle_done_flag_off_byte_identical_across_personas(monkeypatch, persona_file):
    """flag OFF -> detect_misconceptions NEVER called and the student-facing
    payload is byte-identical to the pre-detector golden, whether the
    underlying student utterance is a correct teaching turn (strong) or a
    genuine wrong belief (misconception persona) — the content must never
    reach the grade while the flag is off."""
    persona = _load_persona(persona_file)
    utterance = _persona_utterance(persona)

    out, detect_mock = await _run_flag_off(monkeypatch, utterance=utterance)

    detect_mock.assert_not_awaited()
    _captured_pre_detector_done_golden(out)


async def test_handle_done_flag_off_goldens_identical_between_personas(monkeypatch):
    """The two persona-driven runs must produce the EXACT SAME student-facing
    dict — proof the differing transcript content has zero effect on the
    flag-OFF grade path (the hard byte-identical regression guard)."""
    strong = _load_persona("strong__gdp_identity.json")
    misconception = _load_persona("misconception__gdp_identity.json")

    out_strong, _ = await _run_flag_off(monkeypatch, utterance=_persona_utterance(strong))
    out_misconception, _ = await _run_flag_off(
        monkeypatch, utterance=_persona_utterance(misconception)
    )

    assert out_strong == out_misconception


async def test_handle_done_flag_explicit_false_is_also_byte_identical(monkeypatch):
    monkeypatch.setenv(_FLAG, "false")

    db, _sess, _attempt, patches = _old_path_patches()
    detect_mock = AsyncMock(side_effect=AssertionError("must not be called when flag='false'"))
    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch("apollo.handlers.done.detect_misconceptions", new=detect_mock),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=()),
        ),
    ]
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=AsyncMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    detect_mock.assert_not_awaited()
    _captured_pre_detector_done_golden(out)


async def test_handle_done_flag_off_write_artifacts_receives_none_outcome(monkeypatch):
    """The threaded ``detection_outcome`` reaching ``write_artifacts`` is
    ``None`` while the flag is off — the artifact/ledger layer sees exactly
    the same no-op signal as every pre-T12 caller."""
    monkeypatch.delenv(_FLAG, raising=False)
    monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", "true")

    db, _sess, _attempt, patches = _old_path_patches()
    write_mock = AsyncMock(return_value=None)
    detect_mock = AsyncMock(side_effect=AssertionError("must not be called while flag is OFF"))

    patches = _patches_with_rubric(patches, _OLD_RUBRIC)
    patches += [
        patch("apollo.handlers.done.detect_misconceptions", new=detect_mock),
        patch(
            "apollo.handlers.done._student_utterances",
            new=AsyncMock(return_value=("transfer payments count",)),
        ),
        patch("apollo.handlers.done.write_artifacts", new=write_mock),
    ]
    for p in patches:
        p.start()
    try:
        await handle_done(db=db, neo=AsyncMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()

    write_mock.assert_awaited_once()
    assert write_mock.await_args.kwargs["detection_outcome"] is None
    detect_mock.assert_not_awaited()
