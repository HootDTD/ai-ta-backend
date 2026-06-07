"""Output filter tests — V3 two-stage filter.

Tests cover the deterministic pre-filter stage in isolation by injecting
a stub judge that always returns leaks=false. Judge-stage tests live in
test_leakage_judge.py.

Filter rejects any draft that contains a concept-scoped forbidden term
NOT present in either the KG summary or the student's history. NO
FALLBACK — rejection raises FilterRejectedError.
"""
import pytest
import pytest_asyncio

from apollo.agent.leakage_judge import JudgeVerdict
from apollo.agent.output_filter import validate_or_raise
from apollo.errors import FilterRejectedError
from apollo.subjects.tests.seed_helpers import seed_bernoulli_concept


@pytest_asyncio.fixture
async def concept(neo4j_test):
    return await seed_bernoulli_concept(neo4j_test)


@pytest.fixture
def stub_judge_clean():
    """Judge that always says the draft is clean. Isolates pre-filter tests."""
    def _judge(*, draft, concept, history, kg_summary):
        return JudgeVerdict(
            leaks=False, offending_phrase=None, reason=None, confidence=0.0,
        )
    return _judge


STUDENT_HISTORY_BERNOULLI = [
    {"role": "user", "content": "For an incompressible fluid, A1*v1 = A2*v2. Density is constant."},
    {"role": "user", "content": "Bernoulli's equation P1 + Rational(1,2)*rho*v1**2 = P2 + Rational(1,2)*rho*v2**2."},
]

# The summary mirrors what `KGStore.summarize_for_apollo` would produce for the
# bernoulli scenario above — student's labels and equation symbolics.
KG_SUMMARY_BERNOULLI = (
    "- equation (Continuity): A1*v1 - A2*v2\n"
    "- equation (Bernoulli's equation): P1 + Rational(1,2)*rho*v1**2 - (P2 + Rational(1,2)*rho*v2**2)\n"
    "- condition: density is constant"
)


@pytest.mark.asyncio
async def test_reply_using_only_student_vocabulary_passes(concept, stub_judge_clean):
    draft = "So when density is constant, A1 times v1 equals A2 times v2 — what does that tell you?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=STUDENT_HISTORY_BERNOULLI,
        kg_summary=KG_SUMMARY_BERNOULLI,
        judge=stub_judge_clean,
    )
    assert out == draft


@pytest.mark.asyncio
async def test_reply_using_student_label_passes(concept, stub_judge_clean):
    draft = "You mentioned Bernoulli's equation — can you remind me what each term represents?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=STUDENT_HISTORY_BERNOULLI,
        kg_summary=KG_SUMMARY_BERNOULLI,
        judge=stub_judge_clean,
    )
    assert out == draft


@pytest.mark.asyncio
async def test_reply_introducing_continuity_unprompted_rejected(concept, stub_judge_clean):
    summary_without_continuity = "- equation ((no label)): A1*v1 - A2*v2"
    history_without_the_word = [
        {"role": "user", "content": "A1*v1 = A2*v2 for incompressible."},
    ]
    draft = "You're using the continuity equation there — nice."
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(
            draft,
            concept=concept,
            history=history_without_the_word,
            kg_summary=summary_without_continuity,
            judge=stub_judge_clean,
        )
    assert exc_info.value.rejected_term == "continuity"


@pytest.mark.asyncio
async def test_reply_introducing_viscosity_rejected(concept, stub_judge_clean):
    draft = "What about viscosity — does that factor in?"
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(
            draft,
            concept=concept,
            history=STUDENT_HISTORY_BERNOULLI,
            kg_summary=KG_SUMMARY_BERNOULLI,
            judge=stub_judge_clean,
        )
    assert exc_info.value.rejected_term == "viscosity"


@pytest.mark.asyncio
async def test_reply_introducing_navier_stokes_rejected(concept, stub_judge_clean):
    draft = "Is this related to Navier-Stokes at all?"
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(
            draft,
            concept=concept,
            history=STUDENT_HISTORY_BERNOULLI,
            kg_summary=KG_SUMMARY_BERNOULLI,
            judge=stub_judge_clean,
        )
    assert "navier" in exc_info.value.rejected_term.lower()


@pytest.mark.asyncio
async def test_reply_introducing_compressibility_rejected(concept, stub_judge_clean):
    draft = "Does compressibility matter here?"
    with pytest.raises(FilterRejectedError):
        validate_or_raise(
            draft,
            concept=concept,
            history=STUDENT_HISTORY_BERNOULLI,
            kg_summary=KG_SUMMARY_BERNOULLI,
            judge=stub_judge_clean,
        )


@pytest.mark.asyncio
async def test_reply_mentioning_energy_conservation_unprompted_rejected(concept, stub_judge_clean):
    draft = "This looks like energy conservation to me."
    with pytest.raises(FilterRejectedError):
        validate_or_raise(
            draft,
            concept=concept,
            history=STUDENT_HISTORY_BERNOULLI,
            kg_summary=KG_SUMMARY_BERNOULLI,
            judge=stub_judge_clean,
        )


@pytest.mark.asyncio
async def test_common_english_words_never_trigger_rejection(concept, stub_judge_clean):
    draft = "Okay, let me make sure I understand. You said the product of area and velocity stays the same — why is that?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=STUDENT_HISTORY_BERNOULLI,
        kg_summary=KG_SUMMARY_BERNOULLI,
        judge=stub_judge_clean,
    )
    assert out == draft


@pytest.mark.asyncio
async def test_judge_high_confidence_leak_rejected(concept):
    """Judge stage rejection path — paraphrase the deterministic stage misses."""
    def _judge(*, draft, concept, history, kg_summary):
        return JudgeVerdict(
            leaks=True,
            offending_phrase="speed times area is constant",
            reason="paraphrase of continuity",
            confidence=0.85,
        )
    draft = "So speed times area is constant — interesting."
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(
            draft,
            concept=concept,
            history=STUDENT_HISTORY_BERNOULLI,
            kg_summary=KG_SUMMARY_BERNOULLI,
            judge=_judge,
        )
    assert exc_info.value.rejected_term == "speed times area is constant"


@pytest.mark.asyncio
async def test_judge_low_confidence_leak_passes(concept):
    """Below-threshold leak is logged but does not block."""
    def _judge(*, draft, concept, history, kg_summary):
        return JudgeVerdict(
            leaks=True,
            offending_phrase="something",
            reason="weak signal",
            confidence=0.3,
        )
    draft = "Hmm, something about how it all fits together."
    out = validate_or_raise(
        draft,
        concept=concept,
        history=STUDENT_HISTORY_BERNOULLI,
        kg_summary=KG_SUMMARY_BERNOULLI,
        judge=_judge,
    )
    assert out == draft
