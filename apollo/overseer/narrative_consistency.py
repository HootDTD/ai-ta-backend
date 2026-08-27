"""Deterministic gate: the served narrative may never contradict the verdicts.

2026-08-07 bimodal-fix spec §5 P2.1 (defect U2). The narrator is already built
FROM the ledger (``topic_narrative.build_topic_narrative_prompt`` renders every
topic's status and percentage), but it is still an LLM: in prod it praised
content the coverage verdict had zeroed — attempt 154 (F/28) opened with "You
clearly contrasted democratization and centralization… gave concrete examples"
for a node graded ``missing``. Prompt rules alone cannot make that impossible,
so the served prose passes one CODE gate after generation.

Contract enforced here, per topic the ledger did not credit:

1. a sentence that claims credit and names no gap ("pure praise") is stripped —
   from that topic's note, and from the headline/next step when the sentence is
   demonstrably ABOUT that topic (see :func:`_names_uncredited_topic`);
2. a topic that counted toward the grade always ends up with its gap named — a
   deterministic sentence is appended when nothing the model wrote names one.
   That sentence quotes the topic's reference wording for at most
   :data:`MAX_REFERENCE_NAME_QUOTES` topics per payload (D2's budget, imported
   from the scorer); past it the gap is named without the wording;
3. nothing else changes: with every topic credited the payload is returned
   byte-identical.

Three carve-outs keep the gate from punishing the student for something that is
not a teaching gap:

* **Hoot-assisted topics** (INTERACTION5) carry a flat POLICY cap of 0.5 —
  unconditionally below :data:`PRAISE_FLOOR` — applied by
  ``aside_penalty.apply_aside_caps`` on top of whatever the adjudicator found.
  Sub-floor credit there is a penalty, not an absence of evidence, so an
  assisted topic with ANY credit is exempt. Only ``credit == 0`` (which the cap
  can produce only from a pre-cap 0) is treated as uncredited.
* **Zero-weight topics** — a graded node excluded from the denominator (P1.2b
  ``unprobed``: Apollo never asked about it this attempt) did not count toward
  the grade, so it never receives a "you did not teach this" sentence. Praise of
  it is still stripped (it was not credited either), and a note left empty by
  that strip gets a neutral, blame-free replacement.
* **Headline / next step** are only edited on strong evidence that the sentence
  is about an uncredited topic; the topic-name overlap test is deliberately
  strict, because emptying a one-sentence headline replaces it wholesale.
* **Flagged claims** (P3.2 L3) are CONSISTENT with credit, not evidence against
  it: a corroborated wrongness finding requires ``credit >= 0.6``, so a topic
  carrying one is credited, is never in ``uncredited``, and keeps its praise and
  its untouched note. The finding is narrated as its own separate line by
  ``topic_narrative``. The only coupling here is the shared reveal budget —
  see :data:`MAX_REFERENCE_NAME_QUOTES`.

Pure, total, and idempotent — string/structural only, no second LLM call, no
IO. Runs AFTER ``sanitize_narrative`` so it sees exactly the served text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from typing import Any

from apollo.overseer.topic_narrative import (
    PRAISE_FLOOR,
    humanize_key,
    nameable_misconception_keys,
)
from apollo.overseer.topic_score import MAX_REFERENCE_TEXT_REVEALS, TopicCredit

_LOG = logging.getLogger(__name__)

# Below this credit the adjudicator did not find sufficient evidence, so prose
# may not credit the student for it. Mirrors the adjudication anchor set
# {0, 0.3, 0.6, 0.85, 1.0} (P1.1, 0.3 added 2026-08-24): 0.6 is still the lowest
# anchor that means "landed", and the floor deliberately did NOT move with the
# new anchor — 0.3 is the bare/tangential-mention anchor, which is exactly the
# shape this gate exists to keep out of praise.
#
# Re-exported, not declared: since study-prep 2026-08-23 the topic line renders a
# status WORD instead of a percentage, so `topic_narrative._credit_status` has to
# split credited from uncredited at the exact same credit this gate does. It owns
# the literal (this module already imports from it — the reverse would be a
# cycle); the public name stays here, where every caller already reads it.

# The gate's own sentences quote the topic's display name — which IS the
# reference solution's wording — so they are a reveal channel exactly like D2's
# `TopicCredit.reference_text`, and they must obey the SAME per-attempt budget.
# Imported, not re-declared: on a wholly-failed attempt every graded topic is
# uncredited, so an uncapped gate would append one quoted reference clause per
# node and hand back the whole graded reference solution in the narrative while
# `topics[]` was carefully capped at two — one payload, two answers to "how much
# of the reference may this student see". `restart_problem` is still reachable
# from REPORT and browse is best-grade-wins, so that union is recitable back
# into a grade. Past the budget the gap is still named, without the wording.
#
# P3.2 L3 (2026-08-12): this is now the budget for the WHOLE narrative, not just
# for this gate. A named misconception (`topic_narrative`) is a third channel
# into the same payload, so it spends from the same two slots and the gate takes
# only what is left — see `_quotable_keys`. Below wrongness level 3 no topic
# carries a misconception, so the gate keeps the full budget and every output is
# byte-identical to the pre-P3.2 build.
MAX_REFERENCE_NAME_QUOTES = MAX_REFERENCE_TEXT_REVEALS

FALLBACK_HEADLINE = "Here is what Apollo did not get from your teaching yet."
# Used when a prose field arrives EMPTY on an attempt with nothing uncredited —
# `FALLBACK_HEADLINE` would be a lie there. Reachable since the grade scrub
# started dropping whole sentences (study-prep 2026-08-23): a headline that was
# nothing but "You scored 100% overall." is scrubbed to "", and a fully credited
# attempt is exactly where the model most wants to headline the number.
FALLBACK_HEADLINE_CREDITED = "Here is how your explanation landed."
FALLBACK_NEXT_STEP_CREDITED = (
    "Teach this one again from memory and see how much you can add without your notes."
)
# Topic names are the reference solution's own wording (prod median 220 chars,
# always sentence-shaped), so they are quoted and shortened rather than dropped
# inline — the flattened back-compat narrative has no topic headings, so the
# gap sentence has to say WHICH idea is missing on its own.
_ZERO_GAP = (
    'Apollo never got this from your teaching: "{name}" — walk through it explicitly next time.'
)
_PARTIAL_GAP = 'Only part of this landed: "{name}" — make the rest explicit next time.'
# Past MAX_REFERENCE_NAME_QUOTES the gap is still named — the note is attached to
# its own topic, so the student still knows WHICH one — just without quoting more
# of the reference wording back at them.
_ZERO_GAP_NO_NAME = (
    "Apollo never got this idea from your teaching — walk through it explicitly next time."
)
_PARTIAL_GAP_NO_NAME = "Only part of this landed — make the rest explicit next time."
_NEXT_STEP = 'Walk Apollo through this in your own words: "{name}".'
# Used instead of _NEXT_STEP when that same topic's note already quotes the
# reference wording, so the student never reads the identical clipped clause
# twice in one card.
_NEXT_STEP_NO_QUOTE = "Teach the idea Apollo did not get back in your own words, start to finish."
# A zero-weight topic was removed from the grade, so its note may not blame the
# student; this replaces a note that pure-praise stripping emptied.
_UNSCORED_NOTE = "Apollo did not ask about this one, so it did not count toward your grade."
_NAME_QUOTE_CHARS = 90

# A credit claim is second person + an accomplishment verb ("you clearly
# contrasted…", "you've shown…"), allowing two filler words in between.
_CREDIT_VERBS = (
    r"explained|described|showed|shown|covered|connected|contrasted|compared|identified|named|"
    r"defined|walked|laid out|gave|provided|demonstrated|established|articulated|captured|"
    r"nailed|taught|applied|linked|tied|highlighted|addressed|mentioned|spelled out|traced|"
    r"derived|justified|illustrated|framed|grounded|unpacked|got"
)
_CREDIT_CLAIM_RE = re.compile(
    rf"\byou(?:'ve|'d| have| had)?\s+(?:\w+\s+){{0,2}}(?:{_CREDIT_VERBS})\b",
    re.IGNORECASE,
)
# Bare "well" is deliberately absent ("as well as" is not praise).
_PRAISE_WORDS = (
    r"clear(?:ly)?|nice(?:ly)?|great|strong(?:ly)?|solid(?:ly)?|excellent|impressive(?:ly)?|"
    r"thorough(?:ly)?|confident(?:ly)?|effective(?:ly)?|successful(?:ly)?|accurate(?:ly)?|"
    r"precise(?:ly)?|sharp(?:ly)?|spot on|well done|done well|good (?:job|work)|nice work"
)
_PRAISE_WORD_RE = re.compile(rf"\b(?:{_PRAISE_WORDS})\b", re.IGNORECASE)
# Negation, contrast, and forward-looking cues — the sentence names a gap.
_GAP_CUES = (
    r"didn't|did not|don't|do not|doesn't|does not|never|not|no|without|missing|missed|"
    r"skip(?:ped)?|left out|absent|unclear|isn't|is not|wasn't|was not|weren't|were not|"
    r"haven't|hasn't|hadn't|need(?:s|ed)? to|next time|try|would|could|should|if you|"
    r"make sure|expand|spell out|stop(?:ped)? short|only|but|however|though|yet|instead|"
    r"still|rather than|beyond|unstated|implicit|assum(?:e|ed|ing)|so far|short of"
)
_GAP_CUE_RE = re.compile(rf"\b(?:{_GAP_CUES})\b", re.IGNORECASE)
# A revision instruction is a gap statement even with no negation in it
# ("Make the continuity step explicit.").
_IMPERATIVES = {
    "add", "address", "avoid", "back", "bring", "build", "clarify", "compare", "connect",
    "contrast", "define", "derive", "describe", "draw", "expand", "explain", "finish", "focus",
    "follow", "give", "go", "ground", "highlight", "include", "justify", "keep", "lay", "link",
    "list", "make", "mention", "name", "note", "outline", "pick", "point", "practice", "put",
    "restate", "return", "review", "revisit", "say", "show", "sketch", "spell", "start", "state",
    "take", "teach", "tell", "tie", "trace", "treat", "try", "unpack", "use", "walk", "work",
    "write",
}  # fmt: skip

_ABBREVIATIONS = {"e.g.", "i.e.", "vs.", "etc.", "cf.", "dr.", "mr.", "ms.", "fig.", "eq."}
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")
_NAME_STOPWORDS = frozenset(
    {"that", "this", "with", "from", "into", "then", "than", "them", "they", "when", "what",
     "which", "your", "here", "there", "about", "these", "those", "does", "also", "between"}
)  # fmt: skip
_DISTINCTIVE_LEN = 6
_SIGNIFICANT_LEN = 4
# How many topic-name words a headline must share with an uncredited topic — and
# with NO credited topic — before its praise is deleted. One shared domain word
# is not evidence when the name is a whole reference sentence: prod graded
# display names run 4-29 distinctive words (median 15, 199 chars), so a single
# 6+ char word like "information" or "privacy" fires on any sentence in the same
# subject area, including accurate praise of a FULLY credited node. Measured
# over the 14 exported prod problems with 2+ graded nodes (240 ledger-supported
# praise headlines, one node credited 1.0 and the rest 0): the single-word rule
# false-stripped 139/240 = 57.9%; the rule below false-stripped 0/240 while
# still catching 252/252 of the attempt-154 defect shape (praise aimed at an
# uncredited node). A name with only one or two distinctive words (the
# `humanize_key` fallback, a terse label) carries no such dilution, so the
# requirement scales down to its half-length — a no-op on real prod names, none
# of which has fewer than four.
_MIN_EXCLUSIVE_HITS = 2
# Openers whose partner may be lost when a long reference name is clipped.
_BRACKET_PAIRS = {"(": ")", "[": "]", "{": "}"}
_QUOTE_CHARS = str.maketrans({'"': "'", "“": "'", "”": "'"})


def enforce_narrative_consistency(
    feedback: dict[str, Any],
    *,
    topics: Sequence[TopicCredit],
) -> dict[str, Any]:
    """Return a NEW feedback payload whose prose matches the per-node verdicts.

    ``topics`` are the ledger's final credits (``TopicScoreResult.topics``).
    Credited topics, Hoot-capped topics that still earned credit, unknown
    canonical keys, and non-string prose fields are passed through untouched, so
    a fully credited attempt is returned equal to its input. The input is never
    mutated.
    """
    uncredited = {t.canonical_key: t for t in topics if _is_uncredited(t)}
    if not uncredited:
        # An EMPTY prose field still has to be repaired here (review wave). The
        # uncredited path gets its fallback from `_repair_prose` below, but this
        # early return used to hand "" straight back — on precisely the attempts
        # (everything credited) where the model most wants to headline the score,
        # and where the grade scrub therefore most often empties the field.
        return _fill_empty_prose(feedback)
    credited = [t for t in topics if t.canonical_key not in uncredited]
    # P3.2 L3: whatever the prompt builder already spent on named misconceptions
    # comes off this gate's quota. Recomputed rather than passed in — it is a
    # pure function of the SAME `topics` sequence `build_topic_narrative_prompt`
    # was given (`diagnostic._build_topic_feedback` hands both the identical
    # `topic_score.topics`), so the two allocations cannot disagree.
    named_misconceptions = nameable_misconception_keys(topics)
    quotable = _quotable_keys(topics, uncredited, named_misconceptions)

    items = feedback.get("topic_feedback")
    repaired_items: Any = items
    quoted_gap_keys: set[str] = set()
    if isinstance(items, list):
        repaired_items = []
        for item in items:
            repaired, quoted_key = _repair_item(item, uncredited, quotable)
            repaired_items.append(repaired)
            if quoted_key is not None:
                quoted_gap_keys.add(quoted_key)

    # Prefer a topic that actually counted against the grade as the next-step
    # subject; only an all-zero-weight ledger falls back to the excluded ones.
    scored = {k: t for k, t in uncredited.items() if t.weight > 0.0}
    subject = min((scored or uncredited).values(), key=lambda t: (t.credit, t.canonical_key))
    next_step_fallback = (
        _NEXT_STEP.format(name=_quotable_name(subject))
        if subject.canonical_key in quotable and subject.canonical_key not in quoted_gap_keys
        else _NEXT_STEP_NO_QUOTE
    )
    return {
        **feedback,
        "headline": _repair_prose(
            feedback.get("headline"),
            uncredited=uncredited.values(),
            credited=credited,
            fallback=FALLBACK_HEADLINE,
        ),
        "topic_feedback": repaired_items,
        "next_step": _repair_prose(
            feedback.get("next_step"),
            uncredited=uncredited.values(),
            credited=credited,
            fallback=next_step_fallback,
        ),
    }


def _fill_empty_prose(feedback: dict[str, Any]) -> dict[str, Any]:
    """Replace a blank headline / next step with the all-credited fallbacks.

    Pure and total: a non-blank field, a non-string field, and a payload with
    neither key are all returned unchanged, so a fully credited attempt whose
    prose survived the scrub is still byte-identical to its input.
    """
    filled = dict(feedback)
    for key, fallback in (
        ("headline", FALLBACK_HEADLINE_CREDITED),
        ("next_step", FALLBACK_NEXT_STEP_CREDITED),
    ):
        value = filled.get(key)
        if isinstance(value, str) and not value.strip():
            filled[key] = fallback
    return filled


def _is_uncredited(topic: TopicCredit) -> bool:
    """True when prose may not claim the student earned this topic.

    INTERACTION5 carve-out: ``aside_penalty.apply_aside_caps`` caps a
    Hoot-assisted node at a flat ``0.5`` — always below :data:`PRAISE_FLOOR` —
    so reading its credit as "no evidence" would strip accurate praise from a
    node the adjudicator scored ``covered`` and append a factually wrong gap
    sentence. The cap is ``min(evidence, 0.5)``, so any credit above zero proves
    the adjudicator found evidence; only exactly ``0`` (reachable solely from a
    pre-cap ``0``) is a real absence.

    P3.2 L3 (2026-08-12) — DELIBERATELY not taught about the wrongness ceiling.
    A corroborated finding requires ``credit >= 0.6``, so a flagged topic is by
    construction a CREDITED topic; level 4's ``min(raw, 84)`` moves the attempt
    SCORE and never a topic's credit. Reading "this attempt was ceilinged" as
    "this topic was not credited" would strip praise the adjudicator's own
    verdict awards and append a factually wrong gap sentence. The flagged claim
    gets its own separate line in the narrative instead — credit sentence AND
    misconception note, never one at the cost of the other.
    """
    if topic.credit >= PRAISE_FLOOR:
        return False
    if getattr(topic, "hoot_assisted", False) and topic.credit > 0.0:
        return False
    return True


def _quotable_keys(
    topics: Sequence[TopicCredit],
    uncredited: dict[str, TopicCredit],
    named_misconceptions: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """The topics whose reference wording the gate may quote in this payload.

    Selection deliberately reuses D2's ordering key from
    ``topic_score._reveal_reference_text`` — lowest credit first, then most
    central (highest weight), then reference order — so the narrative names the
    same nodes ``topics[].reference_text`` reveals instead of widening the
    reveal to a different subset. Topics that counted toward the grade come
    first; an all-zero-weight ledger (every graded node excluded) falls back to
    the excluded ones, matching how the next-step subject is chosen.

    ``named_misconceptions`` (P3.2 L3) is what the prompt builder already spent
    out of the shared :data:`MAX_REFERENCE_NAME_QUOTES` budget. It is subtracted
    from the quota AND excluded from the candidate set, so the union of the two
    channels never exceeds the budget in reveals OR in distinct nodes — one
    topic can never spend two slots. Empty (every attempt below wrongness level
    3) restores the full budget and the pre-P3.2 selection exactly.
    """
    budget = MAX_REFERENCE_NAME_QUOTES - len(named_misconceptions)
    if budget <= 0:
        return frozenset()
    rank = {topic.canonical_key: index for index, topic in enumerate(topics)}
    candidates = [t for k, t in uncredited.items() if k not in named_misconceptions]
    scored = [t for t in candidates if t.weight > 0.0] or candidates
    ordered = sorted(scored, key=lambda t: (t.credit, -t.weight, rank[t.canonical_key]))
    return frozenset(t.canonical_key for t in ordered[:budget])


def _repair_item(
    item: Any, uncredited: dict[str, TopicCredit], quotable: frozenset[str]
) -> tuple[Any, str | None]:
    """Rewrite one topic note; report whether its reference wording got quoted."""
    if not isinstance(item, dict):
        return item, None
    key = item.get("canonical_key")
    topic = uncredited.get(key) if isinstance(key, str) else None
    note = item.get("note")
    if topic is None or not isinstance(note, str):
        return dict(item), None
    repaired, quoted = _repair_note(note, topic, may_quote=topic.canonical_key in quotable)
    return {**item, "note": repaired}, (topic.canonical_key if quoted else None)


def _repair_note(note: str, topic: TopicCredit, *, may_quote: bool) -> tuple[str, bool]:
    """Strip pure praise, then guarantee the gap is named when it was graded.

    ``may_quote`` is this topic's share of the per-attempt reference-wording
    budget (:data:`MAX_REFERENCE_NAME_QUOTES`). Past the budget the gap is still
    named — the note hangs off its own ``canonical_key``, so the student still
    knows which topic it is about — using the name-free template.
    """
    sentences = _split_sentences(note)
    kept = [s for s in sentences if not _is_pure_praise(s)]
    stripped = len(sentences) - len(kept)
    # A zero-weight topic left the denominator (P1.2b `unprobed`), so it is not
    # a teaching gap and must never be narrated as one.
    scored = topic.weight > 0.0
    needs_gap = scored and not any(_names_a_gap(s) for s in kept)
    if not stripped and not needs_gap and sentences:
        return note, False  # Nothing to repair — the note is served untouched.
    if stripped:
        _LOG.info(
            "apollo_narrative_praise_stripped canonical_key=%s credit=%.2f dropped=%d",
            topic.canonical_key,
            topic.credit,
            stripped,
        )
    quoted = False
    if needs_gap:
        zeroed = topic.credit <= 0.0
        if may_quote:
            template = _ZERO_GAP if zeroed else _PARTIAL_GAP
            kept.append(template.format(name=_quotable_name(topic)))
            quoted = True
        else:
            kept.append(_ZERO_GAP_NO_NAME if zeroed else _PARTIAL_GAP_NO_NAME)
        _LOG.info(
            "apollo_narrative_gap_named canonical_key=%s credit=%.2f quoted=%s",
            topic.canonical_key,
            topic.credit,
            quoted,
        )
    if not kept:
        kept.append(_UNSCORED_NOTE)
    return " ".join(kept), quoted


def _repair_prose(
    text: Any,
    *,
    uncredited: Iterable[TopicCredit],
    credited: Sequence[TopicCredit],
    fallback: str,
) -> Any:
    """Drop pure-praise sentences that are demonstrably about an uncredited topic."""
    if not isinstance(text, str):
        return text
    sentences = _split_sentences(text)
    uncredited = list(uncredited)
    kept = [
        s
        for s in sentences
        if not (_is_pure_praise(s) and _names_uncredited_topic(s, uncredited, credited))
    ]
    if not kept:
        return fallback
    return text if len(kept) == len(sentences) else " ".join(kept)


def _split_sentences(text: str) -> list[str]:
    """Split on sentence terminators, keeping common abbreviations intact."""
    sentences: list[str] = []
    for part in _SENTENCE_SPLIT_RE.split(text.strip()):
        if not part.strip():
            continue
        tail = sentences[-1].split()[-1].lower() if sentences and sentences[-1].split() else ""
        if tail in _ABBREVIATIONS:
            sentences[-1] = f"{sentences[-1]} {part}"
        else:
            sentences.append(part)
    return sentences


def _names_a_gap(sentence: str) -> bool:
    """True when the sentence states what is missing or what to do next."""
    return bool(_GAP_CUE_RE.search(sentence)) or _is_imperative(sentence)


def _is_pure_praise(sentence: str) -> bool:
    """A credit claim (or praise word) with no gap named anywhere in it."""
    claims_credit = bool(_CREDIT_CLAIM_RE.search(sentence)) or bool(
        _PRAISE_WORD_RE.search(sentence)
    )
    return claims_credit and not _names_a_gap(sentence)


def _is_imperative(sentence: str) -> bool:
    words = _WORD_RE.findall(sentence.lower())
    return bool(words) and words[0] in _IMPERATIVES


def _topic_name(topic: TopicCredit) -> str:
    return topic.display_name or humanize_key(topic.canonical_key)


def _balanced(name: str) -> str:
    """Truncate before any bracket the clipped name never closes.

    Graded display names are reference sentences and 67% of the real prod ones
    exceed the quote budget, so clipping routinely lands inside a parenthetical
    ("…the growth of information technology (enhanced capacity for…"). The
    student is meant to learn what full credit looks like from this span, so it
    ends at the last clause that closes cleanly.
    """
    open_stack: list[tuple[str, int]] = []
    for index, char in enumerate(name):
        if char in _BRACKET_PAIRS:
            open_stack.append((char, index))
        elif open_stack and char == _BRACKET_PAIRS[open_stack[-1][0]]:
            open_stack.pop()
    if not open_stack:
        return name
    trimmed = name[: open_stack[0][1]].strip(" .;,:!?-–—")
    return trimmed or name


def _quotable_name(topic: TopicCredit) -> str:
    """The topic name as it can be quoted inside one sentence.

    Reference wording is sentence-shaped and long, so trailing punctuation is
    dropped, embedded double quotes become single ones (the templates wrap the
    name in double quotes), anything past :data:`_NAME_QUOTE_CHARS` is cut at a
    word boundary, and an unclosed bracket left by that cut is removed — a
    quoted reference clause, never a run-on or a dangling parenthesis.
    """
    name = " ".join(_topic_name(topic).translate(_QUOTE_CHARS).split()).strip(" .;,:!?")
    if len(name) <= _NAME_QUOTE_CHARS:
        return _balanced(name)
    head = name[:_NAME_QUOTE_CHARS].rsplit(" ", 1)[0].strip(" .;,:!?")
    return f"{_balanced(head)}…"


def _name_tokens(topic: TopicCredit) -> set[str]:
    tokens = _WORD_RE.findall(_topic_name(topic).lower())
    return {t for t in tokens if len(t) >= _SIGNIFICANT_LEN and t not in _NAME_STOPWORDS}


def _names_uncredited_topic(
    sentence: str,
    uncredited: Sequence[TopicCredit],
    credited: Sequence[TopicCredit],
) -> bool:
    """True only on strong evidence the sentence is about an uncredited topic.

    Deleting a headline sentence usually replaces the WHOLE headline (the prompt
    asks for one sentence), so the bar is high and asymmetric — a miss leaves
    accurate-but-unpoliced praise standing, a false hit serves a canned line
    instead of real feedback. Two independent guards:

    1. the shared words must be EXCLUSIVE to the uncredited topic — a word that
       also appears in a credited topic's reference wording is evidence for the
       credited one, not against it — and there must be enough of them
       (:data:`_MIN_EXCLUSIVE_HITS`, scaled down for a one- or two-word name),
       one of them long;
    2. the sentence must overlap this topic MORE than any credited topic, so a
       sentence that is mostly about credited work is never deleted.
    """
    words = set(_WORD_RE.findall(sentence.lower()))
    credited_tokens = [_name_tokens(t) for t in credited]
    credited_union: set[str] = set().union(*credited_tokens) if credited_tokens else set()
    best_credited = max((len(t & words) for t in credited_tokens), default=0)
    for topic in uncredited:
        name_tokens = _name_tokens(topic)
        own = name_tokens & words
        exclusive = own - credited_union
        required = min(_MIN_EXCLUSIVE_HITS, max(1, (len(name_tokens) + 1) // 2))
        if (
            len(exclusive) >= required
            and any(len(t) >= _DISTINCTIVE_LEN for t in exclusive)
            and len(own) > best_credited
        ):
            return True
    return False


__all__ = [
    "FALLBACK_HEADLINE",
    "FALLBACK_HEADLINE_CREDITED",
    "FALLBACK_NEXT_STEP_CREDITED",
    "MAX_REFERENCE_NAME_QUOTES",
    "PRAISE_FLOOR",
    "enforce_narrative_consistency",
]
