"""P2.6 — Output filter blocks misconception payload leak.

The persona shift only ever serializes authored `probe`/`rt_steps`
into Apollo's prompt. This filter is the defense-in-depth that catches
any LLM that paraphrases its way back to the suspected-misconception
description (or — defensively — quotes the bank code/id).

Block conditions:
- draft contains misconception.description verbatim (case-insensitive),
- draft contains misconception.bank_code (>=3 chars),
- draft contains misconception.bank_id (>=3 chars, non-numeric).

Pass conditions:
- misconception is None,
- misconception.fired is False,
- draft is a normal probe in tutee voice that doesn't quote internal strings.
"""
from __future__ import annotations

import pytest

from apollo.agent.leakage_judge import JudgeVerdict
from apollo.agent.output_filter import validate_or_raise
from apollo.errors import FilterRejectedError
from apollo.overseer.misconception import MisconceptionSignal
from apollo.subjects import load_concept


@pytest.fixture(scope="module")
def concept():
    return load_concept("fluid_mechanics", "bernoulli_principle")


@pytest.fixture
def stub_judge_clean():
    def _judge(*, draft, concept, history, kg_summary):
        return JudgeVerdict(
            leaks=False, offending_phrase=None, reason=None, confidence=0.0,
        )
    return _judge


def _signal(
    *,
    fired: bool = True,
    description: str = "treats fluid as having no density",
    bank_code: str | None = "no_density",
    bank_id: str | None = "42",
) -> MisconceptionSignal:
    return MisconceptionSignal(
        fired=fired,
        state="probe",
        description=description,
        bank_code=bank_code,
        bank_id=bank_id,
        probe="hmm, is density part of this?",
        rt_steps=("ask about density",),
    )


_HISTORY = [
    {"role": "user", "content": "Take A1 v1 equals A2 v2 with same fluid."},
]
_KG = "facts: A1 v1 = A2 v2 (label: continuity given by student)"


def test_clean_draft_with_no_misconception_passes(concept, stub_judge_clean):
    draft = "I'm not sure where to start — what should I do first?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=_HISTORY,
        kg_summary=_KG,
        judge=stub_judge_clean,
        misconception=None,
    )
    assert out == draft


def test_unfired_signal_does_not_block(concept, stub_judge_clean):
    draft = "treats fluid as having no density"  # would normally be a leak
    sig = _signal(fired=False)
    # Even though the offending text is literally present, an unfired
    # signal means there's nothing to leak.
    out = validate_or_raise(
        draft,
        concept=concept,
        history=_HISTORY,
        kg_summary=_KG,
        judge=stub_judge_clean,
        misconception=sig,
    )
    assert out == draft


def test_blocks_when_draft_contains_description(concept, stub_judge_clean):
    draft = (
        "I think the issue is the user treats fluid as having no density "
        "in their setup, so I'd like to ask..."
    )
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(
            draft,
            concept=concept,
            history=_HISTORY,
            kg_summary=_KG,
            judge=stub_judge_clean,
            misconception=_signal(),
        )
    assert "no density" in exc_info.value.rejected_term


def test_description_match_is_case_insensitive(concept, stub_judge_clean):
    draft = "TREATS FLUID AS HAVING NO DENSITY in this picture."
    with pytest.raises(FilterRejectedError):
        validate_or_raise(
            draft,
            concept=concept,
            history=_HISTORY,
            kg_summary=_KG,
            judge=stub_judge_clean,
            misconception=_signal(),
        )


def test_blocks_when_draft_contains_bank_code(concept, stub_judge_clean):
    """Even if the LLM doesn't echo the description, a paraphrase that
    happens to namedrop the bank code (e.g. an analytics-leak from
    debug logs being shown verbatim) must be blocked."""
    draft = "I should run the no_density check next, right?"
    with pytest.raises(FilterRejectedError) as exc_info:
        validate_or_raise(
            draft,
            concept=concept,
            history=_HISTORY,
            kg_summary=_KG,
            judge=stub_judge_clean,
            misconception=_signal(description="something else", bank_code="no_density"),
        )
    assert exc_info.value.rejected_term == "no_density"


def test_numeric_bank_id_does_not_falsely_block(concept, stub_judge_clean):
    """Surrogate integer ids would collide with real numeric content
    (e.g. 'set v=42'). The filter explicitly skips digit-only bank_ids
    — bank_code is the durable channel."""
    draft = "OK I'll set v = 42 m/s and see what happens."
    out = validate_or_raise(
        draft,
        concept=concept,
        history=_HISTORY,
        kg_summary=_KG,
        judge=stub_judge_clean,
        misconception=_signal(
            description="something unrelated",
            bank_code="x",  # too short, ignored
            bank_id="42",   # digits, ignored
        ),
    )
    assert out == draft


def test_clean_probe_in_tutee_voice_passes(concept, stub_judge_clean):
    draft = "Hmm, I'm wondering — does the same answer hold if speed changes?"
    out = validate_or_raise(
        draft,
        concept=concept,
        history=_HISTORY,
        kg_summary=_KG,
        judge=stub_judge_clean,
        misconception=_signal(),
    )
    assert out == draft


def test_short_bank_code_below_threshold_is_skipped(concept, stub_judge_clean):
    """Codes shorter than 3 chars must not block on substring (would
    false-fire on common letters)."""
    draft = "I'd like to ask about A and B."
    out = validate_or_raise(
        draft,
        concept=concept,
        history=_HISTORY,
        kg_summary=_KG,
        judge=stub_judge_clean,
        misconception=_signal(
            description="not present in draft",
            bank_code="A",  # 1 char, ignored
            bank_id=None,
        ),
    )
    assert out == draft
