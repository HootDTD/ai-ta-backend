"""Output filter tests — V3 two-stage filter.

Tests cover the deterministic pre-filter stage in isolation by injecting
a stub judge that always returns leaks=false. Judge-stage tests live in
test_leakage_judge.py.

Filter rejects any draft that contains a concept-scoped forbidden term
NOT present in either the KG summary or the student's history. NO
FALLBACK — rejection raises FilterRejectedError.
"""
import pytest

from apollo.agent.leakage_judge import JudgeVerdict
from apollo.agent.output_filter import validate_or_raise
from apollo.errors import FilterRejectedError
from apollo.subjects import load_concept


@pytest.fixture(scope="module")
def concept():
    return load_concept("fluid_mechanics", "bernoulli_principle")


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


def test_reply_using_only_student_vocabulary_passes(concept, stub_judge_clean):
    draft = "So when density is constant, A1 times v1 equals A2 times v2 — what does that tell you?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=STUDENT_HISTORY_BERNOULLI,
        kg_summary=KG_SUMMARY_BERNOULLI,
        judge=stub_judge_clean,
    )
    assert out == draft


def test_reply_using_student_label_passes(concept, stub_judge_clean):
    draft = "You mentioned Bernoulli's equation — can you remind me what each term represents?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=STUDENT_HISTORY_BERNOULLI,
        kg_summary=KG_SUMMARY_BERNOULLI,
        judge=stub_judge_clean,
    )
    assert out == draft


def test_reply_introducing_continuity_unprompted_rejected(concept, stub_judge_clean):
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


def test_reply_introducing_viscosity_rejected(concept, stub_judge_clean):
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


def test_reply_introducing_navier_stokes_rejected(concept, stub_judge_clean):
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


def test_reply_introducing_compressibility_rejected(concept, stub_judge_clean):
    draft = "Does compressibility matter here?"
    with pytest.raises(FilterRejectedError):
        validate_or_raise(
            draft,
            concept=concept,
            history=STUDENT_HISTORY_BERNOULLI,
            kg_summary=KG_SUMMARY_BERNOULLI,
            judge=stub_judge_clean,
        )


def test_reply_mentioning_energy_conservation_unprompted_rejected(concept, stub_judge_clean):
    draft = "This looks like energy conservation to me."
    with pytest.raises(FilterRejectedError):
        validate_or_raise(
            draft,
            concept=concept,
            history=STUDENT_HISTORY_BERNOULLI,
            kg_summary=KG_SUMMARY_BERNOULLI,
            judge=stub_judge_clean,
        )


def test_common_english_words_never_trigger_rejection(concept, stub_judge_clean):
    draft = "Okay, let me make sure I understand. You said the product of area and velocity stays the same — why is that?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=STUDENT_HISTORY_BERNOULLI,
        kg_summary=KG_SUMMARY_BERNOULLI,
        judge=stub_judge_clean,
    )
    assert out == draft


def test_misspelled_law_name_allows_apollo_canonical(concept, stub_judge_clean):
    """Student introduces Bernoulli by a misspelled name ("bernulis"); Apollo
    may then use the correctly-spelled canonical name. Regression for the
    over-strict exact-match pre-filter that blocked the typo case."""
    history = [
        {"role": "user", "content": (
            "Use bernulis equation P + Rational(1,2)*v**2*rho = C. "
            "Now we can use continuity, then sub in values."
        )},
    ]
    summary = "- equation (bernulis equation): P + Rational(1,2)*v**2*rho - C"
    draft = "Okay — so using Bernoulli's equation, what do we do after continuity?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=history,
        kg_summary=summary,
        judge=stub_judge_clean,
    )
    assert out == draft


def test_misspelled_law_name_allows_apollo_canonical_no_apostrophe(concept, stub_judge_clean):
    """Same as above but Apollo writes 'Bernoulli' without the possessive 's."""
    history = [
        {"role": "user", "content": "lets use bernulis here, P + half rho v^2 = const"},
    ]
    summary = "- equation (bernulis): P + Rational(1,2)*rho*v**2 - C"
    draft = "Right, so the Bernoulli relation is what ties pressure and speed together?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=history,
        kg_summary=summary,
        judge=stub_judge_clean,
    )
    assert out == draft


def test_typo_tolerance_does_not_allow_unintroduced_law(concept, stub_judge_clean):
    """Fuzzy tolerance must stay scoped: introducing one law (continuity)
    must NOT unlock an unrelated law (Torricelli) Apollo was never taught."""
    history = [
        {"role": "user", "content": "continuity says A1*v1 = A2*v2 for incompressible flow"},
    ]
    summary = "- equation (continuity): A1*v1 - A2*v2"
    draft = "Ah, this is really Torricelli's theorem in disguise."
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(
            draft,
            concept=concept,
            history=history,
            kg_summary=summary,
            judge=stub_judge_clean,
        )
    assert "torricelli" in exc_info.value.rejected_term.lower()


def test_judge_high_confidence_leak_rejected(concept):
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


def test_judge_low_confidence_leak_passes(concept):
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
