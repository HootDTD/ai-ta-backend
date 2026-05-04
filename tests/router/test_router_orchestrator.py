import numpy as np
import pytest
from unittest.mock import AsyncMock
from ai.router.embedding_router import Stage1Decision
from ai.router.llm_router import Stage2Decision
from ai.router.orchestrator import route, RouteDecision


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stage1_accept_skips_stage2():
    s1 = AsyncMock(); s2 = AsyncMock()
    s1.classify.return_value = Stage1Decision(
        accepted=True, route="definition", confidence=0.91,
        margin=0.4, top1_score=0.91, top2_score=0.51, top2_route="factual_lookup",
    )
    decision = await route(
        query="Define X.", stage1=s1, stage2=s2,
        recent_turns=[], cached_titles=[],
        topic_centroid=None, has_cache=False,
    )
    assert decision.final_route == "definition"
    assert decision.stage2_invoked is False
    s2.classify.assert_not_called()
    assert decision.retrieval_mode == "FRESH"     # has_cache=False -> FRESH


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stage1_abstain_invokes_stage2():
    s1 = AsyncMock(); s2 = AsyncMock()
    s1.classify.return_value = Stage1Decision(
        accepted=False, route=None, confidence=0.0,
        margin=0.05, top1_score=0.6, top2_score=0.55, top2_route="b",
    )
    s2.classify.return_value = Stage2Decision(
        route="conceptual_explainer", retrieval_mode="AUGMENT",
        confidence=0.78, reason="follow-up to prior turn",
    )
    decision = await route(
        query="And why?", stage1=s1, stage2=s2,
        recent_turns=[{"role": "user", "content": "What is entropy?"}],
        cached_titles=["Thermo ch.4"],
        topic_centroid=np.ones(3072),
        has_cache=True,
    )
    assert decision.final_route == "conceptual_explainer"
    assert decision.stage2_invoked is True
    assert decision.retrieval_mode == "AUGMENT"
