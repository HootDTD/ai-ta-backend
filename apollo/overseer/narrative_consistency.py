"""Deterministic gate: the served narrative may never contradict the verdicts.

2026-08-07 bimodal-fix spec §5 P2.1 (defect U2). The narrator is already built
FROM the ledger (``topic_narrative.build_topic_narrative_prompt`` renders every
topic's status and percentage), but it is still an LLM: in prod it praised
content the coverage verdict had zeroed — attempt 154 (F/28) opened with "You
clearly contrasted democratization and centralization… gave concrete examples"
for a node graded ``missing``. Prompt rules alone cannot make that impossible,
so the served prose passes one CODE gate after generation.

Contract enforced here, per topic whose credit is below :data:`PRAISE_FLOOR`:

1. a sentence that claims credit and names no gap ("pure praise") is stripped —
   from that topic's note, and from the headline/next step when the sentence
   also names the topic;
2. the topic always ends up with its gap named — a deterministic sentence is
   appended when nothing the model wrote names one;
3. nothing else changes: with every topic at or above the floor the payload is
   returned byte-identical.

Pure, total, and idempotent — string/structural only, no second LLM call, no
IO. Runs AFTER ``sanitize_narrative`` so it sees exactly the served text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

from apollo.overseer.topic_narrative import humanize_key
from apollo.overseer.topic_score import TopicCredit

_LOG = logging.getLogger(__name__)

# Below this credit the adjudicator did not find sufficient evidence, so prose
# may not credit the student for it. Mirrors the adjudication anchor set
# {0, 0.6, 0.85, 1.0} (P1.1): 0.6 is the lowest anchor that means "landed".
PRAISE_FLOOR = 0.6

FALLBACK_HEADLINE = "Here is what Apollo did not get from your teaching yet."
# Topic names are the reference solution's own wording (prod median 220 chars,
# always sentence-shaped), so they are quoted and shortened rather than dropped
# inline — the flattened back-compat narrative has no topic headings, so the
# gap sentence has to say WHICH idea is missing on its own.
_ZERO_GAP = 'Apollo never got this from your teaching: "{name}" — walk through it explicitly next time.'
_PARTIAL_GAP = 'Only part of this landed: "{name}" — make the rest explicit next time.'
_NEXT_STEP = 'Walk Apollo through this in your own words: "{name}".'
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
# One long shared word (or two short ones) means the sentence is talking about
# that topic. Conservative on purpose: a miss leaves prose untouched.
_DISTINCTIVE_LEN = 6
_SIGNIFICANT_LEN = 4


def enforce_narrative_consistency(
    feedback: dict[str, Any],
    *,
    topics: Sequence[TopicCredit],
) -> dict[str, Any]:
    """Return a NEW feedback payload whose prose matches the per-node verdicts.

    ``topics`` are the ledger's final credits (``TopicScoreResult.topics``).
    Topics at or above :data:`PRAISE_FLOOR`, unknown canonical keys, and
    non-string prose fields are passed through untouched, so a fully credited
    attempt is returned equal to its input. The input is never mutated.
    """
    uncredited = {t.canonical_key: t for t in topics if t.credit < PRAISE_FLOOR}
    if not uncredited:
        return dict(feedback)

    items = feedback.get("topic_feedback")
    repaired_items = (
        [_repair_item(item, uncredited) for item in items] if isinstance(items, list) else items
    )
    lowest = min(uncredited.values(), key=lambda t: (t.credit, t.canonical_key))
    return {
        **feedback,
        "headline": _repair_prose(
            feedback.get("headline"),
            uncredited=uncredited,
            fallback=FALLBACK_HEADLINE,
        ),
        "topic_feedback": repaired_items,
        "next_step": _repair_prose(
            feedback.get("next_step"),
            uncredited=uncredited,
            fallback=_NEXT_STEP.format(name=_quotable_name(lowest)),
        ),
    }


def _repair_item(item: Any, uncredited: dict[str, TopicCredit]) -> Any:
    """Rewrite one topic note when its own topic was not credited."""
    if not isinstance(item, dict):
        return item
    key = item.get("canonical_key")
    topic = uncredited.get(key) if isinstance(key, str) else None
    note = item.get("note")
    if topic is None or not isinstance(note, str):
        return dict(item)
    return {**item, "note": _repair_note(note, topic)}


def _repair_note(note: str, topic: TopicCredit) -> str:
    """Strip pure praise, then guarantee the gap is named."""
    sentences = _split_sentences(note)
    kept = [s for s in sentences if not _is_pure_praise(s)]
    needs_gap = not any(_names_a_gap(s) for s in kept)
    if len(kept) == len(sentences) and not needs_gap:
        return note  # Nothing to repair — the note is served untouched.
    if len(kept) != len(sentences):
        _LOG.info(
            "apollo_narrative_praise_stripped canonical_key=%s credit=%.2f dropped=%d",
            topic.canonical_key,
            topic.credit,
            len(sentences) - len(kept),
        )
    if needs_gap:
        template = _ZERO_GAP if topic.credit <= 0.0 else _PARTIAL_GAP
        kept.append(template.format(name=_quotable_name(topic)))
        _LOG.info(
            "apollo_narrative_gap_named canonical_key=%s credit=%.2f",
            topic.canonical_key,
            topic.credit,
        )
    return " ".join(kept)


def _repair_prose(
    text: Any,
    *,
    uncredited: dict[str, TopicCredit],
    fallback: str,
) -> Any:
    """Drop pure-praise sentences that name an uncredited topic."""
    if not isinstance(text, str):
        return text
    sentences = _split_sentences(text)
    kept = [
        s
        for s in sentences
        if not (_is_pure_praise(s) and _names_a_topic(s, uncredited.values()))
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


def _quotable_name(topic: TopicCredit) -> str:
    """The topic name as it can be quoted inside one sentence.

    Reference wording is sentence-shaped and long, so trailing punctuation is
    dropped and anything past :data:`_NAME_QUOTE_CHARS` is cut at a word
    boundary — a quoted reference clause, never a run-on second sentence.
    """
    name = " ".join(_topic_name(topic).split()).strip(" .;,:!?")
    if len(name) <= _NAME_QUOTE_CHARS:
        return name
    head = name[:_NAME_QUOTE_CHARS].rsplit(" ", 1)[0].strip(" .;,:!?")
    return f"{head}…"


def _name_tokens(topic: TopicCredit) -> set[str]:
    tokens = _WORD_RE.findall(_topic_name(topic).lower())
    return {t for t in tokens if len(t) >= _SIGNIFICANT_LEN and t not in _NAME_STOPWORDS}


def _names_a_topic(sentence: str, topics: Any) -> bool:
    words = set(_WORD_RE.findall(sentence.lower()))
    for topic in topics:
        overlap = _name_tokens(topic) & words
        if any(len(t) >= _DISTINCTIVE_LEN for t in overlap) or len(overlap) >= 2:
            return True
    return False


__all__ = ["FALLBACK_HEADLINE", "PRAISE_FLOOR", "enforce_narrative_consistency"]
