"""Lane B1 / G3 — shadow-grader crash isolation.

The defect: with ``APOLLO_GRAPH_GRADER_LIVE=0`` (SHADOW mode — exactly how
staging/prod run during calibration) a shadow-chain exception (e.g. the G3
repro ``KeyError('variable_mapping')`` raised out of
``load_problem_candidates_with_soundness`` at ``done_grading.py`` step 3) used to
RE-RAISE out of ``handle_done`` and 500 the student's ``POST
/apollo/sessions/{id}/done`` — a *shadow* failure killing the *live* LLM grade.

The fix (NARROWED boundary): UNEXPECTED shadow-mode exceptions are caught at
the shadow-chain boundary in ``done.py`` (the ``except Exception`` around
``run_graph_simulation``). The handler logs with full context, records a
shadow-failure marker on the canonical LLM artifact
(``abstention.graph_failure``, so paired analysis sees the missing ``pair``
row and WHY), drops the shadow result, and serves the already-committed
OLD/LLM grade BYTE-IDENTICAL to a shadow-off Done-click.

The CONTRACTUAL typed failure modes the route maps to NON-500 responses keep
PROPAGATING in shadow mode exactly as pre-G3, with their pinned commit
semantics (``tests/database/test_done_shadow_route_postgres.py``). The
authoritative list is ``apollo/api.py::register_exception_handlers`` →
``done.py``'s ``_SHADOW_PROPAGATE_ERRORS``: ``ResolutionUnavailableError``
(503), ``TranscriptAuditUnavailableError`` (503), ``StudentGraphInvalidError``
(422), ``ReferenceGraphInvalidError`` (409). ``ResolutionInvalidOutputError``
maps to 500 and is therefore ISOLATED with the unexpected class.

The LIVE path (``APOLLO_GRAPH_GRADER_LIVE=1``) keeps its pre-existing
any-exception fallback semantics untouched (it catches ALL types, including
the four contractual ones).

Unlike ``test_done_shadow_flag`` / ``test_done_graph_grader_live`` (which patch
``run_graph_simulation`` wholesale), these tests run the REAL
``run_graph_simulation`` and inject failures at DISTINCT chain depths
(``done_grading``'s module-level collaborators) so the boundary catch is
exercised for an early / mid / late crash — the same ``except`` in ``done.py``
catches all three.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.errors import (
    ResolutionInvalidOutputError,
    ResolutionUnavailableError,
    TranscriptAuditUnavailableError,
)
from apollo.grading.artifact_build import (
    GRADER_USED_LLM_FALLBACK,
    build_llm_artifact,
)
from apollo.grading.composite import load_weights
from apollo.graph_compare.validator import (
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
)
from apollo.handlers import done as done_mod
from apollo.handlers.done import handle_done
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches, _rerun_inputs

pytestmark = pytest.mark.unit


# The OLD-path golden student_response (no scorecard) — the exact dict a
# shadow-OFF Done-click serves, from `test_done_shadow_flag`'s fixtures.
_GOLDEN_NO_SCORECARD = {
    "rubric": {"overall": {"score": 0.5}},
    "diagnostic_narrative": "narrative",
    "coverage": {},
    "progress": {
        "xp_earned": 10,
        "xp_before": 0,
        "xp_after": 10,
        "level_before": 1,
        "level_after": 1,
        "level_up": False,
        "title_after": "Novice",
        "level_progress_pct": 0.1,
        "xp_to_next_level": 90,
    },
    "xp_earned": 10,
    "xp_before": 0,
    "xp_after": 10,
    "level_before": 1,
    "level_after": 1,
    "level_up": False,
    "transcript": [],
    "grading_provenance": {
        "grader_used": "llm_fallback",
        "evidence_source": "graph_nodes",
        "transcript_grader_failure": None,
        "score_before_dock": 0.0,
        "topics": [],
        "docks": [],
        "graph_lane": None,
    },
}

# A deterministic canonical payload the mocked `write_artifacts` returns, so the
# rendered scorecard is identical across control and injected-crash runs (the
# scorecard templates over THIS, never over `graph_failure`).
_FAKE_CANONICAL_PAYLOAD = {
    "grader_used": GRADER_USED_LLM_FALLBACK,
    "scores": {"composite": 0.5},
    "node_ledger": [],
    "misconceptions": [],
    "clarification_trace": [],
}


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    for flag in (
        "APOLLO_GRAPH_SIM_SHADOW_ENABLED",
        "APOLLO_GRAPH_SIM_LIVE_ENABLED",
        "APOLLO_GRAPH_GRADER_LIVE",
        "APOLLO_GRADING_ARTIFACT_ENABLED",
        "APOLLO_GRAPH_SIM_LAYER3_ENABLED",
    ):
        monkeypatch.delenv(flag, raising=False)
    yield


# ---------------------------------------------------------------------------
# Injection points inside the REAL run_graph_simulation, keyed early/mid/late.
# Each returns the list of context-manager patches to layer over the OLD-path
# patches. All three raise out of run_graph_simulation and must hit the SAME
# `except` in done.py.
# ---------------------------------------------------------------------------

_DG = "apollo.handlers.done_grading"


def _inject_early(exc: BaseException):
    """Step 3 — the G3 site: `load_problem_candidates_with_soundness` raises
    before ANY resolution/cross-store work (no pending flag set)."""
    return [patch(f"{_DG}.load_problem_candidates_with_soundness", new=AsyncMock(side_effect=exc))]


def _inject_mid(exc: BaseException):
    """Step 4 — the raw-graph gate: `validate_student_graph` raises after the
    candidate load but still BEFORE the cross-store window."""
    fake_inputs = SimpleNamespace(candidates=(), symbolic_mappings={})
    return [
        patch(
            f"{_DG}.load_problem_candidates_with_soundness",
            new=AsyncMock(return_value=(fake_inputs, True)),
        ),
        patch(f"{_DG}.validate_student_graph", new=MagicMock(side_effect=exc)),
    ]


def _inject_late(exc: BaseException):
    """Step 5 — the cross-store window: `load_confirmed_resolutions` raises, so
    run_graph_simulation's own `except Exception` sets learner_update_pending +
    commits, THEN re-raises. The done.py boundary must still catch it."""
    fake_inputs = SimpleNamespace(candidates=(), symbolic_mappings={})
    return [
        patch(
            f"{_DG}.load_problem_candidates_with_soundness",
            new=AsyncMock(return_value=(fake_inputs, True)),
        ),
        patch(f"{_DG}.validate_student_graph", new=MagicMock(return_value=None)),
        patch(f"{_DG}.load_confirmed_resolutions", new=AsyncMock(side_effect=exc)),
    ]


async def _run(
    monkeypatch,
    *,
    shadow: bool,
    live,
    artifact: bool,
    injection_patches=None,
):
    """Drive the REAL handle_done + REAL run_graph_simulation, with the OLD path
    mocked, `build_rerun_inputs` stubbed, `write_artifacts` mocked (returns the
    fake canonical payload), and any `injection_patches` layered on. Returns
    ``(student_response, write_artifacts_mock)``."""
    if shadow:
        monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    if live is not None:
        monkeypatch.setenv("APOLLO_GRAPH_GRADER_LIVE", live)
    if artifact:
        monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", "true")

    db, _sess, _attempt, patches = _old_path_patches()
    rerun = _rerun_inputs(problem_payload={"declared_paths": [["a"]], "symbolic_mappings": {}})
    write_artifacts_mock = AsyncMock(return_value=_FAKE_CANONICAL_PAYLOAD)

    boundary = [
        patch("apollo.handlers.done.build_rerun_inputs", new=AsyncMock(return_value=rerun)),
        patch("apollo.handlers.done.write_artifacts", new=write_artifacts_mock),
        # Task B2 projection reads the DB — neutralize it (its own tests cover it).
        patch("apollo.handlers.done._project_mastery", new=AsyncMock(return_value=None)),
    ]
    boundary.extend(injection_patches or [])

    for p in patches:
        p.start()
    for p in boundary:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(boundary):
            p.stop()
        for p in reversed(patches):
            p.stop()
    return out, write_artifacts_mock


# ---------------------------------------------------------------------------
# 1. G3 regression — the exact KeyError('variable_mapping') no longer 500s.
# ---------------------------------------------------------------------------


async def test_g3_variable_mapping_keyerror_does_not_500(monkeypatch):
    """The exact G3 repro: SHADOW on, LIVE off, the step-3 loader raises
    ``KeyError('variable_mapping')``. Pre-fix this re-raised and 500'd the Done
    request; post-fix ``handle_done`` returns HTTP-200-worthy OLD-path values."""
    out, write_artifacts_mock = await _run(
        monkeypatch,
        shadow=True,
        live="false",
        artifact=True,
        injection_patches=_inject_early(KeyError("variable_mapping")),
    )
    # No exception escaped — the response is the OLD-path grade + a scorecard.
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["diagnostic_narrative"] == "narrative"
    assert "scorecard" in out

    # The shadow-failure marker reached the artifact writer, shadow dropped,
    # LLM grade served.
    write_artifacts_mock.assert_awaited_once()
    kwargs = write_artifacts_mock.await_args.kwargs
    assert kwargs["shadow"] is None
    assert kwargs["served"] == GRADER_USED_LLM_FALLBACK
    assert kwargs["graph_failure"].startswith(done_mod._SHADOW_FAILURE_MARKER)
    assert "variable_mapping" in kwargs["graph_failure"]


# ---------------------------------------------------------------------------
# 2. Byte-identity — a crash at early/mid/late chain depths serves a response
#    byte-identical (serialized JSON) to a shadow-DISABLED control run.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_injection, exc",
    [
        (_inject_early, KeyError("variable_mapping")),
        (_inject_mid, TypeError("gate blew up on a malformed node")),
        (_inject_late, RuntimeError("cross-store boom")),
    ],
    ids=["early", "mid", "late"],
)
async def test_shadow_crash_byte_identical_to_control(monkeypatch, make_injection, exc):
    """A shadow crash at ANY depth serves a payload byte-identical to the
    control (shadow DISABLED) run — the shadow flag/crash must not perturb a
    single byte of the student response."""
    control, _ = await _run(monkeypatch, shadow=False, live="false", artifact=True)
    crashed, _ = await _run(
        monkeypatch,
        shadow=True,
        live="false",
        artifact=True,
        injection_patches=make_injection(exc),
    )
    assert json.dumps(crashed, sort_keys=True) == json.dumps(control, sort_keys=True)
    # And it is the OLD-path golden (plus the deterministic scorecard).
    assert {k: v for k, v in crashed.items() if k != "scorecard"} == _GOLDEN_NO_SCORECARD


async def test_shadow_crash_byte_identical_no_artifact(monkeypatch):
    """Same byte-identity guard with artifact capture OFF: the shadow crash
    still serves the exact OLD-path golden and write_artifacts is never
    reached."""
    control, control_wa = await _run(monkeypatch, shadow=False, live="false", artifact=False)
    crashed, crashed_wa = await _run(
        monkeypatch,
        shadow=True,
        live="false",
        artifact=False,
        injection_patches=_inject_early(KeyError("variable_mapping")),
    )
    control_wa.assert_not_awaited()
    crashed_wa.assert_not_awaited()
    assert json.dumps(crashed, sort_keys=True) == json.dumps(control, sort_keys=True)
    assert crashed == _GOLDEN_NO_SCORECARD


# ---------------------------------------------------------------------------
# 3. The shadow-failure marker lands in the persisted artifact-row shape.
# ---------------------------------------------------------------------------


async def test_shadow_failure_marker_present_and_distinguishable(monkeypatch):
    """The marker done.py hands to `write_artifacts` (a) carries the
    shadow-failure prefix + the underlying error, and (b) lands in the LLM
    artifact row's `abstention.graph_failure` field (the persisted row shape —
    `build_llm_artifact` is the pure builder `write_artifacts` calls). Together
    these prove paired analysis can see the gap on the canonical row when the
    `pair` row is missing."""
    _out, write_artifacts_mock = await _run(
        monkeypatch,
        shadow=True,
        live="false",
        artifact=True,
        injection_patches=_inject_early(KeyError("variable_mapping")),
    )
    marker = write_artifacts_mock.await_args.kwargs["graph_failure"]
    assert marker.startswith(done_mod._SHADOW_FAILURE_MARKER)
    assert "variable_mapping" in marker

    # The pure builder writes the marker into the row's abstention block.
    row_payload = build_llm_artifact(
        coverage={},
        rubric={"overall": {"score": 0.5}},
        weights=load_weights(),
        graph_failure=marker,
        latency_ms=1,
        clarification_trace=[],
    )
    assert row_payload["abstention"]["graph_failure"] == marker
    assert row_payload["grader_used"] == GRADER_USED_LLM_FALLBACK


# ---------------------------------------------------------------------------
# 4. Propagation contract — the CONTRACTUAL typed failure modes (route-mapped
#    to NON-500 responses) keep propagating in shadow mode, exactly as pre-G3.
#    Pins the list so a future widening of the isolation boundary fails loudly
#    here instead of in the integration CI job
#    (tests/database/test_done_shadow_route_postgres.py).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_injection, exc",
    [
        # 503 — retryable infra failure in the cross-store window: the chain's
        # own except sets learner_update_pending + commits, then re-raises; the
        # done.py boundary must let it surface (route maps it to 503).
        (
            _inject_late,
            ResolutionUnavailableError(stage="write_resolves_to", last_error="neo4j down"),
        ),
        # 503 — transcript-audit infra failure (same pending-then-surface path).
        (_inject_late, TranscriptAuditUnavailableError(last_error="audit LLM timeout")),
        # 422 — the step-4 raw-graph gate: raised pre-cross-store, no pending.
        (_inject_mid, StudentGraphInvalidError(reasons=("bad node",))),
        # 409 — a bad reference: raised without pending, route maps to 409.
        (_inject_early, ReferenceGraphInvalidError(reasons=("no declared_paths",))),
    ],
    ids=[
        "resolution_unavailable_503",
        "transcript_audit_unavailable_503",
        "student_graph_invalid_422",
        "reference_graph_invalid_409",
    ],
)
async def test_shadow_mode_contractual_errors_propagate(monkeypatch, make_injection, exc):
    """Shadow mode + a `_SHADOW_PROPAGATE_ERRORS` type ⇒ the exception
    PROPAGATES out of handle_done (no marker, no swallow, no artifact write) so
    the route's registered handler serves its contractual non-500 response."""
    with pytest.raises(type(exc)):
        await _run(
            monkeypatch,
            shadow=True,
            live="false",
            artifact=True,
            injection_patches=make_injection(exc),
        )


def test_shadow_propagate_list_matches_route_contract():
    """Pin `_SHADOW_PROPAGATE_ERRORS` to exactly the shadow-chain error types
    whose registered route handlers map to a NON-500 status
    (`apollo/api.py::register_exception_handlers`). `ResolutionInvalidOutputError`
    maps to 500 and must NOT be in the list (it is isolated with the
    unexpected class — a 500 is exactly what G3 must never serve)."""
    assert set(done_mod._SHADOW_PROPAGATE_ERRORS) == {
        ResolutionUnavailableError,
        TranscriptAuditUnavailableError,
        StudentGraphInvalidError,
        ReferenceGraphInvalidError,
    }
    assert ResolutionInvalidOutputError not in done_mod._SHADOW_PROPAGATE_ERRORS


async def test_shadow_mode_resolution_invalid_output_is_isolated(monkeypatch):
    """`ResolutionInvalidOutputError` route-maps to 500, so shadow mode
    ISOLATES it like any unexpected failure: no re-raise, LLM grade served,
    marker recorded."""
    out, write_artifacts_mock = await _run(
        monkeypatch,
        shadow=True,
        live="false",
        artifact=True,
        injection_patches=_inject_late(
            ResolutionInvalidOutputError(returned_key="eq.hallucinated", allowed_keys=("eq.a",))
        ),
    )
    assert out["rubric"] == {"overall": {"score": 0.5}}
    kwargs = write_artifacts_mock.await_args.kwargs
    assert kwargs["shadow"] is None
    assert kwargs["graph_failure"].startswith(done_mod._SHADOW_FAILURE_MARKER)
    assert "eq.hallucinated" in kwargs["graph_failure"]


# ---------------------------------------------------------------------------
# 5. LIVE-path fallback semantics UNCHANGED (explicit guard).
# ---------------------------------------------------------------------------


async def test_live_mode_fallback_unchanged_no_shadow_marker(monkeypatch):
    """LIVE on + a shadow-chain crash: the pre-existing A4 any-exception
    fallback still fires — OLD/LLM values served, `graph_failure` a BARE
    `repr(e)` with NO shadow-failure prefix (the two error paths stay
    distinguishable), no re-raise."""
    out, write_artifacts_mock = await _run(
        monkeypatch,
        shadow=True,
        live="true",
        artifact=True,
        injection_patches=_inject_early(KeyError("variable_mapping")),
    )
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["diagnostic_narrative"] == "narrative"

    kwargs = write_artifacts_mock.await_args.kwargs
    assert kwargs["shadow"] is None
    assert kwargs["served"] == GRADER_USED_LLM_FALLBACK
    gf = kwargs["graph_failure"]
    assert not gf.startswith(done_mod._SHADOW_FAILURE_MARKER)
    assert "variable_mapping" in gf


async def test_live_mode_still_catches_contractual_types(monkeypatch):
    """LIVE mode's A4 any-exception fallback is UNTOUCHED by the shadow-mode
    propagate list: even a `_SHADOW_PROPAGATE_ERRORS` type (here
    `ResolutionUnavailableError`) is caught in LIVE mode and falls back to the
    OLD/LLM values — the student never loses their grade when the graph grader
    is live (spec §3 error handling, pre-G3 behavior)."""
    out, write_artifacts_mock = await _run(
        monkeypatch,
        shadow=True,
        live="true",
        artifact=True,
        injection_patches=_inject_late(
            ResolutionUnavailableError(stage="write_resolves_to", last_error="neo4j down")
        ),
    )
    assert out["rubric"] == {"overall": {"score": 0.5}}
    kwargs = write_artifacts_mock.await_args.kwargs
    assert kwargs["shadow"] is None
    assert kwargs["served"] == GRADER_USED_LLM_FALLBACK
    gf = kwargs["graph_failure"]
    assert not gf.startswith(done_mod._SHADOW_FAILURE_MARKER)
    assert "neo4j down" in gf
