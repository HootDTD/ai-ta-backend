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
        ("percent-second-person", "That puts you at 72% for the attempt.", "72%"),
        ("percent-anchored-rubric", "Only 40% of the rubric landed this time.", "40%"),
        ("percent-anchored-grader", "The grader credited 80 percent of it.", "80 percent"),
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


def test_a_frame_makes_its_whole_sentence_a_grading_sentence():
    """The percentage tier is sentence-anchored, and a frame IS the anchor: once
    a scoring word sits next to a number, every other percentage in that
    sentence belongs to the same statement — and the statement goes whole."""
    out = sanitize_narrative("You earned 80% on the causality topic and 100% on the definition.")
    assert out == ""


# ── 3a-i. review wave: a grade STATEMENT is removed whole ────────────────────
#
# Phrase deletion left broken prose on every realistic leak. These are the
# reviewer's five reproductions, asserted as EXACT output — the two tests that
# used substring assertions hid exactly this class, so nothing here may use one.


@pytest.mark.parametrize(
    "text",
    [
        "You earned 80% on causality and 100% on the definition.",
        "You scored 72% overall, which is a solid start.",
        "Overall you scored 65%; the friction term is the gap.",
        "Your score is 72, so keep pushing on the energy balance.",
        "Apollo credited 2 of the 3 topics, giving you 67%.",
    ],
)
def test_a_grade_statement_is_removed_whole_not_carved_out(text):
    """Dropping the sentence is safe because the prompt puts the substance in
    the other fields, and `enforce_narrative_consistency` supplies a fallback
    for any field this empties."""
    assert sanitize_narrative(text) == ""


def test_only_the_grade_sentence_goes_when_others_carry_feedback():
    """The whole point of sentence granularity: the neighbours survive intact,
    and the dropped sentence takes its terminator with it (no doubled stop)."""
    assert (
        sanitize_narrative(
            "You scored 72% overall. Your continuity explanation was the strongest part."
        )
        == "Your continuity explanation was the strongest part."
    )
    assert (
        sanitize_narrative("Great start. You earned a B+. Keep the energy balance explicit.")
        == "Great start. Keep the energy balance explicit."
    )


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


# ── 3b-i. review fix: content PERCENTAGES outside quotes (2026-08-23) ────────
#
# The first cut scrubbed every percentage unconditionally, on the theory that
# the result stays grammatical. Review reproduced otherwise, and the inventory
# above had no content-percentage case at all, so the loss was invisible. These
# are the reviewer's own reproductions, pinned so the anchoring cannot regress.
# None of these sentences carries a grading anchor, and no frame fires in one.


@pytest.mark.parametrize(
    ("why", "text"),
    [
        ("sentence-final percentage", "The tax rate is 30%."),
        ("sentence-initial percentage", "About 30% of the head is lost to friction."),
        ("a rate the problem supplies", "Use a 20% discount rate in the NPV calculation."),
        ("a certainty, not a grade", "There is a 100% chance the pressure drops."),
        ("a named statistical interval", "You correctly used the 90 percent confidence interval."),
        ("efficiency is a quantity", "The pump runs at 85% efficiency under full load."),
        ("a margin", "The firm holds a 12% margin on that product line."),
    ],
)
def test_content_percentages_outside_quotes_survive(why, text):
    assert sanitize_narrative(text) == text, why


# ── 3b-ii. review fix: the grade/rating NOUN frames (2026-08-23) ─────────────
#
# F3/F4 had every element optional but the number, so they degenerated to
# "grade noun near a number". "Pump/motor rating" is ordinary vocabulary in the
# very fluids domain these fixtures use. The connector is now mandatory, the
# number must close its clause or carry a GRADE unit, and the reversed frame
# takes a bare number for `grade`/`rating` (so "a 5% grade" is a road, not a
# verdict).


@pytest.mark.parametrize(
    ("why", "text"),
    [
        ("a motor rating with a subject-matter unit", "The motor rating is 5 kW at full load."),
        ("a rating in kVA", "A rating of 500 kVA is plenty for that load."),
        ("a road grade as a percentage", "The road has a 5% grade over that stretch."),
        ("a bolt grade, no connector", "Use grade 8 bolts for the flange."),
        ("a pump rating", "The pump has a rating of 12 bar before it cavitates."),
    ],
)
def test_grade_and_rating_nouns_in_content_survive(why, text):
    assert sanitize_narrative(text) == text, why


# ── 3b-iv. review fix round 2: DECIMAL measurements (2026-08-23) ─────────────
#
# The round-1 negatives above all used integer quantities, which hid two bugs
# that only a decimal exposes: the sentence splitter treated `7.5`'s point as a
# terminator, and `_GRADE_TAIL` accepted that same point as a clause close after
# backtracking `_GRADE_NUM` to the integer part. Both served the fragment
# ".5 kW". A decimal is the common case for a real measurement, so this family
# gets its own block.


@pytest.mark.parametrize(
    ("why", "text"),
    [
        ("a decimal motor rating", "The motor rating is 7.5 kW at full load."),
        ("a decimal pressure rating", "The pump has a rating of 3.5 bar before it cavitates."),
        ("a decimal safety factor", "The safety factor rating is 2.5 for that beam."),
        ("a decimal quantity mid-sentence", "The 1.5 kg mass slides 2.5 m before it stops."),
    ],
)
def test_decimal_measurements_survive(why, text):
    assert sanitize_narrative(text) == text, why


def test_a_decimal_grade_is_scrubbed_whole():
    """The recall half of the same bug: the splitter cut `87.5%` in two and the
    frame ate only `87`, leaking a mangled `5%` back into the served prose."""
    assert sanitize_narrative("Your score is 87.5% overall.") == ""
    out = sanitize_narrative("Your score came out to 3.5. Next, the 20% rate matters.")
    assert "3.5" not in out and "5." not in out
    # ...and the content percentage in the FOLLOWING sentence is untouched: the
    # anchor is per sentence, and the splitter no longer merges the two.
    assert out == "Next, the 20% rate matters."


# ── 3b-v. review fix round 2: the second-person standing frame ───────────────
#
# `puts/leaves/has you at NN%` ran unanchored, so any subject qualified and a
# physical reading was mangled. The subject is now a bare pronoun.


@pytest.mark.parametrize(
    ("why", "text"),
    [
        ("a pump curve operating point", "The pump curve puts you at 80% efficiency."),
        ("a capacity reading", "That operating point leaves you at 60% of capacity."),
        ("a conversion on an isotherm", "Following the isotherm has you at 40% conversion."),
        # Round 3: the SAME readings one anaphor later. Prose names the subject in
        # one sentence and continues with that/this/it in the next, so this is the
        # common form, and the pronoun restriction alone let all three through —
        # dropping subject AND verb ("It puts you at 80% efficiency." -> ".").
        ("an anaphoric pump reading", "It puts you at 80% efficiency."),
        ("an anaphoric capacity reading", "This leaves you at 60% of capacity."),
        ("a pronoun subject with a named quantity",
         "That puts you at 80% efficiency on the pump curve."),
        ("an anaphoric flow fraction", "It has you at 30% of the design flow."),
        ("an anaphoric margin", "This puts you at 15% margin on the deal."),
    ],
)  # fmt: skip
def test_a_following_noun_keeps_the_you_at_reading_physical(why, text):
    """The TAIL is what separates a grade from a measurement.

    A percentage followed by a noun or an of-phrase measures a NAMED QUANTITY;
    only one that closes its clause (or carries an adverbial of scope) is the
    student's standing. Restricting the subject to a pronoun was not enough —
    the anaphoric form is exactly the one prose reaches for.
    """
    assert sanitize_narrative(text) == text, why


@pytest.mark.parametrize(
    "text",
    [
        "That puts you at 72%.",
        "That puts you at 72% overall.",
        "That puts you at 72% for the attempt.",
        "You're at 60% on this topic.",
        "You are at 40% so far.",
    ],
)
def test_a_standing_that_closes_its_clause_is_still_a_grade(text):
    """The other direction, pinned too: trailing punctuation or an adverbial of
    scope keeps the frame firing, so tightening the tail cost no recall."""
    assert "%" not in sanitize_narrative(text)


def test_a_standing_statement_goes_whole():
    """Same rule as every other frame since the review wave: the sentence goes,
    not just the phrase, so no "which is a start." fragment ships."""
    assert sanitize_narrative("It puts you at 65%, which is a start.") == ""


# ── 3b-iii. the residuals, pinned rather than omitted ────────────────────────


def test_accepted_content_loss_a_parenthetical_percentage():
    """ACCEPTED LOSS, pinned so it stays visible.

    A parenthetical holding nothing but a percentage is scrubbed with no
    sentence anchor, because it is the exact staging leak shape
    ("You covered causality well (80%).") and content percentages are written
    inline rather than parenthesised. A content percentage that IS parenthesised
    loses its number — the cost of keeping that one annotation shape. The
    sentence stays true and grammatical, which is why the trade is taken.
    """
    assert sanitize_narrative("The pump efficiency (85%) held steady.") == (
        "The pump efficiency held steady."
    )


@pytest.mark.parametrize(
    "text",
    [
        "You were graded on the section covering the 30% tax rate.",
        "The rubric asks for the 45% split between the two funds.",
        "Your score depends on whether you used the 12% discount rate.",
    ],
)
def test_accepted_content_loss_a_content_percentage_in_a_grading_sentence(text):
    """ACCEPTED LOSS, pinned so it stays visible.

    Sentence anchoring is coarser than adjacency: a CONTENT percentage that
    happens to share a sentence with a grading word takes the WHOLE sentence
    with it since the review wave. That is a bigger loss than the "number only"
    version this test used to pin, and it is the price of never shipping broken
    prose — carving the number out of these three left them grammatical, but the
    same carve left "on causality and on the definition." one sentence over, and
    the two cases are not separable by regex. The other prose fields still carry
    the feedback, and an emptied field gets a fallback.
    """
    assert sanitize_narrative(text) == ""


def test_accepted_residual_leak_a_percentage_with_no_anchor():
    """ACCEPTED RESIDUAL LEAK, pinned so it stays visible.

    "rated" cannot join the anchor set: "the pump is rated at 95% efficiency" is
    exactly the fluids content this scrub must not touch. So a grade stated with
    an unanchored verb survives the backstop. That is the precision-over-recall
    trade the brief demands, and it is acceptable because the PROMPT is the
    primary control — it supplies no percentage anywhere and forbids numeric
    grades outright, so there is no number for the narrator to recite.
    """
    text = "Apollo rated your teaching 65 percent."
    assert sanitize_narrative(text) == text


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


def test_the_exemption_does_not_shelter_prose_in_a_LATER_sentence():
    """The quoted span survives; the grade statement beside it does not. Split
    across two sentences because a grade statement is now removed WHOLE — see
    `test_accepted_loss_a_quote_sharing_a_sentence_with_a_grade` for what that
    costs when the two share one sentence."""
    out = sanitize_narrative('Great: "we hit 30% margin". Then you scored 88%.')
    assert out == 'Great: "we hit 30% margin".'


def test_accepted_loss_a_quote_sharing_a_sentence_with_a_grade():
    """ACCEPTED LOSS, pinned so it stays visible.

    Whole-sentence removal and the quotes exemption meet here: a student span
    quoted INSIDE a grade statement goes with the statement. Keeping the
    sentence to save the quote would ship exactly the broken residue the review
    wave removed ("Great: \"we hit 30% margin\" and then."), and the quote is
    still served in its own exact-gated `quote` field."""
    assert sanitize_narrative('Great: "we hit 30% margin" and then you scored 88%.') == ""


def test_an_unterminated_quote_fails_closed():
    """An odd quote count is not a quotation — scrubbing the tail is the safe
    reading, because the alternative exempts the whole rest of the field."""
    out = sanitize_narrative('You said "I got 80% of it')
    assert "80%" not in out


def test_the_flattened_narrative_never_serves_a_bare_next_step_label():
    """The empty-field edge this change opened, closed at the other end.

    `sanitize_narrative` returns "" for a field that was nothing but a numeric
    grade statement, and `_flatten_topic_feedback` used to join unconditionally
    — serving the label "Next step: " with nothing behind it. Empty parts are
    now dropped, the way `_deterministic_recap` already drops empty appender
    output.
    """
    from apollo.overseer.diagnostic import _flatten_topic_feedback

    flattened = _flatten_topic_feedback(
        {
            "headline": "",
            "topic_feedback": [
                {"canonical_key": "eq1", "note": "Make the continuity step explicit."},
                {"canonical_key": "c1", "note": "   "},
            ],
            "recap": [],
            "next_step": "",
        }
    )
    assert flattened == "Make the continuity step explicit."
    assert "Next step:" not in flattened

    # ...and a populated payload still flattens in the unchanged order.
    assert _flatten_topic_feedback(
        {
            "headline": "Solid start.",
            "topic_feedback": [{"canonical_key": "eq1", "note": "Nice link."}],
            "recap": ["You negotiated 2 entries with Apollo."],
            "next_step": "Walk through continuity out loud.",
        }
    ) == (
        "Solid start.\n\nNice link.\n\nYou negotiated 2 entries with Apollo."
        "\n\nNext step: Walk through continuity out loud."
    )


def test_a_fully_credited_attempt_still_gets_a_fallback_for_an_emptied_field():
    """The other half of the whole-sentence rule (review wave).

    `enforce_narrative_consistency` early-returns when nothing is uncredited, so
    an emptied headline/next step used to be served BLANK on exactly the
    attempts where the model most wants to headline the number — a 100% attempt.
    The fallback now runs before that early return, and it uses the
    all-credited wording, because `FALLBACK_HEADLINE` ("what Apollo did not get")
    would be a lie on a fully credited card.
    """
    credited = _topic("eq1", 1.0, "covered")
    scrubbed_headline = sanitize_narrative("You scored 100% overall.")
    assert scrubbed_headline == ""

    repaired = nc.enforce_narrative_consistency(
        {
            "headline": scrubbed_headline,
            "topic_feedback": [{"canonical_key": "eq1", "note": "You nailed the streamline."}],
            "next_step": "",
        },
        topics=[credited],
    )
    assert repaired["headline"] == nc.FALLBACK_HEADLINE_CREDITED
    assert repaired["next_step"] == nc.FALLBACK_NEXT_STEP_CREDITED
    assert repaired["headline"] and repaired["next_step"]
    # The all-credited wording, never the uncredited one.
    assert repaired["headline"] != nc.FALLBACK_HEADLINE


def test_a_fully_credited_attempt_with_real_prose_is_untouched():
    """The early return still returns the payload unchanged when nothing is
    blank — the fallback is a repair, not a rewrite."""
    payload = {
        "headline": "Your explanation held together end to end.",
        "topic_feedback": [{"canonical_key": "eq1", "note": "Nice link."}],
        "next_step": "Try it again without notes.",
    }
    assert nc.enforce_narrative_consistency(payload, topics=[_topic("eq1", 1.0)]) == payload


def test_the_ledger_path_residue_is_swept_not_served():
    """Regression pin for a pre-existing output the repair block changed.

    Before the repair block, the ledger scrub left "Your weight 0.5." as "Your."
    — a bare pronoun sentence. It now sweeps to "". Unpinned until the review
    wave caught it; the change is correct, so it is pinned rather than reverted.
    """
    assert sanitize_narrative("Your weight 0.5.") == ""


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
