from types import SimpleNamespace

import pytest

from apollo.provisioning.authored_sets.verification import verify_against_generated
from apollo.provisioning.solution import ReferenceSolutionDraft


def _draft(answer):
    return ReferenceSolutionDraft(
        solution_source="extracted",
        reference_solution=[{"entry_type": "answer", "symbolic": f"x = {answer}"}],
        grounding=(),
        provenance={},
    )


class _FakeMC:
    def __init__(self, equivalent: bool):
        self._eq = equivalent

    def main(self, **k):
        import json

        return json.dumps({"reference_solution": [{"entry_type": "answer", "symbolic": "x = 5"}]})

    def cheap(self, **k):
        import json

        return json.dumps({"equivalent": self._eq, "reason": "n/a"})


@pytest.mark.asyncio
async def test_high_confidence_skips_verification():
    v = await verify_against_generated(
        db=None,
        candidate=SimpleNamespace(problem_text="p"),
        draft=_draft("5"),
        min_conf=0.95,
        problem_low_conf=False,
        match_method="label",
        metered_chat=_FakeMC(True),
        conf_threshold=0.6,
    )
    assert v.review_required is False
    assert v.generated_alt is None


@pytest.mark.asyncio
async def test_low_confidence_divergence_flags(monkeypatch):
    import apollo.provisioning.authored_sets.verification as ver

    async def fake_generate(*a, **k):
        return _draft("999")

    monkeypatch.setattr(ver, "_independent_generate", fake_generate)

    v = await verify_against_generated(
        db=None,
        candidate=SimpleNamespace(problem_text="p"),
        draft=_draft("5"),
        min_conf=0.30,
        problem_low_conf=False,
        match_method="retrieval",
        metered_chat=_FakeMC(False),
        conf_threshold=0.6,
    )
    assert v.review_required is True
    assert v.reason == "ocr_divergence"
    assert v.generated_alt is not None


@pytest.mark.asyncio
async def test_low_confidence_agreement_trusts(monkeypatch):
    import apollo.provisioning.authored_sets.verification as ver

    async def fake_generate(*a, **k):
        return _draft("5")

    monkeypatch.setattr(ver, "_independent_generate", fake_generate)

    v = await verify_against_generated(
        db=None,
        candidate=SimpleNamespace(problem_text="p"),
        draft=_draft("5"),
        min_conf=0.30,
        problem_low_conf=False,
        match_method="label",
        metered_chat=_FakeMC(True),
        conf_threshold=0.6,
    )
    assert v.review_required is False
