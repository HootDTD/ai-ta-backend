"""Resolver V2 windowing — student turns -> sentence-group windows (§5.1, T3).

Deterministic, pure text processing: each student turn is split into
sentences with a LaTeX-safe scanner (never splits inside ``$...$``,
``$$...$$``, or ``\\[...\\]``), then grouped into sliding windows of
``window_sentences`` with ``window_overlap_sentences`` overlap, hard-capped
at ``max_window_words`` words. An over-long single sentence (> the word cap)
interrupts the sliding sequence and becomes its own window, truncated at the
cap. Same turns -> same windows, always.

Stdlib only; imports nothing beyond the T1 types/config contract.
"""

from __future__ import annotations

from collections.abc import Sequence

from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.types import Window

#: Sentence terminators (outside math mode). Newline is a separator and is
#: not kept in the sentence; ``.!?`` are kept with their sentence.
_TERMINATORS: frozenset[str] = frozenset(".!?\n")


def split_sentences(text: str) -> list[str]:
    """Split ``text`` into sentences on ``.`` / ``!`` / ``?`` / newline,
    NEVER splitting inside LaTeX math (``$...$``, ``$$...$$``, ``\\[...\\]``)
    per design §5.1. Terminal punctuation stays with its sentence; empty
    fragments are dropped; output is whitespace-stripped. Deterministic
    single pass, no regex backtracking."""
    sentences: list[str] = []
    buffer: list[str] = []
    in_dollar = False  # inside $...$ or $$...$$
    in_bracket = False  # inside \[ ... \]
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "$":
            # $$ (display math) toggles ONCE, not twice.
            if i + 1 < n and text[i + 1] == "$":
                buffer.append("$$")
                i += 2
            else:
                buffer.append(ch)
                i += 1
            in_dollar = not in_dollar
            continue
        if ch == "\\" and i + 1 < n and text[i + 1] in "[]":
            in_bracket = text[i + 1] == "["
            buffer.append(text[i : i + 2])
            i += 2
            continue
        if ch in _TERMINATORS and not (in_dollar or in_bracket):
            if ch != "\n":
                buffer.append(ch)
            sentence = "".join(buffer).strip()
            if sentence:
                sentences.append(sentence)
            buffer = []
            i += 1
            continue
        buffer.append(ch)
        i += 1
    tail = "".join(buffer).strip()
    if tail:
        sentences.append(tail)
    return sentences


def _word_count(text: str) -> int:
    return len(text.split())


def _truncate_words(text: str, max_words: int) -> str:
    """First ``max_words`` whitespace-delimited words (whitespace-normalized,
    deterministic)."""
    return " ".join(text.split()[:max_words])


def _slide(sentences: Sequence[str], params: ResolverV2Params) -> list[str]:
    """Sliding sentence groups: size ``window_sentences``, step ``size -
    window_overlap_sentences`` (floored at 1). Stops after the group whose
    end reaches the last sentence, so no trailing group is ever a strict
    subset of the previous one. Each joined group is hard-capped at
    ``max_window_words``."""
    if not sentences:
        return []
    size = max(1, params.window_sentences)
    step = max(1, size - max(0, params.window_overlap_sentences))
    texts: list[str] = []
    start = 0
    while True:
        group = sentences[start : start + size]
        texts.append(_truncate_words(" ".join(group), params.max_window_words))
        if start + size >= len(sentences):
            break
        start += step
    return texts


def _turn_window_texts(turn_text: str, params: ResolverV2Params) -> list[str]:
    """Window texts for ONE turn, in transcript order. Normal-length
    sentences form sliding groups; a sentence longer than
    ``max_window_words`` becomes its own truncated window where it occurs
    and the sliding sequence restarts after it (design §5.1)."""
    texts: list[str] = []
    run: list[str] = []  # consecutive sentences within the word cap
    for sentence in split_sentences(turn_text):
        if _word_count(sentence) > params.max_window_words:
            texts.extend(_slide(run, params))
            run = []
            texts.append(_truncate_words(sentence, params.max_window_words))
        else:
            run.append(sentence)
    texts.extend(_slide(run, params))
    return texts


def build_windows(
    student_turns: Sequence[str], params: ResolverV2Params
) -> tuple[Window, ...]:
    """Student turn texts (already role-filtered, in ``turn_index`` order)
    -> the full window tuple for the attempt. ``Window.index`` is the 0-based
    global transcript order; ``Window.turn_index`` is the position of the
    source turn within ``student_turns``. Empty/whitespace turns contribute
    no windows; empty input -> ``()``."""
    windows: list[Window] = []
    for turn_index, turn_text in enumerate(student_turns):
        for text in _turn_window_texts(turn_text, params):
            windows.append(Window(index=len(windows), turn_index=turn_index, text=text))
    return tuple(windows)
