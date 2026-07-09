"""Unit test for the NLI pre-warm entrypoint (campaign C2).

Mocks the model construction/load entirely — this test must never touch the
network or the real ``transformers`` package.
"""

from apollo.resolution import nli_adjudicator as mod
from apollo.resolution.nli_adjudicator import NLIResult


class _FakeAdjudicator:
    """Records construction args + classify() calls; never loads a real model."""

    instances: list["_FakeAdjudicator"] = []

    def __init__(self, model_name: str, device=None):
        self.model_name = model_name
        self.device = device
        self.classify_calls: list[tuple[str, str]] = []
        _FakeAdjudicator.instances.append(self)

    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        self.classify_calls.append((premise, hypothesis))
        return NLIResult("neutral", 0.3, 0.3, 0.4, self.model_name)


def test_prewarm_constructs_adjudicator_for_active_model_and_classifies_once(monkeypatch):
    _FakeAdjudicator.instances.clear()
    monkeypatch.setattr(mod, "TransformersNLIAdjudicator", _FakeAdjudicator)
    monkeypatch.setattr("apollo.resolution.nli_config.active_nli_model", lambda: "test-checkpoint")

    result = mod.prewarm()

    assert result is None
    assert len(_FakeAdjudicator.instances) == 1
    adjudicator = _FakeAdjudicator.instances[0]
    assert adjudicator.model_name == "test-checkpoint"
    # exactly one dummy classify — the "touches the loader once" contract.
    assert adjudicator.classify_calls == [("a", "a")]


def test_prewarm_uses_configured_nli_device(monkeypatch):
    _FakeAdjudicator.instances.clear()
    monkeypatch.setattr(mod, "TransformersNLIAdjudicator", _FakeAdjudicator)
    monkeypatch.setattr("apollo.resolution.nli_config.active_nli_model", lambda: "m")
    monkeypatch.setattr("apollo.resolution.nli_config.NLI_DEVICE", "cpu")

    mod.prewarm()

    assert _FakeAdjudicator.instances[-1].device == "cpu"
