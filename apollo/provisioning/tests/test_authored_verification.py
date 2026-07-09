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


@pytest.mark.asyncio
async def test_missing_confidence_fails_closed_and_triggers_verification(monkeypatch):
    """M4: a page missing from page_debug yields min_conf=None — that must be
    treated as LOW confidence (verification runs), never as high confidence."""
    import apollo.provisioning.authored_sets.verification as ver

    async def fake_generate(*a, **k):
        return _draft("5")

    monkeypatch.setattr(ver, "_independent_generate", fake_generate)

    v = await verify_against_generated(
        db=None,
        candidate=SimpleNamespace(problem_text="p"),
        draft=_draft("5"),
        min_conf=None,
        problem_low_conf=False,
        match_method="label",
        metered_chat=_FakeMC(True),
        conf_threshold=0.6,
    )
    # Agreement (same final answer "5") -> trusted, but verification DID run
    # (ocr_confidence carries the None through, review not required).
    assert v.review_required is False
    assert v.ocr_confidence is None


@pytest.mark.asyncio
async def test_missing_confidence_divergence_flags_for_review(monkeypatch):
    """M4 companion: min_conf=None + a divergent generated solution must still
    flag for review — proves verification actually ran rather than being
    short-circuited by the None-as-high-confidence bug."""
    import apollo.provisioning.authored_sets.verification as ver

    async def fake_generate(*a, **k):
        return _draft("999")

    monkeypatch.setattr(ver, "_independent_generate", fake_generate)

    v = await verify_against_generated(
        db=None,
        candidate=SimpleNamespace(problem_text="p"),
        draft=_draft("5"),
        min_conf=None,
        problem_low_conf=False,
        match_method="retrieval",
        metered_chat=_FakeMC(False),
        conf_threshold=0.6,
    )
    assert v.review_required is True
    assert v.reason == "ocr_divergence"


@pytest.mark.asyncio
async def test_empty_retrieve_yields_no_spans():
    from apollo.provisioning.authored_sets.verification import _empty_retrieve

    assert await _empty_retrieve(SimpleNamespace()) == ()


@pytest.mark.asyncio
async def test_independent_generate_grounds_on_empty_retrieve(monkeypatch):
    import apollo.provisioning.authored_sets.verification as ver

    captured = {}

    async def fake_fog(db, candidate, *, retrieve_fn, chat_fn):
        captured["retrieve_fn"] = retrieve_fn
        return _draft("5")

    monkeypatch.setattr(ver, "find_or_generate", fake_fog)
    out = await ver._independent_generate(None, SimpleNamespace(), chat_fn=lambda **k: "{}")
    assert out is not None
    assert captured["retrieve_fn"] is ver._empty_retrieve


def test_final_answer_empty_without_symbolic_steps():
    from apollo.provisioning.authored_sets.verification import _final_answer
    from apollo.provisioning.solution import ReferenceSolutionDraft

    draft = ReferenceSolutionDraft(
        solution_source="extracted",
        reference_solution=[{"entry_type": "procedure_step", "content": {}}],
        grounding=(),
        provenance={},
    )
    assert _final_answer(draft) == ""


class _JudgeMC:
    """A metered-chat whose judge (cheap) returns a fixed raw payload."""

    def __init__(self, raw: str):
        self._raw = raw

    def main(self, **_k):
        return "{}"

    def cheap(self, **_k):
        return self._raw


@pytest.mark.asyncio
async def test_low_confidence_judge_equivalent_trusts(monkeypatch):
    import json

    import apollo.provisioning.authored_sets.verification as ver

    async def fake_generate(*a, **k):
        return _draft("999")  # different final answer -> reaches the judge

    monkeypatch.setattr(ver, "_independent_generate", fake_generate)
    v = await verify_against_generated(
        db=None,
        candidate=SimpleNamespace(problem_text="p"),
        draft=_draft("5"),
        min_conf=0.30,
        problem_low_conf=False,
        match_method="label",
        metered_chat=_JudgeMC(json.dumps({"equivalent": True, "reason": "same"})),
        conf_threshold=0.6,
    )
    assert v.review_required is False


@pytest.mark.asyncio
async def test_low_confidence_unparseable_judge_fails_closed(monkeypatch):
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
        match_method="label",
        metered_chat=_JudgeMC("not json at all"),
        conf_threshold=0.6,
    )
    assert v.review_required is True
    assert v.reason == "ocr_divergence"
