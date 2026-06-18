"""WU-4C2 — the DORMANT promote-to-live flag (`APOLLO_GRAPH_SIM_LIVE_ENABLED`).

The flag STAYS OFF in this build (prod, test, conftest). When OFF (the only build
state), `handle_done`'s student-facing return is byte-identical to WU-4C1's
OLD-path dict. When ON (dormant — exercised only via `monkeypatch` here), the
graph-sim `rubric` + constrained-diagnostic `narrative` REPLACE the two
student-facing keys; nothing else (coverage/progress/XP) changes.

Pure unit tests: every OLD-path collaborator is mocked deterministically (reusing
`test_done_shadow_flag._old_path_patches`), Neo4j is a MagicMock, and
`run_graph_simulation` is patched on the `done` module so no real chain / live
LLM runs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers import done as done_mod
from apollo.handlers.done import _graph_sim_live_enabled, handle_done
from apollo.handlers.tests.test_done_shadow_flag import _old_path_patches

pytestmark = pytest.mark.unit


def _shadow_result() -> MagicMock:
    """A fabricated ShadowGradeResult sentinel carrying a DISTINCT graph_sim
    rubric + constrained-diagnostic narrative (so promotion is observable)."""
    result = MagicMock(name="ShadowGradeResult")
    result.graph_sim_rubric = {"overall": {"score": 88, "letter": "B+"}}
    result.diagnostic = MagicMock(narrative="graph-sim narrative")
    return result


@pytest.fixture(autouse=True)
def _clear_flags(monkeypatch):
    monkeypatch.delenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LIVE_ENABLED", raising=False)
    yield


async def _run_with_flags(monkeypatch, *, shadow: bool, live, shadow_return):
    """Run handle_done with the two flags set + run_graph_simulation patched."""
    if shadow:
        monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    if live is not None:
        monkeypatch.setenv("APOLLO_GRAPH_SIM_LIVE_ENABLED", live)

    db, _sess, _attempt, patches = _old_path_patches()
    payload = {"declared_paths": [["a"]], "symbolic_mappings": {"d": "2*r"}}
    shadow_mock = AsyncMock(return_value=shadow_return)

    with (
        patch("apollo.handlers.done.run_graph_simulation", new=shadow_mock),
        patch("apollo.handlers.done._find_problem_payload", new=AsyncMock(return_value=payload)),
    ):
        for p in patches:
            p.start()
        try:
            out = await handle_done(db=db, neo=MagicMock(), session_id=11)
        finally:
            for p in reversed(patches):
                p.stop()
    return out, shadow_mock


def test_live_flag_parsing(monkeypatch):
    for truthy in ("1", "true", "TRUE", "Yes", "yes"):
        monkeypatch.setenv("APOLLO_GRAPH_SIM_LIVE_ENABLED", truthy)
        assert _graph_sim_live_enabled() is True
    for falsy in ("0", "false", "no", "", "off", "maybe"):
        monkeypatch.setenv("APOLLO_GRAPH_SIM_LIVE_ENABLED", falsy)
        assert _graph_sim_live_enabled() is False
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LIVE_ENABLED", raising=False)
    assert _graph_sim_live_enabled() is False


def test_live_flag_constant_name():
    assert done_mod._GRAPH_SIM_LIVE_FLAG == "APOLLO_GRAPH_SIM_LIVE_ENABLED"


async def test_live_off_return_byte_identical(monkeypatch):
    """SHADOW on, LIVE off: the shadow result carries a DISTINCT graph_sim rubric
    + diagnostic, but the student-facing rubric/diagnostic_narrative are the
    OLD-path values (byte-identical guard)."""
    out, shadow_mock = await _run_with_flags(
        monkeypatch, shadow=True, live="false", shadow_return=_shadow_result()
    )
    shadow_mock.assert_awaited_once()
    assert out["rubric"] == {"overall": {"score": 0.5}}  # OLD-path value
    assert out["diagnostic_narrative"] == "narrative"  # OLD-path value
    assert out["coverage"] == {}


async def test_live_off_default_when_unset(monkeypatch):
    """SHADOW on, LIVE unset -> defaults OFF -> OLD-path values stand."""
    out, _shadow = await _run_with_flags(
        monkeypatch, shadow=True, live=None, shadow_return=_shadow_result()
    )
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["diagnostic_narrative"] == "narrative"


async def test_live_on_promotes_graph_sim_rubric_and_diagnostic(monkeypatch):
    """SHADOW on + LIVE on: the two keys are REPLACED by graph-sim values;
    coverage/progress STAY OLD-path."""
    shadow = _shadow_result()
    out, _shadow_mock = await _run_with_flags(
        monkeypatch, shadow=True, live="true", shadow_return=shadow
    )
    assert out["rubric"] is shadow.graph_sim_rubric
    assert out["diagnostic_narrative"] == "graph-sim narrative"
    # coverage + progress unchanged (OLD-path).
    assert out["coverage"] == {}
    assert out["progress"]["xp_earned"] == 10


async def test_live_on_but_shadow_returns_none_keeps_old(monkeypatch):
    """LIVE on but run_graph_simulation returns None -> OLD-path values stand (no
    AttributeError on a None shadow)."""
    out, _shadow_mock = await _run_with_flags(
        monkeypatch, shadow=True, live="true", shadow_return=None
    )
    assert out["rubric"] == {"overall": {"score": 0.5}}
    assert out["diagnostic_narrative"] == "narrative"


async def test_old_rubric_forwarded_to_shadow(monkeypatch):
    """run_graph_simulation gets old_rubric=<the OLD student-facing rubric>."""
    _out, shadow_mock = await _run_with_flags(
        monkeypatch, shadow=True, live="false", shadow_return=_shadow_result()
    )
    kwargs = shadow_mock.await_args.kwargs
    assert kwargs["old_rubric"] == {"overall": {"score": 0.5}}
