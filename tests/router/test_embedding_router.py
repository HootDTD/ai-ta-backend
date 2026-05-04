import numpy as np
import pytest
from ai.router.embedding_router import EmbeddingRouter, Stage1Decision


class _StubEmbedder:
    """Returns deterministic vectors keyed by string contents."""

    def __init__(self, mapping: dict[str, np.ndarray]):
        self._m = mapping

    async def embed(self, text: str) -> np.ndarray:
        return self._m[text]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clear_winner_accepts():
    seeds = {"definition": ["what is X"], "factual_lookup": ["value of X"]}
    v_def = np.zeros(3072)
    v_def[0] = 1.0
    v_fact = np.zeros(3072)
    v_fact[1] = 1.0
    embedder = _StubEmbedder(
        {"what is X": v_def, "value of X": v_fact, "Define X please": v_def}
    )
    router = await EmbeddingRouter.build(embedder, seeds)
    decision = await router.classify("Define X please")
    assert isinstance(decision, Stage1Decision)
    assert decision.accepted is True
    assert decision.route == "definition"
    assert decision.confidence > 0.9
    assert decision.margin > 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_low_margin_abstains():
    seeds = {"definition": ["what is X"], "factual_lookup": ["what is X"]}
    v = np.zeros(3072)
    v[0] = 1.0
    embedder = _StubEmbedder({"what is X": v, "ambiguous": v})
    router = await EmbeddingRouter.build(embedder, seeds)
    decision = await router.classify("ambiguous")
    assert decision.accepted is False
    assert decision.margin < 0.10
