"""`APOLLO_NODE_JUDGE` switch on the Done path (spec 2026-10-29).

Unset / `gpt` -> the GPT adjudicator exactly as before (Jev never touched).
`jev` -> the Jev per-turn judge supplies coverage + spans. Any Jev exception
logs and falls back to the GPT adjudicator, so the flag can never fail a Done."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apollo.handlers.done import handle_done
from apollo.handlers.tests._done_fixtures import _old_path_patches
from apollo.ontology import KGGraph
from apollo.overseer.jev_client import JevError
from apollo.overseer.jev_node_judge import JevJudgeResult, TurnNodeCell
from config.settings import NODE_JUDGE_GPT, NODE_JUDGE_JEV, apollo_node_judge

pytestmark = pytest.mark.unit

_NEG = {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0}
_GPT_COVERAGE = {
    "per_step": {"eq1": "missing"},
    "procedure_scores": {"eq1": 0.3},
    "confidences": {"eq1": 0.5},
    "negotiation_counts": _NEG,
}
_JEV_COVERAGE = {
    "per_step": {"eq1": "covered"},
    "procedure_scores": {"eq1": 1.0},
    "confidences": {"eq1": 0.97},
    "negotiation_counts": _NEG,
    "basis": {"eq1": "covered"},
}
_TRANSCRIPT = (("apollo", "What equation?"), ("student", "Bernoulli: P + q = c"))


def _jev_result() -> JevJudgeResult:
    cell = TurnNodeCell(0, "eq1", "covered", {"covered": 0.97}, 0.97, 120.0)
    return JevJudgeResult(
        verdict=dict(_JEV_COVERAGE),  # type: ignore[arg-type]
        cells=[cell],
        winning_turn={"eq1": 0},
        usage={"input_tokens": 10, "output_tokens": 1},
    )


async def _run(monkeypatch, *, flag: str | None, judge: AsyncMock):
    if flag is None:
        monkeypatch.delenv("APOLLO_NODE_JUDGE", raising=False)
    else:
        monkeypatch.setenv("APOLLO_NODE_JUDGE", flag)
    db, _sess, _attempt, patches = _old_path_patches()
    db.begin_nested.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)

    drop = {
        "compute_transcript_coverage_with_spans",
        "compute_rubric",
        "_full_transcript",
        "compute_topic_score",
    }
    patches = [p for p in patches if getattr(p, "attribute", None) not in drop]
    gpt = AsyncMock(return_value=(dict(_GPT_COVERAGE), {"eq1": "gpt span"}))
    rubric = MagicMock(return_value={"overall": {"score": 0.5}})
    topic = MagicMock(return_value=None)
    patches += [
        patch("apollo.handlers.done.compute_topic_score", new=topic),
        patch("apollo.handlers.done.compute_transcript_coverage_with_spans", new=gpt),
        patch("apollo.handlers.done.compute_rubric", new=rubric),
        patch("apollo.handlers.done._full_transcript", new=AsyncMock(return_value=_TRANSCRIPT)),
        patch("apollo.handlers.done.judge_transcript", new=judge),
    ]
    for p in patches:
        p.start()
    try:
        out = await handle_done(db=db, neo=MagicMock(), session_id=11)
    finally:
        for p in reversed(patches):
            p.stop()
    return out, gpt, rubric, topic


@pytest.mark.parametrize("flag", [None, "", "gpt", "GPT", "openai", "jevv"])
async def test_flag_absent_or_not_jev_never_touches_jev(monkeypatch, flag):
    judge = AsyncMock(return_value=_jev_result())
    out, gpt, rubric, topic = await _run(monkeypatch, flag=flag, judge=judge)

    judge.assert_not_awaited()
    gpt.assert_awaited_once()
    assert rubric.call_args.args[0] == _GPT_COVERAGE
    assert topic.call_args.kwargs["evidence_spans"] == {"eq1": "gpt span"}
    assert out is not None


@pytest.mark.parametrize("flag", ["jev", " JEV "])
async def test_flag_jev_grades_with_the_jev_verdict(monkeypatch, flag):
    judge = AsyncMock(return_value=_jev_result())
    _out, gpt, rubric, topic = await _run(monkeypatch, flag=flag, judge=judge)

    judge.assert_awaited_once()
    graph_arg, transcript_arg = judge.await_args.args
    assert isinstance(graph_arg, KGGraph)
    assert transcript_arg == _TRANSCRIPT
    gpt.assert_not_awaited()
    assert rubric.call_args.args[0] == _JEV_COVERAGE
    # The narrative span is the winning student turn itself, verbatim.
    assert topic.call_args.kwargs["evidence_spans"] == {"eq1": "Bernoulli: P + q = c"}


@pytest.mark.parametrize("error", [JevError("HTTP 529", status_code=529), RuntimeError("boom")])
async def test_any_jev_failure_raises_without_gpt_fallback(monkeypatch, error):
    judge = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await _run(monkeypatch, flag="jev", judge=judge)

    judge.assert_awaited_once()


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, NODE_JUDGE_GPT),
        ("", NODE_JUDGE_GPT),
        ("gpt", NODE_JUDGE_GPT),
        ("nonsense", NODE_JUDGE_GPT),
        ("jev", NODE_JUDGE_JEV),
        ("Jev", NODE_JUDGE_JEV),
    ],
)
def test_apollo_node_judge_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("APOLLO_NODE_JUDGE", raising=False)
    else:
        monkeypatch.setenv("APOLLO_NODE_JUDGE", raw)
    assert apollo_node_judge() == expected
