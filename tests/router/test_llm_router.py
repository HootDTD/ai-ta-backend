import json
import pytest
from unittest.mock import AsyncMock
from ai.router.llm_router import LLMRouter, Stage2Decision


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_route_and_retrieval_mode():
    fake_openai = AsyncMock()
    fake_openai.chat.completions.create.return_value.choices = [type("C", (), {
        "message": type("M", (), {
            "content": json.dumps({
                "route": "definition","retrieval_mode": "FRESH",
                "confidence": 0.84,"reason": "asks to define a term"
            })
        })()
    })()]
    router = LLMRouter(client=fake_openai, model="gpt-4o-mini")
    decision = await router.classify(
        query="Define enthalpy.",
        recent_turns=[],
        cached_titles=[],
    )
    assert isinstance(decision, Stage2Decision)
    assert decision.route == "definition"
    assert decision.retrieval_mode == "FRESH"
    assert 0.0 <= decision.confidence <= 1.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_uses_structured_output_response_format():
    fake_openai = AsyncMock()
    fake_openai.chat.completions.create.return_value.choices = [type("C", (), {
        "message": type("M", (), {"content": '{"route":"clarify","retrieval_mode":"NONE","confidence":0.5,"reason":"x"}'})()
    })()]
    router = LLMRouter(client=fake_openai, model="gpt-4o-mini")
    await router.classify(query="?", recent_turns=[], cached_titles=[])
    kwargs = fake_openai.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["strict"] is True
