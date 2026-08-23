"""Study-prep 2026-08-23 (user ruling): no numeric grade in student-facing prose.

Students see a proficiency BAND, so a score, a percentage, a points tally or a
letter grade must not appear anywhere they read. The UI stopped rendering the
number; the last leak was the LLM-authored narrative, which had TWO sources:

1. the prompt handed the narrator the number — every topic line ended
   ``— {pct}%`` and a ``SCORE CONSISTENCY`` block told the prose to track those
   percentages. That is the PRIMARY control and this file pins its replacement:
   a status word, and a ``CREDIT CONSISTENCY`` block that says the same thing in
   words;
2. ``sanitize_narrative`` scrubbed only ledger terms paired with a 0-1 decimal
   and DELIBERATELY preserved whole-number percentages. That is the BACKSTOP and
   this file is its pattern inventory: one positive per family, and — the half
   that actually matters — the negatives proving what each family leaves alone.

The design rule is precision over recall. A scrub that mangles physics or
business content is worse than a residual leak, so a BARE INTEGER is never
touched (it is a step, a problem id, a section, a year, a quantity, a value the
student computed) and only two shapes go on sight: a percentage, and a number
carrying an explicit grade unit or a score/grade collocation. Removing a
percentage stays cheap even when it was content — "the 12% growth rate" degrades
to "the growth rate", still true and still grammatical.
"""

from __future__ import annotations

import re

import pytest

from apollo.overseer import narrative_consistency as nc
from apollo.overseer.topic_narrative import (
    PRAISE_FLOOR,
    build_topic_narrative_prompt,
    sanitize_narrative,
)
from apollo.overseer.topic_score import TopicCredit, TopicScoreResult

pytestmark = pytest.mark.unit

_TOPIC_LINE_RE = re.compile(r"^- Topic canonical_key=\"[^\"]+\", name=\"[^\"]*\": (?P<status>.+)$")


def _topic(
    key: str,
    credit: float,
    status: str = "covered",
    *,
    name: str | None = "Apply the Bernoulli equation",
    weight: float = 0.5,
) -> TopicCredit:
    return TopicCredit(
        canonical_key=key,
        display_name=name,
        credit=credit,
        status=status,  # type: ignore[arg-type]
        weight=weight,
        misconceptions=(),
    )


def _result(*topics: TopicCredit) -> TopicScoreResult:
    return TopicScoreResult(
        score=72,
        letter="B-",
        coverage_component=0.72,
        misconception_dock=0.0,
        topics=topics or (_topic("eq1", 0.9),),
    )


def _topic_statuses(user: str) -> list[str]:
    return [
        match.group("status") for line in user.splitlines() if (match := _TOPIC_LINE_RE.match(line))
    ]


# ── 1. the prompt: status words, no number of score origin ───────────────────


def test_topic_lines_carry_a_status_word_and_no_digits():
    result = _result(
        _topic("eq1", 1.0, "covered", name="Apply the Bernoulli equation"),
        _topic("p1", 0.3, "partial", name="Use continuity between the two areas"),
        _topic("c1", 0.0, "missing", name="State the incompressible condition"),
    )
    _system, user = build_topic_narrative_prompt(result, problem_text="Water flows.")

    assert _topic_statuses(user) == ["covered", "partially covered", "missing"]
    # No percentage anywhere, and no bare digit on any topic line either.
    assert "%" not in user
    for status in _topic_statuses(user):
        assert not re.search(r"\d", status)


def test_a_number_in_the_problem_text_still_reaches_the_prompt():
    """The ban is on GRADE numbers. Subject-matter numbers are the narrator's
    raw material — a prompt that scrubbed them would produce vaguer feedback,
    which is the opposite of the goal."""
    _system, user = build_topic_narrative_prompt(
        _result(),
        problem_text="A 12 kg block slides 3 m down a 30 degree incline.",
        student_utterances=("the block loses 4 J to friction",),
    )
    assert "12 kg" in user and "30 degree" in user
    assert "loses 4 J to friction" in user


def test_the_consistency_block_keeps_its_job_in_status_words():
    """c2 (pilot): prose must never praise a topic the ledger zeroed. The rule
    survives the currency change — only the words it keys on moved."""
    system, _user = build_topic_narrative_prompt(_result(), problem_text="p")
    lowered = " ".join(system.lower().split())

    assert "credit consistency" in lowered
    assert '"covered" is the only status that was credited' in lowered
    assert '"partially covered" was not credited' in lowered
    assert '"missing" was not credited at all' in lowered
    assert "name the missing idea explicitly" in lowered
    assert "the headline and the next step follow the same rule" in lowered
    assert "score consistency" not in lowered


# ── 2. the consistency PROPERTY: the word means what the code gate means ─────


@pytest.mark.parametrize("credit", [round(0.05 * step, 2) for step in range(21)])
def test_status_word_agrees_with_the_praise_gate_at_every_credit(credit):
    """THE preservation argument, machine-checked.

    Before this change the topic line carried the percentage, and
    ``narrative_consistency`` (which strips praise below ``PRAISE_FLOOR`` of
    CREDIT) and the prompt rule ("a topic below 60% was NOT credited") were
    keyed on the SAME number, so they could not disagree. With the number gone
    the status word is the only signal the narrator has, so it must split
    credited from uncredited at the identical point — otherwise the model writes
    praise the gate then deletes, or a "you never taught this" line on a topic
    the gate happily credits.
    """
    topic = _topic("eq1", credit)
    _system, user = build_topic_narrative_prompt(_result(topic), problem_text="p")
    (word,) = _topic_statuses(user)

    assert (word == "covered") is (not nc._is_uncredited(topic))
    assert (word == "covered") is (credit >= PRAISE_FLOOR)
    assert (word == "missing") is (credit == 0.0)
    assert word in {"covered", "partially covered", "missing"}


def test_no_status_word_praises_a_zeroed_topic():
    """The c2 shape end to end: a zeroed topic reads "missing" to the narrator
    and is ``uncredited`` to the gate, so neither surface can credit it."""
    zeroed = _topic("c1", 0.0, "missing", name="State the incompressible condition")
    _system, user = build_topic_narrative_prompt(_result(zeroed), problem_text="p")

    assert _topic_statuses(user) == ["missing"]
    assert nc._is_uncredited(zeroed)


def test_the_word_follows_credit_even_when_the_adjudicator_status_disagrees():
    """``topic_score._credit_for_node`` derives status and credit from different
    inputs (the coverage verdict vs ``procedure_scores``), so ``covered`` at 0.4
    and ``missing`` at 0.7 are both reachable. Rendering ``TopicCredit.status``
    verbatim would hand the narrator the half the gate does NOT use."""
    generous = _topic("eq1", 0.4, "covered")
    stingy = _topic("eq2", 0.7, "missing", name="Relate the two energy heads")
    _system, user = build_topic_narrative_prompt(_result(generous, stingy), problem_text="p")

    assert _topic_statuses(user) == ["partially covered", "covered"]
    assert nc._is_uncredited(generous) and not nc._is_uncredited(stingy)


def test_unprobed_is_passed_through_untouched():
    """ "Apollo never asked" is neither a credit verdict nor a teaching gap; the
    gate gives it its own blame-free treatment, so the word must survive."""
    unprobed = _topic("eq9", 0.0, "unprobed", name="An idea Apollo never asked about",
                      weight=0.0)  # fmt: skip
    _system, user = build_topic_narrative_prompt(_result(unprobed), problem_text="p")
    assert _topic_statuses(user) == ["unprobed"]


# ── 3a. the scrub: one positive per pattern family ───────────────────────────


@pytest.mark.parametrize(
    ("family", "text", "gone"),
    [
        ("percent", "That puts you at 72% for the attempt.", "72%"),
        ("percent-word", "Apollo rated your teaching 65 percent.", "65 percent"),
        ("percent-paren", "You covered causality well (80%).", "80%"),
        ("out-of-100", "That works out to 72 out of 100.", "72 out of 100"),
        ("slash-100", "Your final tally was 72/100.", "72/100"),
        ("strong-verb-bare", "You scored 72 this time.", "72"),
        ("strong-verb-unit", "You scored 72% overall.", "72"),
        ("weak-verb-unit", "You earned 18 points on this attempt.", "18 points"),
        ("weak-verb-lost", "You lost 12 points for the missing step.", "12 points"),
        ("weak-verb-worth", "Your explanation is worth 18 points out of 25.", "18 points"),
        ("noun-of", "Your score is 72, which is a solid start.", "72"),
        ("noun-colon", "Overall grade: 84%.", "84"),
        ("noun-reversed", "That is an 85 grade for the explanation.", "85"),
        ("noun-came-out-to", "Nice work: your grade came out to 84.", "84"),
    ],
)
def test_each_grade_family_is_scrubbed(family, text, gone):
    out = sanitize_narrative(text)
    assert gone not in out, family
    assert sanitize_narrative(out) == out, f"{family} is not idempotent"


def test_a_leak_mid_sentence_keeps_the_feedback_around_it():
    """Phrase deletion, not sentence deletion: the real feedback in the same
    sentence as the leak is what the student came for."""
    out = sanitize_narrative(
        "You explained continuity clearly and scored 88% overall, so keep it up."
    )
    assert out == "You explained continuity clearly, so keep it up."


def test_a_field_that_was_only_a_grade_statement_goes_empty():
    """Nothing in "You scored 72% overall." is feedback, so nothing is served;
    ``narrative_consistency`` substitutes its fallback headline / next step.
    Debris like a bare "You." is swept rather than shipped."""
    assert sanitize_narrative("You scored 72% overall.") == ""
    assert sanitize_narrative("Your overall grade: 84%.") == ""


# ── 3b. the scrub: what every family provably leaves alone ───────────────────


@pytest.mark.parametrize(
    ("why", "text"),
    [
        ("step and equation references", "Revisit step 2 and equation (3) next time."),
        ("problem id", "Problem 449 asks for the same idea in reverse."),
        ("section reference", "Walk through section 4 and problem 12 again."),
        ("currency quantity", "The $1,200 depreciation charge belongs in year 3."),
        ("a year", "Toffler named it in his 1970 book."),
        ("a physical quantity", "The 12 kg block slides 3 m before it stops."),
        ("index points are not grade points", "The index fell 200 points after the news."),
        ("basis points", "A 50 basis points cut changes the discount factor."),
        ("percentage points", "That is a 3 percentage points swing in margin."),
        ("a counted subset", "You got 3 of the 5 steps in the right order."),
        ("spelled-out points", "You made two good points about scope."),
        ("scoring word, no number", "No points were docked for errors."),
        ("ledger decimals out of range", "The weight 1.5 factor acts on a weight 2.5 kg mass."),
        ("inline math", "Bernoulli: $P + 0.5 \\rho v^2 = const$ along a streamline."),
        ("physics weight", "The weight of the fluid column ($w = mg$) pushes down."),
        ("the recap line", "You negotiated 3 entries with Apollo: 1 paraphrased, 2 skipped."),
        (
            "the misconception recap line",
            "During the conversation, you and Apollo navigated through 2 suspected "
            "misconceptions; you resolved 1 of them.",
        ),
        ("the unavailable placeholder",
         "[Diagnostic narrative unavailable — the grade above is still accurate.]"),
        ("short genuine prose", "That's it."),
        ("a grounded citation marker", "Re-read the pressure section [Lecture 4, p. 12]."),
        ("`mark` as an imperative, not a grade noun", "Mark 3 key assumptions explicitly."),
        ("`marked` on a bare count", "You marked 3 steps as done before checking them."),
        ("a road grade in degrees", "The grade of the ramp is 3 degrees at the top."),
    ],
)  # fmt: skip
def test_content_numbers_survive_untouched(why, text):
    assert sanitize_narrative(text) == text, why


@pytest.mark.parametrize("token", ["beginner", "intermediate", "advanced"])
def test_band_words_still_pass(token):
    """Task 2's contract, re-pinned from the scrub side: the vocabulary that
    REPLACED the number must not be eaten by the scrub that removed it."""
    for text in (
        f"You are at the {token} level on this topic.",
        f"Nice work ({token} on continuity) — keep going.",
        f"{token.capitalize()}: you explained the pressure term clearly.",
    ):
        assert sanitize_narrative(text) == text


def test_unchanged_input_is_returned_byte_identical_across_paragraphs():
    text = (
        "You covered causality well and grounded it in the pipe example.\n\n"
        "You negotiated 3 entries with Apollo: 1 paraphrased.\n\n"
        "Next step: walk through the continuity step out loud."
    )
    assert sanitize_narrative(text) == text


def test_paragraph_structure_survives_a_scrub():
    out = sanitize_narrative("You covered causality well (80%).\n\nNext step: explain overload.")
    assert out == "You covered causality well.\n\nNext step: explain overload."


# ── 3c. the quotes exemption (deliberate — see `_scrub_outside_quotes`) ──────


def test_a_percentage_inside_a_student_quote_survives():
    """QUOTES ARE EXEMPT. Everything the narrative quotes is the STUDENT'S own
    words: the prompt allows a quote only from a verbatim "You said" span and
    ``diagnostic._gate_topic_quote`` enforces exact equality with that span in
    code. A number the student said is subject-matter content, not the system
    disclosing a grade."""
    text = 'You said "I got about 80% of the way there" and that instinct is right.'
    assert sanitize_narrative(text) == text


def test_a_score_collocation_inside_a_quote_survives_too():
    text = 'Your words: "the firm scored 72 on the ESG index" are exactly right.'
    assert sanitize_narrative(text) == text


def test_the_exemption_does_not_shelter_prose_after_the_quote():
    out = sanitize_narrative('Great: "we hit 30% margin" and then you scored 88%.')
    assert '"we hit 30% margin"' in out
    assert "88%" not in out


def test_an_unterminated_quote_fails_closed():
    """An odd quote count is not a quotation — scrubbing the tail is the safe
    reading, because the alternative exempts the whole rest of the field."""
    out = sanitize_narrative('You said "I got 80% of it')
    assert "80%" not in out


def test_the_gated_quote_field_drops_rather_than_mangles():
    """``diagnostic._gate_topic_quote`` serves the quote only when the sanitizer
    round-trips it, so a student span the scrub would rewrite is dropped whole
    rather than served altered. Pinned here because the quote arrives WITHOUT
    its surrounding quotation marks, so the in-prose exemption cannot cover it.
    """
    from apollo.overseer.diagnostic import _gate_topic_quote

    plain = "the pipe narrows so pressure drops"
    assert _gate_topic_quote(plain, evidence_span=plain, canonical_keys=()) == plain

    scored = "I scored 80% on the practice set"
    assert _gate_topic_quote(scored, evidence_span=scored, canonical_keys=()) is None
