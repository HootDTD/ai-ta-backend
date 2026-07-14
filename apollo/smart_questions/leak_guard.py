"""Deterministic answer-blind guard for smart-question wording.

Spec §6.4 (dimension-not-value): student-visible wording may steer toward a
reference node but must never carry its content. LLM instructions alone do not
enforce this (session 73, 2026-07-14: the writer paraphrased the reference
definition into its question), so this guard checks the actual words: a text
leaks when it contains a content word that is private to the target node —
present in the node's content but in neither the problem text nor the
student's own messages. Checking the output also catches leaks sourced from
the model's pretraining knowledge, not just from the prompt.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from apollo.ontology import Node

_WORD_RE = re.compile(r"[a-z]+")
_MIN_WORD_LEN = 5
# Normalized (see _normalize) generic words that must never count as a leak.
_STOPWORDS = frozenset(
    {
        "about",
        "above",
        "actually",
        "after",
        "again",
        "against",
        "because",
        "before",
        "being",
        "below",
        "between",
        "cannot",
        "could",
        "doing",
        "during",
        "every",
        "having",
        "instead",
        "might",
        "other",
        "really",
        "should",
        "since",
        "still",
        "their",
        "there",
        "these",
        "thing",
        "those",
        "through",
        "under",
        "until",
        "where",
        "which",
        "while",
        "without",
        "would",
    }
)


def _normalize(token: str) -> str:
    """Collapse the most common inflections so 'happens', 'happening', and
    'happen' compare equal. Deliberately crude — both sides of every
    comparison run through the same rule, so only consistency matters."""
    if token.endswith("ing") and len(token) > 6:
        return token[:-3]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _content_words(texts: Iterable[str]) -> set[str]:
    words: set[str] = set()
    for text in texts:
        for token in _WORD_RE.findall(text.casefold()):
            normalized = _normalize(token)
            if len(normalized) >= _MIN_WORD_LEN and normalized not in _STOPWORDS:
                words.add(normalized)
    return words


def _private_texts(node: Node) -> list[str]:
    """Every string the node's content carries, including list items. All
    fields are private — `purpose` strings restate the answer just as
    `meaning` does, so whitelisting fields out is not safe."""
    texts: list[str] = []
    for value in node.content.model_dump().values():
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, (list, tuple)):
            texts.extend(item for item in value if isinstance(item, str))
    return texts


def private_leak_words(
    candidate: str,
    *,
    node: Node,
    problem_text: str,
    student_messages: Sequence[str],
) -> set[str]:
    """Normalized content words in ``candidate`` that are private to ``node``.

    Public vocabulary (the problem text plus everything the student has said)
    is never a leak, however closely it matches the reference content — the
    student introduced it, so echoing it back is legitimate.
    """
    private = _content_words(_private_texts(node))
    public = _content_words([problem_text, *student_messages])
    return _content_words([candidate]) & (private - public)


def leaks_private_content(
    candidate: str,
    *,
    node: Node,
    problem_text: str,
    student_messages: Sequence[str],
) -> bool:
    """True when ``candidate`` contains a content word private to ``node``."""
    return bool(
        private_leak_words(
            candidate, node=node, problem_text=problem_text, student_messages=student_messages
        )
    )
