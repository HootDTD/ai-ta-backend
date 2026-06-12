"""Tests for ai/router/mode.py — the LLM-only retrieval-mode decision."""

import json
from unittest.mock import AsyncMock

import pytest

from ai.router.llm_router import LLMRouter
from ai.router.mode import ModeDecision, decide_retrieval_mode


def _fake_client(route: str, mode: str, confidence: float, reason: str = "r") -> AsyncMock:
    fake = AsyncMock()
    fake.chat.completions.create.return_value.choices = [
        type(
            "C",
            (),
            {
                "message": type(
                    "M",
                    (),
                    {
                        "content": json.dumps(
                            {
                                "route": route,
                                "retrieval_mode": mode,
                                "confidence": confidence,
                                "reason": reason,
                            }
                        )
                    },
                )()
            },
        )()
    ]
    return fake


def _router(client) -> LLMRouter:
    return LLMRouter(client=client, model="gpt-4o-mini")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_cache_short_circuits_to_fresh_without_llm_call():
    client = _fake_client("definition", "NONE", 0.9)
    decision = await decide_retrieval_mode(
        question="What is a p-series?",
        has_cache=False,
        recent_turns=[],
        cached_titles=[],
        llm_router=_router(client),
    )
    assert decision.mode == "FRESH"
    assert decision.llm_invoked is False
    client.chat.completions.create.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cyu_reply_routes_none_via_llm():
    client = _fake_client("conceptual_explainer", "NONE", 0.92, "answer to CYU in last turn")
    decision = await decide_retrieval_mode(
        question="B",
        has_cache=True,
        recent_turns=[
            {
                "role": "assistant",
                "content": "Check your understanding: which test applies? A) ratio B) p-series",
            },
            {"role": "user", "content": "B"},
        ],
        cached_titles=["Calc Textbook — 7.3 Series"],
        llm_router=_router(client),
    )
    assert decision.mode == "NONE"
    assert decision.llm_invoked is True
    assert decision.route == "conceptual_explainer"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_low_confidence_none_downgrades_to_fresh():
    client = _fake_client("factual_lookup", "NONE", 0.3)
    decision = await decide_retrieval_mode(
        question="hmm what about the other one",
        has_cache=True,
        recent_turns=[],
        cached_titles=[],
        llm_router=_router(client),
    )
    assert decision.mode == "FRESH"
    assert "downgraded" in decision.reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_router_exception_fails_open_to_fresh():
    client = AsyncMock()
    client.chat.completions.create.side_effect = RuntimeError("api down")
    decision = await decide_retrieval_mode(
        question="more about integration by parts",
        has_cache=True,
        recent_turns=[],
        cached_titles=[],
        llm_router=_router(client),
    )
    assert decision.mode == "FRESH"
    assert decision.llm_invoked is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_question_is_fresh_without_llm():
    client = _fake_client("clarify", "NONE", 0.9)
    decision = await decide_retrieval_mode(
        question="   ",
        has_cache=True,
        recent_turns=[],
        cached_titles=[],
        llm_router=_router(client),
    )
    assert decision.mode == "FRESH"
    assert decision.llm_invoked is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_augment_passes_through_with_confidence():
    client = _fake_client("conceptual_explainer", "AUGMENT", 0.7, "related follow-up")
    decision = await decide_retrieval_mode(
        question="can you go deeper on convergence tests?",
        has_cache=True,
        recent_turns=[{"role": "user", "content": "what is a p-series?"}],
        cached_titles=["Calc Textbook — 7.3 Series"],
        llm_router=_router(client),
    )
    assert decision.mode == "AUGMENT"
    assert decision.confidence == pytest.approx(0.7)
    assert isinstance(decision, ModeDecision)


@pytest.mark.unit
def test_min_confidence_falls_back_on_bad_env(monkeypatch):
    from ai.router.mode import _min_confidence

    monkeypatch.setenv("ROUTER_MIN_CONFIDENCE", "not-a-float")
    assert _min_confidence() == 0.5
