"""Private-atom leakage belt for Apollo's student-facing questioning turn.

`belt_verdict` scans one candidate reply for the three atom classes that would
give the private reference material away — digits/dates, distinctive vocabulary,
and whole private phrases — and reports whether the reply has the required
one-question shape. It is **telemetry only**: `questioning/unified` logs the
verdict and still serves the reply (contrast the dead `agent/output_filter`,
which raised).

This module also owns `normalized`, the single word-level text key the whole
questioning loop compares with (evidence quotes, repeated-question detection),
so the belt and the engine can never drift into two different notions of "the
same words".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from apollo.ontology import KGGraph

#: The one tokenizer: ASCII word/number runs, everything else is a separator.
WORD_RE = re.compile(r"[a-zA-Z0-9]+")

# Deliberately small: function words and extremely common verbs, not a vocabulary allowlist.
_FUNCTION_WORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because been before
    being below between both but by can cannot could did do does doing down during each few
    for from further get gets getting got had has have having he her here hers herself him
    himself his how i if in into is it its itself just make makes made making may me might
    more most must my myself no nor not now of off on once only or other ought our ours
    ourselves out over own same she should so some such than that the their theirs them
    themselves then there these they this those through to too under until up very was we
    were what when where which while who whom why will with would you your yours yourself
    yourselves able actually also always another around ask asked asking back become becomes
    became begin begins began bring brings brought call called calling come comes came day
    days different done else end ends ended even ever every explain explained explaining
    feel feels felt find finds found first give gives gave given go goes going gone good
    happen happens happened happening help helps helped helping idea ideas
    """.split()
)


def normalized(text: str) -> str:
    """The case- and punctuation-insensitive word sequence of `text`."""
    return " ".join(WORD_RE.findall(text.casefold()))


@dataclass(frozen=True)
class BeltVerdict:
    """What one candidate reply would leak, plus whether its shape is usable."""

    malformed: bool = False
    digits: tuple[str, ...] = ()
    private_vocabulary: tuple[str, ...] = ()
    private_phrases: tuple[str, ...] = ()

    @property
    def hit(self) -> bool:
        return bool(self.digits or self.private_vocabulary or self.private_phrases)

    @property
    def offending_atoms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.digits, *self.private_vocabulary, *self.private_phrases)))


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        return [text for child in value.values() for text in _walk_strings(child)]
    if isinstance(value, (list, tuple)):
        return [text for child in value for text in _walk_strings(child)]
    return []


def private_strings(reference_graph: KGGraph) -> list[str]:
    """Every string reachable in the private reference graph, nodes and edges alike."""
    values: list[str] = []
    for node in reference_graph.nodes:
        values.extend(_walk_strings(node.model_dump(mode="json")))
    for edge in reference_graph.edges:
        values.extend(_walk_strings(edge.model_dump(mode="json")))
    return list(dict.fromkeys(values))


def belt_verdict(
    reply: str,
    *,
    reference_graph: KGGraph,
    public_text: str,
    student_messages: Sequence[str],
) -> BeltVerdict:
    """Classify `reply` against the private graph; anything the student or the
    public problem already said is safe to echo back."""
    cleaned = re.sub(r"\s+", " ", reply).strip()
    malformed = not cleaned.endswith("?") or cleaned.count("?") != 1
    normalized_reply = normalized(cleaned)
    public_and_student = normalized(f"{public_text} {' '.join(student_messages)}")
    safe_tokens = set(WORD_RE.findall(public_and_student))
    reply_tokens = WORD_RE.findall(normalized_reply)

    digits = tuple(
        dict.fromkeys(
            token
            for token in reply_tokens
            if any(char.isdigit() for char in token) and token not in safe_tokens
        )
    )
    private_values = private_strings(reference_graph)
    private_tokens = {
        token
        for value in private_values
        for token in WORD_RE.findall(normalized(value))
        if len(token) >= 3
    }
    private_vocabulary = tuple(
        dict.fromkeys(
            token
            for token in reply_tokens
            if len(token) >= 3
            and token in private_tokens
            and token not in safe_tokens
            and token not in _FUNCTION_WORDS
        )
    )
    private_phrases = tuple(
        dict.fromkeys(
            normalized_private
            for value in private_values
            if len(normalized_private := normalized(value)) >= 4
            and normalized_private in normalized_reply
            and normalized_private not in public_and_student
        )
    )
    return BeltVerdict(
        malformed=malformed,
        digits=digits,
        private_vocabulary=private_vocabulary,
        private_phrases=private_phrases,
    )


def leaks_private_content(
    reply: str,
    *,
    reference_graph: KGGraph,
    public_text: str,
    student_messages: Sequence[str],
) -> bool:
    """Boolean shorthand for `belt_verdict(...).hit`."""
    return belt_verdict(
        reply,
        reference_graph=reference_graph,
        public_text=public_text,
        student_messages=student_messages,
    ).hit


__all__ = [
    "WORD_RE",
    "BeltVerdict",
    "belt_verdict",
    "leaks_private_content",
    "normalized",
    "private_strings",
]
