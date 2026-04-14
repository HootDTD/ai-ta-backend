"""Output filter tests — the structural guarantee Apollo is 'genuinely stupid'.

Filter rejects any draft that contains a physics-stopword NOT present in
either the current KG or the student's message history. NO FALLBACK —
rejection raises FilterRejectedError, which the UI surfaces as a visible
error. No template substitution."""
import pytest

from apollo.agent.output_filter import validate_or_raise
from apollo.errors import FilterRejectedError


STUDENT_HISTORY_BERNOULLI = [
    {"role": "user", "content": "For an incompressible fluid, A1*v1 = A2*v2. Density is constant."},
    {"role": "user", "content": "Bernoulli's equation P1 + Rational(1,2)*rho*v1**2 = P2 + Rational(1,2)*rho*v2**2."},
]

KG_BERNOULLI = {
    "equation": [
        {"symbolic": "A1*v1 - A2*v2", "label": "Continuity"},
        {"symbolic": "P1 + Rational(1,2)*rho*v1**2 - (P2 + Rational(1,2)*rho*v2**2)", "label": "Bernoulli's equation"},
    ],
    "definition": [],
    "condition": [{"applies_when": "density is constant", "label": "Incompressibility"}],
    "simplification": [],
    "variable_mapping": [],
}


def test_reply_using_only_student_vocabulary_passes():
    draft = "So when density is constant, A1 times v1 equals A2 times v2 — what does that tell you?"
    assert validate_or_raise(draft, KG_BERNOULLI, STUDENT_HISTORY_BERNOULLI) == draft


def test_reply_using_student_label_passes():
    draft = "You mentioned Bernoulli's equation — can you remind me what each term represents?"
    assert validate_or_raise(draft, KG_BERNOULLI, STUDENT_HISTORY_BERNOULLI) == draft


def test_reply_introducing_continuity_unprompted_rejected():
    kg_without_continuity_label = {
        "equation": [
            {"symbolic": "A1*v1 - A2*v2", "label": ""},
        ],
        "definition": [],
        "condition": [],
        "simplification": [],
        "variable_mapping": [],
    }
    student_without_the_word = [
        {"role": "user", "content": "A1*v1 = A2*v2 for incompressible."},
    ]
    draft = "You're using the continuity equation there — nice."
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(draft, kg_without_continuity_label, student_without_the_word)
    assert exc_info.value.rejected_term == "continuity"


def test_reply_introducing_viscosity_rejected():
    draft = "What about viscosity — does that factor in?"
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(draft, KG_BERNOULLI, STUDENT_HISTORY_BERNOULLI)
    assert exc_info.value.rejected_term == "viscosity"


def test_reply_introducing_navier_stokes_rejected():
    draft = "Is this related to Navier-Stokes at all?"
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(draft, KG_BERNOULLI, STUDENT_HISTORY_BERNOULLI)
    assert "navier" in exc_info.value.rejected_term.lower()


def test_reply_introducing_compressibility_rejected():
    draft = "Does compressibility matter here?"
    with pytest.raises(FilterRejectedError):
        validate_or_raise(draft, KG_BERNOULLI, STUDENT_HISTORY_BERNOULLI)


def test_reply_mentioning_energy_conservation_unprompted_rejected():
    draft = "This looks like energy conservation to me."
    with pytest.raises(FilterRejectedError):
        validate_or_raise(draft, KG_BERNOULLI, STUDENT_HISTORY_BERNOULLI)


def test_common_english_words_never_trigger_rejection():
    draft = "Okay, let me make sure I understand. You said the product of area and velocity stays the same — why is that?"
    assert validate_or_raise(draft, KG_BERNOULLI, STUDENT_HISTORY_BERNOULLI) == draft
